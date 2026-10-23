from typing import Any
from datetime import date
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from database import TokenUsage
from services.utils import get_logger, report_and_raise_500

logger = get_logger(__name__)

INPUT_TOKEN_LIMIT = 2_000_000
OUTPUT_TOKEN_LIMIT = 500_000
# INPUT_TOKEN_LIMIT = 0
# OUTPUT_TOKEN_LIMIT = 0


def _get_current_period() -> date:
    """Return the first day of the current calendar month."""
    return date.today().replace(day=1)


def _get_next_period() -> date:
    """Return the first day of the next calendar month."""
    today = date.today()
    if today.month == 12:
        return today.replace(year=today.year + 1, month=1, day=1)
    return today.replace(month=today.month + 1, day=1)


async def check_usage_limit(db: AsyncSession, user_email: str) -> tuple[bool, dict]:
    """Check if user is within their monthly limits.

    Returns:
        (allowed: bool, usage_info: dict)
    """
    period_start = _get_current_period()
    resets_on = _get_next_period()

    stmt = select(TokenUsage).where(
        TokenUsage.user_email == user_email, TokenUsage.period_start == period_start
    )
    result = await db.execute(stmt)
    usage = result.scalar_one_or_none()

    usage_info: dict[str, Any] = {
        "input_used": usage.input_tokens_used if usage else 0,
        "input_limit": INPUT_TOKEN_LIMIT,
        "output_used": usage.output_tokens_used if usage else 0,
        "output_limit": OUTPUT_TOKEN_LIMIT,
        "period_start": period_start.isoformat(),
        "resets_on": resets_on.isoformat(),
    }

    allowed: bool = (
        int(usage_info["input_used"]) < INPUT_TOKEN_LIMIT
        and int(usage_info["output_used"]) < OUTPUT_TOKEN_LIMIT
    )

    return allowed, usage_info


async def record_usage(
    db: AsyncSession,
    user_email: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    """Atomically increment the user's token usage for the current month.

    Uses INSERT ... ON CONFLICT UPDATE for an atomic upsert.
    """
    if input_tokens <= 0 and output_tokens <= 0:
        return

    period_start = _get_current_period()

    stmt = insert(TokenUsage).values(
        user_email=user_email,
        period_start=period_start,
        input_tokens_used=input_tokens,
        output_tokens_used=output_tokens,
    )

    # On conflict (record already exists for this user/month), add to existing counts
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_email", "period_start"],
        set_={
            "input_tokens_used": TokenUsage.input_tokens_used + input_tokens,
            "output_tokens_used": TokenUsage.output_tokens_used + output_tokens,
        },
    )

    try:
        await db.execute(stmt)
        await db.commit()
    except Exception as e:
        logger.error(f"Failed to record token usage for {user_email}: {e}")
        await report_and_raise_500(e, context="record_usage", raise_error=False)
        await db.rollback()


async def get_usage(db: AsyncSession, user_email: str) -> dict:
    """Return current month's usage and limits for display."""
    _, usage_info = await check_usage_limit(db, user_email)
    return usage_info
