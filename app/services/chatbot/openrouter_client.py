"""Async OpenRouter client for Gemma 4 with streaming and function-calling.

Handles:
- SSE streaming of chat completions
- Tool-call chunk accumulation and malformed-argument sanitization
- Timeout + retry (5xx / 429)
- Vision (image_url content parts) — same model, Gemma 4 is natively multimodal
"""

import os
import json
import asyncio
import re
from typing import Any, AsyncGenerator, Optional

import httpx

from services.utils import get_logger, report_and_raise_500

logger = get_logger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemma-4-31b-it"
TIMEOUT_S = 75
MAX_RETRIES = 2


def _get_model() -> str:
    return os.environ.get("CHATBOT_MODEL") or DEFAULT_MODEL


def _get_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    return key


def _sanitize_tool_arguments(raw: str) -> dict:
    """Attempt to parse tool-call arguments; fall back to regex extraction."""
    if not raw or not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try to extract a JSON object from the string
    match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    # Last resort: try to fix common issues (trailing commas, unquoted keys)
    cleaned = re.sub(r",\s*}", "}", raw)
    cleaned = re.sub(r",\s*]", "]", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(f"Could not parse tool arguments: {raw[:200]}")
        return {"_raw": raw}


# Gemma sometimes outputs tool calls as inline XML text instead of using the
# OpenAI function-calling API format.  Known variants:
#
#   <tool_call>call:TOOL_NAME{JSON_ARGS}<tool_call|>
#   <|tool_call>call:TOOL_NAME{ARGS_WITH_PIPE_QUOTES}<tool_call|>
#   <tool_call>{"name": "TOOL_NAME", "arguments": {...}}</tool_call>
#
# The <|tool_call> form uses <|"|> as an encoded double-quote — we normalise
# those before trying to parse the argument JSON.

# Opening tag pattern — matches <tool_call> and <|tool_call>
_TC_OPEN_STR = r"(?:<\|tool_call>|<tool_call>)"
# Closing tag pattern
_TC_CLOSE_STR = r"(?:<\|?tool_call[|/]>|</tool_call>)?"

_TEXT_TOOL_CALL_RE = re.compile(
    _TC_OPEN_STR + r"\s*" r"(?:"
    # variant 1: call:name{args}
    r"call:(?P<name1>[A-Za-z0-9_]+)\s*(?P<args1>\{.*?\})" r"|"
    # variant 2: JSON object with a 'name' key
    r"(?P<json_blob>\{.*?\})" r")" r"\s*" + _TC_CLOSE_STR,
    re.DOTALL,
)

# Pre-compiled pattern for detecting any opening tool-call tag in a stream chunk
_TC_OPEN_RE = re.compile(r"<\|?tool_call>")


def _decode_gemma_text(raw: str) -> str:
    """Normalise Gemma pipe-token encodings to plain ASCII.

    Gemma sometimes encodes special characters with pipe-separator syntax:
      <|"|>  ->  "   (double-quote)
      <|'|>  ->  '   (single-quote)
    """
    raw = raw.replace('<|"|>', '"')
    raw = raw.replace("<|'|>", "'")
    return raw


def _parse_text_tool_calls(text: str) -> list[dict[str, Any]]:
    """Extract tool calls embedded as text by Gemma.

    Normalises pipe-encoded quotes first, then scans for all known tag variants.
    Returns a list of dicts shaped like tool_call_complete events, or [] if none.
    """
    normalised = _decode_gemma_text(text)
    calls = []  # type: ignore[var-annotated]
    for m in _TEXT_TOOL_CALL_RE.finditer(normalised):
        if m.group("name1"):
            name = m.group("name1")
            args = _sanitize_tool_arguments(m.group("args1") or "")
        else:
            blob = _sanitize_tool_arguments(m.group("json_blob") or "")
            name = blob.pop("name", "") or blob.pop("function", "")
            args = blob.get("arguments") or blob.get("args") or blob
            if isinstance(args, str):
                args = _sanitize_tool_arguments(args)
        if name:
            calls.append(
                {
                    "type": "tool_call_complete",
                    "index": len(calls),
                    "id": f"text_tc_{len(calls)}",
                    "name": name,
                    "arguments": args,
                }
            )
    return calls


async def stream_chat_completion(
    messages: list[dict[str, Any]],
    tools: Optional[list[dict[str, Any]]] = None,
    *,
    reasoning: Optional[dict[str, Any]] = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Stream a chat completion from OpenRouter.

    Yields dicts with one of these shapes:
        {"type": "content_delta", "content": "..."}
        {"type": "tool_call_delta", "index": int, "id": str, "name": str, "arguments_delta": str}
        {"type": "tool_call_complete", "index": int, "id": str, "name": str, "arguments": dict}
        {"type": "finish", "reason": str}
        {"type": "error", "message": str}
    """
    model = _get_model()
    api_key = _get_api_key()

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": 16384,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
    if reasoning:
        payload["reasoning"] = reasoning

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://smartstructs.org",
        "X-Title": "SmartStructs Assistant",
    }
    # Accumulate tool call chunks (they arrive incrementally)
    tool_calls_acc: dict[int, dict[str, Any]] = {}
    # Track full text to detect Gemma's text-encoded tool calls
    accumulated_text: str = ""
    # Whether we've seen the opening <tool_call> tag (suppress streaming)
    in_text_tool_call: bool = False

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(TIMEOUT_S, connect=15.0)
            ) as client:
                async with client.stream(
                    "POST", OPENROUTER_URL, headers=headers, json=payload
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        body = await response.aread()
                        last_error = (
                            f"HTTP {response.status_code}: {body.decode()[:300]}"
                        )
                        logger.warning(
                            f"OpenRouter {response.status_code} (attempt {attempt + 1}): {last_error}"
                        )
                        if attempt < MAX_RETRIES:
                            await asyncio.sleep(2**attempt)
                            continue
                        yield {"type": "error", "message": last_error}
                        return

                    if response.status_code != 200:
                        body = await response.aread()
                        yield {
                            "type": "error",
                            "message": f"HTTP {response.status_code}: {body.decode()[:500]}",
                        }
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            # Flush any remaining proper tool calls
                            for idx, tc in sorted(tool_calls_acc.items()):
                                yield {
                                    "type": "tool_call_complete",
                                    "index": idx,
                                    "id": tc.get("id", ""),
                                    "name": tc.get("name", ""),
                                    "arguments": _sanitize_tool_arguments(
                                        tc.get("arguments", "")
                                    ),
                                }
                            # Check accumulated text for text-encoded tool calls
                            if accumulated_text and not tool_calls_acc:
                                text_calls = _parse_text_tool_calls(accumulated_text)
                                if text_calls:
                                    logger.info(
                                        f"Detected {len(text_calls)} text-encoded tool call(s) at [DONE]"
                                    )
                                    for tc in text_calls:
                                        yield tc
                                    yield {"type": "finish", "reason": "tool_calls"}
                                    return
                            yield {"type": "finish", "reason": "stop"}
                            return

                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        # Check for error in chunk
                        if "error" in chunk:
                            err_msg = chunk["error"]
                            if isinstance(err_msg, dict):
                                err_msg = err_msg.get("message", str(err_msg))
                            yield {"type": "error", "message": str(err_msg)}
                            return

                        # Extract usage if present
                        if "usage" in chunk and chunk["usage"]:
                            print("Usage: ", chunk["usage"])
                            usage = chunk["usage"]
                            yield {
                                "type": "usage",
                                "prompt_tokens": usage.get("prompt_tokens", 0),
                                "completion_tokens": usage.get("completion_tokens", 0),
                                "total_tokens": usage.get("total_tokens", 0),
                            }

                        choices = chunk.get("choices") or []
                        if not choices:
                            continue

                        delta = choices[0].get("delta") or {}
                        finish_reason = choices[0].get("finish_reason")

                        # Text content — suppress if it looks like a tool_call block
                        content = delta.get("content")
                        if content:
                            accumulated_text += content
                            # Detect either <tool_call> or <|tool_call> opening
                            open_match = _TC_OPEN_RE.search(content)
                            if open_match:
                                in_text_tool_call = True
                            if not in_text_tool_call:
                                yield {"type": "content_delta", "content": content}
                            elif open_match:
                                # Emit any clean text that came BEFORE the tag
                                pre = content[: open_match.start()]
                                if pre:
                                    yield {"type": "content_delta", "content": pre}

                        # Tool calls (streamed incrementally via API)
                        tc_deltas = delta.get("tool_calls") or []
                        for tc_delta in tc_deltas:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": "",
                                    "name": "",
                                    "arguments": "",
                                }
                            acc = tool_calls_acc[idx]
                            if tc_delta.get("id"):
                                acc["id"] = tc_delta["id"]
                            fn = tc_delta.get("function") or {}
                            if fn.get("name"):
                                acc["name"] = fn["name"]
                            if fn.get("arguments"):
                                acc["arguments"] += fn["arguments"]

                        # Finish reason
                        if finish_reason:
                            if finish_reason == "tool_calls":
                                # Flush accumulated API-style tool calls
                                for idx, tc in sorted(tool_calls_acc.items()):
                                    yield {
                                        "type": "tool_call_complete",
                                        "index": idx,
                                        "id": tc.get("id", ""),
                                        "name": tc.get("name", ""),
                                        "arguments": _sanitize_tool_arguments(
                                            tc.get("arguments", "")
                                        ),
                                    }
                                tool_calls_acc.clear()
                                yield {
                                    "type": "finish",
                                    "reason": "tool_calls",
                                }
                                continue
                            else:
                                # Flush any remaining API-style tool calls
                                for idx, tc in sorted(tool_calls_acc.items()):
                                    yield {
                                        "type": "tool_call_complete",
                                        "index": idx,
                                        "id": tc.get("id", ""),
                                        "name": tc.get("name", ""),
                                        "arguments": _sanitize_tool_arguments(
                                            tc.get("arguments", "")
                                        ),
                                    }
                                tool_calls_acc.clear()

                                # Check if Gemma used text-encoded tool calls instead
                                if accumulated_text and not tool_calls_acc:
                                    text_calls = _parse_text_tool_calls(
                                        accumulated_text
                                    )
                                    if text_calls:
                                        logger.info(
                                            f"Detected {len(text_calls)} text-encoded tool call(s) "
                                            f"(finish_reason={finish_reason})"
                                        )
                                        for tc in text_calls:
                                            yield tc
                                        yield {"type": "finish", "reason": "tool_calls"}
                                        accumulated_text = ""
                                        continue

                                yield {
                                    "type": "finish",
                                    "reason": finish_reason,
                                }
                                continue
            # If we reach here without returning, the stream ended without [DONE]
            # Flush remaining tool calls
            for idx, tc in sorted(tool_calls_acc.items()):
                yield {
                    "type": "tool_call_complete",
                    "index": idx,
                    "id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "arguments": _sanitize_tool_arguments(tc.get("arguments", "")),
                }
            yield {"type": "finish", "reason": "stop"}
            return

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_error = str(e)
            logger.warning(
                f"OpenRouter connection error (attempt {attempt + 1}): {last_error}"
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(2**attempt)
                continue
            yield {"type": "error", "message": f"Connection failed: {last_error}"}
            return

        except Exception as e:
            logger.error(f"OpenRouter unexpected error: {e}")
            await report_and_raise_500(
                e, context="openrouter_client", raise_error=False
            )
            yield {"type": "error", "message": f"Unexpected error: {e}"}
            return
