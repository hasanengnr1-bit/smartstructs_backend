import os
import httpx
import logging
import traceback
from fastapi import HTTPException

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

logger = logging.getLogger(__name__)

async def send_telegram_alerts(message: str, level: str = "ERROR"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    emoji = {"ERROR": "🔴", "WARNING": "🟡", "CRITICAL": "🚨"}.get(level, "ℹ️")
    text = f"{emoji} *{level}*\n\n{message[:3800]}"  # Telegram msg limit ~4096
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
            })
    except Exception:
        logger.exception("Failed to send Telegram alert")
        

async def report_and_raise_500(e: Exception, context: str = "", raise_error=True):
    logger.error(f"Error{f' in {context}' if context else ''}: {e}", exc_info=True)
    #sentry_sdk.capture_exception(e)
    if os.environ.get("ENVIRONMENT") == "production":
        await send_telegram_alerts(f"`{context}`\n```\n{traceback.format_exc()[-2000:]}\n```")
        
    if raise_error:
        raise HTTPException(status_code=500, detail="Something went wrong") from e
