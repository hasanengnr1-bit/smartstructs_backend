"""Core agentic chat loop for the SmartStructs Assistant.

Orchestrates: user message → LLM → tool execution → stream → DB save.
Yields SSE events as an async generator for the route layer.
"""

import json
import uuid
import asyncio
from typing import Any, AsyncGenerator, Optional, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import ChatHistory
from core.llm.prompts import SYS_PROMPT_CHATBOT
from services.utils import get_logger, report_and_raise_500
from services.chatbot.tools import (
    build_tools,
    tools_to_openai_schema,
    build_tool_dispatch,
)
from services.chatbot.extraction_tools import build_extraction_tools
from services.chatbot.openrouter_client import stream_chat_completion
from services.chatbot.calc_sheet_store import store_calc_sheet, store_calc_sheet_db
from services.chatbot.token_usage_service import check_usage_limit, record_usage

logger = get_logger(__name__)

MAX_TOOL_ROUNDS = 15


def _sse_event(event: str, data: dict[str, Any]) -> dict[str, str]:
    """Format an SSE event dict for EventSourceResponse."""
    # The frontend parses the JSON data payload and expects a "type" field
    payload = {**data, "type": event}
    return {"event": event, "data": json.dumps(payload, default=str)}


async def _load_history(
    db: AsyncSession, session_id: str, user_email: str
) -> list[dict[str, Any]]:
    """Load conversation history from the database."""
    result = await db.execute(
        select(ChatHistory).where(
            ChatHistory.session_id == session_id,
            ChatHistory.user_email == user_email,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return []
    return cast(list[dict[str, Any]], row.messages) or []


async def _save_history(
    db: AsyncSession,
    session_id: str,
    user_email: str,
    messages: list[dict[str, Any]],
) -> None:
    """Save conversation history to the database (upsert)."""
    result = await db.execute(
        select(ChatHistory).where(
            ChatHistory.session_id == session_id,
            ChatHistory.user_email == user_email,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        row = ChatHistory(
            session_id=session_id,
            user_email=user_email,
            messages=messages,
        )
        db.add(row)
    else:
        row.messages = messages  # type: ignore[assignment]
    await db.commit()


def _build_user_content(message: str, image_data_url: Optional[str] = None) -> Any:
    """Build the user message content — text-only or multimodal."""
    if not image_data_url:
        return message
    # OpenAI vision format: list of content parts
    return [
        {"type": "image_url", "image_url": {"url": image_data_url}},
        {"type": "text", "text": message},
    ]


async def _execute_tool(
    tool_name: str,
    arguments: dict[str, Any],
    dispatch: dict[str, Any],
) -> dict[str, Any]:
    """Execute a single tool, handling both sync and async callables."""
    fn = dispatch.get(tool_name)
    if fn is None:
        return {"error": f"Unknown tool: {tool_name}"}
    try:
        if asyncio.iscoroutinefunction(fn):
            result = await fn(**arguments)
        else:
            result = await asyncio.to_thread(fn, **arguments)
        return result
    except TypeError as e:
        await report_and_raise_500(e, context="_execute_tool", raise_error=False)
        logger.warning(f"Tool {tool_name} argument error: {e}")
        return {"error": f"Invalid arguments for {tool_name}: {e}"}
    except Exception as e:
        await report_and_raise_500(e, context="_execute_tool", raise_error=False)
        logger.error(f"Tool {tool_name} execution error: {e}")
        return {"error": f"Tool {tool_name} failed: {e}"}


async def chat_stream(
    message: str,
    session_id: Optional[str],
    user_email: str,
    db: AsyncSession,
    image_data_url: Optional[str] = None,
) -> AsyncGenerator[dict[str, str], None]:
    """Main agentic loop. Yields SSE event dicts.

    Each yielded dict has {"event": "<type>", "data": "<json>"} suitable
    for sse-starlette's EventSourceResponse.
    """
    # Generate session_id if new conversation
    if not session_id:
        session_id = uuid.uuid4().hex

    # Check usage limit first
    allowed, usage_info = await check_usage_limit(db, user_email)
    if not allowed:
        yield _sse_event("error", {"message": "You have hit your monthly quota."})
        yield _sse_event("done", {"session_id": session_id})
        return

    total_input_tokens = 0
    total_output_tokens = 0

    try:
        # Load conversation history from DB
        history = await _load_history(db, session_id, user_email)

        # Build tools
        calc_tools = build_tools()
        all_tools = list(calc_tools)

        # If image is attached, register extraction tools
        latest_image: Optional[str] = image_data_url

        def get_image() -> Optional[str]:
            return latest_image

        if image_data_url:
            extraction_tools = build_extraction_tools(
                get_image=get_image, provider="openrouter"
            )
            all_tools.extend(extraction_tools)

        tool_schemas = tools_to_openai_schema(all_tools)
        dispatch = build_tool_dispatch(all_tools)

        # Build user message
        user_content = _build_user_content(message, image_data_url)
        user_msg: dict[str, Any] = {"role": "user", "content": user_content}

        # Assemble messages for the LLM
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYS_PROMPT_CHATBOT},
        ]
        messages.extend(history)
        messages.append(user_msg)

        # Track all tool results for calc sheet
        all_tool_results: list[dict[str, Any]] = []

        # Agentic loop
        for round_num in range(MAX_TOOL_ROUNDS):
            # Determine reasoning config
            reasoning = None
            if image_data_url and round_num == 0:
                # First round with image: boost reasoning for extraction
                reasoning = {"effort": "low"}

            # Stream the LLM response
            assistant_content = ""
            tool_calls_this_round: list[dict[str, Any]] = []
            finish_reason = "stop"

            async for chunk in stream_chat_completion(
                messages=messages,
                tools=tool_schemas if tool_schemas else None,
                reasoning=reasoning,
            ):
                chunk_type = chunk["type"]

                if chunk_type == "content_delta":
                    assistant_content += chunk["content"]
                    yield _sse_event("token", {"content": chunk["content"]})

                elif chunk_type == "tool_call_complete":
                    tool_calls_this_round.append(chunk)

                elif chunk_type == "finish":
                    finish_reason = chunk["reason"]

                elif chunk_type == "usage":
                    total_input_tokens += chunk.get("prompt_tokens", 0)
                    total_output_tokens += chunk.get("completion_tokens", 0)

                elif chunk_type == "error":
                    yield _sse_event("error", {"message": chunk["message"]})
                    # Save what we have so far
                    if assistant_content:
                        history.append(user_msg)
                        history.append(
                            {"role": "assistant", "content": assistant_content}
                        )
                        await _save_history(db, session_id, user_email, history)
                    yield _sse_event("done", {"session_id": session_id})
                    return

            # If no tool calls, we're done — the model gave a final text response
            if finish_reason != "tool_calls" or not tool_calls_this_round:
                # Save conversation to DB
                history.append(user_msg)
                if assistant_content:
                    history.append({"role": "assistant", "content": assistant_content})
                await _save_history(db, session_id, user_email, history)

                # Generate calc sheet if we have results
                if all_tool_results:
                    sheet = store_calc_sheet(all_tool_results)
                    if sheet:
                        calc_sheet_id, sheet_data = sheet
                        # Persist to DB (fire-and-forget; errors are logged internally)
                        await store_calc_sheet_db(
                            db=db,
                            calc_sheet_id=calc_sheet_id,
                            session_id=session_id,
                            user_email=user_email,
                            data=sheet_data,
                        )
                        yield _sse_event("calc_sheet", {"calc_sheet_id": calc_sheet_id})

                yield _sse_event("done", {"session_id": session_id})
                return

            # Tool calls detected — execute them
            # Strip any text-encoded tool_call XML from the content before saving.
            # Covers all known Gemma variants:
            #   <tool_call>...</tool_call|>   and   <|tool_call>...<tool_call|>
            import re as _re

            clean_content = _re.sub(
                r"<\|?tool_call>.*?(?:<\|?tool_call[|/]>|</tool_call>|$)",
                "",
                assistant_content,
                flags=_re.DOTALL,
            ).strip()

            # Build the assistant message with tool calls for the conversation
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if clean_content:
                assistant_msg["content"] = clean_content
            else:
                assistant_msg["content"] = None
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["arguments"], default=str),
                    },
                }
                for tc in tool_calls_this_round
            ]
            messages.append(assistant_msg)

            # Execute all tool calls concurrently
            async def _run_tool(tc: dict) -> dict:
                tool_name = tc["name"]
                args = tc["arguments"]
                tool_id = tc["id"]

                # Emit tool_start
                yield_data = {"tool": tool_name, "args": args}
                return {
                    "tool_id": tool_id,
                    "tool_name": tool_name,
                    "args": args,
                    "start_event": yield_data,
                    "result": await _execute_tool(tool_name, args, dispatch),
                }

            # We need to yield SSE events during execution, so we can't use
            # asyncio.gather with the yield. Execute and collect, then yield.
            tasks = []
            for tc in tool_calls_this_round:
                tasks.append(_execute_tool(tc["name"], tc["arguments"], dispatch))

            # Emit all tool_start events first
            for tc in tool_calls_this_round:
                yield _sse_event(
                    "tool_start", {"tool": tc["name"], "args": tc["arguments"]}
                )

            # Execute concurrently
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results and emit tool_result events
            for tc, result in zip(tool_calls_this_round, results):
                if isinstance(result, Exception):
                    result = {"error": f"Tool failed: {result}"}

                if isinstance(result, dict) and "_usage" in result:
                    tool_usage = result.pop("_usage")
                    if isinstance(tool_usage, dict):
                        total_input_tokens += tool_usage.get("prompt_tokens", 0)
                        total_output_tokens += tool_usage.get("completion_tokens", 0)

                # Emit tool_result
                yield _sse_event(
                    "tool_result",
                    {
                        "tool": tc["name"],
                        "args": tc["arguments"],
                        "result": result,
                    },
                )

                # Track for calc sheet
                all_tool_results.append(
                    {
                        "tool": tc["name"],
                        "args": tc["arguments"],
                        "result": result,
                    }
                )

                # Add tool result message for the LLM
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(result, default=str),
                    }
                )

            # Continue the loop — the LLM will process tool results

        # If we exhausted all rounds
        yield _sse_event(
            "error",
            {
                "message": "Maximum tool call rounds exceeded. Please try a simpler request."
            },
        )
        # Save whatever we have
        if history:
            await _save_history(db, session_id, user_email, history)
        yield _sse_event("done", {"session_id": session_id})

    except Exception as e:
        logger.error(f"Chat stream error: {e}", exc_info=True)
        await report_and_raise_500(e, context="chat_stream", raise_error=False)
        yield _sse_event("error", {"message": f"Internal error: {e}"})
        yield _sse_event("done", {"session_id": session_id})
    finally:
        if total_input_tokens > 0 or total_output_tokens > 0:
            await record_usage(db, user_email, total_input_tokens, total_output_tokens)
