"""Chat endpoints: SSE streaming chat + calc-sheet HTML rendering.

POST /api/chat/stream  — Conversational chat with SSE streaming
GET  /api/chat/calc-sheet/{calc_sheet_id} — Serves HTML calc sheet report
"""

from fastapi.responses import HTMLResponse
from sqlalchemy import select, delete, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse  # type: ignore[import-untyped]
from fastapi import APIRouter, Depends, HTTPException, Request

from schema import ChatRequest
from services.rate_limiter import limiter
from services.auth import get_current_user
from services.report import build_html_report
from services.chatbot.chat_service import chat_stream
from database import get_db, User, ChatHistory, CalcSheet
from services.chatbot.calc_sheet_store import get_calc_sheet_data, build_report_inputs
from services.chatbot.token_usage_service import get_usage
from services.utils import get_logger, report_and_raise_500

router = APIRouter()
logger = get_logger(__name__)

@router.get("/api/chat/usage")
async def get_token_usage(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get current month's token usage for the authenticated user."""
    return await get_usage(db, str(user.email))



@router.post("/api/chat/stream")
@limiter.limit("200/minute")
async def chat_stream_endpoint(
    request: Request,
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """SSE streaming chat endpoint.

    Streams events:
      - token: streamed text content
      - tool_start: tool execution started
      - tool_result: tool completed with results
      - calc_sheet: calc sheet available (provides ID)
      - done: stream complete (provides session_id)
      - error: error occurred
    """
    return EventSourceResponse(
        chat_stream(
            message=body.message,
            session_id=body.session_id,
            user_email=str(user.email),
            db=db,
            image_data_url=body.image,
        )
    )


@router.get("/api/chat/calc-sheet/{calc_sheet_id}")
async def get_calc_sheet(
    calc_sheet_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Serve the HTML calculation sheet for a chat-initiated analysis.

    Checks the in-memory TTL cache first, then falls back to the database
    so calc sheets are always accessible regardless of server restarts.
    """
    try:
        data = await get_calc_sheet_data(calc_sheet_id, db=db)
        if data is None:
            raise HTTPException(
                status_code=404,
                detail="Calculation sheet not found or expired.",
            )

        extracted_data, analysis_result = build_report_inputs(data)
        html = build_html_report(
            extracted_data=extracted_data,
            analysis_result=analysis_result,
        )
        return HTMLResponse(content=html)

    except HTTPException as ht:
        await report_and_raise_500(
            ht, context="get_calc_sheet: HTTP", raise_error=False
        )
        raise

    except Exception as e:
        await report_and_raise_500(e, context="get_calc_sheet: Exception")


@router.get("/api/chat/session/{session_id}/calc-sheets")
async def get_session_calc_sheets(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all calc sheet IDs for a given chat session.

    Returns the most recent calc sheet first. The frontend uses this to
    display the 'View Calculation Sheet' button when loading a past session.
    """
    try:
        stmt = (
            select(CalcSheet)
            .where(
                CalcSheet.session_id == session_id,
                CalcSheet.user_email == str(user.email),
            )
            .order_by(desc(CalcSheet.created_at))
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return {
            "calc_sheets": [
                {
                    "calc_sheet_id": r.calc_sheet_id,
                    "created_at": r.created_at,
                }
                for r in rows
            ]
        }
    except Exception as e:
        await report_and_raise_500(e, context="get_session_calc_sheets: Exception")


@router.get("/api/chat/sessions")
async def get_chat_sessions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get all chat sessions for the current user."""
    try:
        stmt = (
            select(ChatHistory)
            .where(ChatHistory.user_email == str(user.email))
            .order_by(desc(ChatHistory.updated_at))
        )
        result = await db.execute(stmt)
        sessions = result.scalars().all()

        res = []
        for s in sessions:
            title = "New Chat"
            if s.messages and isinstance(s.messages, list) and len(s.messages) > 0:
                first_msg = next(
                    (m for m in s.messages if m.get("role") == "user"), None
                )
                if first_msg and first_msg.get("content"):
                    content_val = first_msg["content"]
                    if isinstance(content_val, list):
                        # Find the text part in the multipart message
                        text_part = next(
                            (
                                p
                                for p in content_val
                                if isinstance(p, dict) and p.get("type") == "text"
                            ),
                            None,
                        )
                        content_val = text_part.get("text", "") if text_part else ""

                    if isinstance(content_val, str):
                        title = content_val[:40]
                        if len(content_val) > 40:
                            title += "..."

            res.append(
                {
                    "session_id": s.session_id,
                    "title": title,
                    "created_at": s.created_at,
                    "updated_at": s.updated_at,
                }
            )
        return {"sessions": res}

    except HTTPException as ht:
        await report_and_raise_500(
            ht, context="get_chat_sessions: HTTP", raise_error=False
        )
        raise

    except Exception as e:
        await report_and_raise_500(e, context="get_chat_sessions: Exception")


@router.get("/api/chat/session/{session_id}")
async def get_chat_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get a specific chat session's history."""
    try:
        stmt = select(ChatHistory).where(
            ChatHistory.session_id == session_id,
            ChatHistory.user_email == str(user.email),
        )
        result = await db.execute(stmt)
        session = result.scalar_one_or_none()

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        return {"session_id": session.session_id, "messages": session.messages}

    except HTTPException as ht:
        if ht.detail == "Session not found":
            raise
        else:
            await report_and_raise_500(
                ht, context="get_chat_session: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="get_chat_session: Exception")


@router.delete("/api/chat/session/{session_id}")
async def delete_chat_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a specific chat session."""
    try:
        stmt = delete(ChatHistory).where(
            ChatHistory.session_id == session_id,
            ChatHistory.user_email == str(user.email),
        )
        result = await db.execute(stmt)
        if getattr(result, "rowcount", 0) == 0:
            raise HTTPException(status_code=404, detail="Session not found")

        await db.commit()
        return {"status": "success", "message": "Session deleted"}

    except HTTPException as ht:
        if ht.detail == "Session not found":
            raise
        else:
            await report_and_raise_500(
                ht, context="delete_chat_session: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="delete_chat_session: Exception")
