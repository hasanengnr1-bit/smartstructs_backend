from sqlalchemy import update, delete

from services.utils import get_logger
from database import local_async_session, User, Project

logger = get_logger(__name__)


async def refund_token(user_email: str, task_id = None) -> None:
    async with local_async_session() as db:
        await db.execute(
            update(User)
            .where(User.email == user_email)
            .values(tokens=User.tokens + 1)
        )
        if task_id:
            await db.execute(
                    update(Project)
                    .where(Project.task_id == task_id)
                    .values(status="extraction_error")
            )
        await db.commit()
