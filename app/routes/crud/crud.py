from typing import Literal
from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import (
    APIRouter,
    Depends,
    Query,
    HTTPException,
    Body,
    Query,
    Form,
    Response,
)

from schema import UserMeResponse
from database import Project, get_db, User
from services.rate_limiter import limiter
from services.utils import get_logger, report_and_raise_500
from services.auth import get_current_user, hash_password, get_reset_token_email

router = APIRouter()
logger = get_logger(__name__)


# List all
@router.get("/api/crud/get_all_projects")
async def get_all_projects(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    try:
        query = (
            select(
                Project.id,
                Project.filename,
                Project.project_name,
                Project.status,
                Project.start_time,
            )
            .where(Project.user_email == user.email)
            .order_by(Project.start_time.desc())
            .limit(limit)
            .offset(offset)
        )

        result = await db.execute(query)
        rows = result.mappings().all()  # list of dict-like rows
        return {"status_code": 200, "data": [dict(row) for row in rows]}

    except HTTPException as ht:
        await report_and_raise_500(
            ht, context="get_all_projects: HTTP", raise_error=False
        )
        raise

    except Exception as e:
        await report_and_raise_500(e, context="get_all_projects: Exception")


# get project
@router.get("/api/crud/get_project")
async def get_project(
    project_id: str = Query(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        query = select(
            Project.project_name,
            Project.extracted_data,
            Project.hazard_data,
            Project.analysis_data,
        ).where(Project.id == project_id, Project.user_email == user.email)
        result = await db.execute(query)
        row = result.one_or_none()

        if not row:
            return HTTPException(status_code=404, detail="Project not found")

        return {
            "status_code": 200,
            "data": [
                row.extracted_data,
                row.hazard_data,
                row.analysis_data,
                row.project_name,
            ],
        }

    except HTTPException as ht:
        if ht.detail == "Project not found":
            raise
        else:
            await report_and_raise_500(
                ht, context="get_project: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="get_project: Exception")


# delete project
@router.delete("/api/crud/delete_project")
async def delete_project(
    project_id: str = Query(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        query = delete(Project).where(
            Project.id == project_id, Project.user_email == user.email
        )
        await db.execute(query)
        await db.commit()
        return {"status_code": 200, "data": "Project Deleted Successfully."}

    except HTTPException as ht:
        await report_and_raise_500(
            ht, context="delete_project: HTTP", raise_error=False
        )
        raise

    except Exception as e:
        await report_and_raise_500(e, context="delete_project: Exception")


ALLOWED_DATA_TYPES = {
    "extracted_data": Project.extracted_data,
    "hazard_data": Project.hazard_data,
}


@router.post("/api/crud/update_extracted_data")
async def update_extracted_data(
    project_id: str = Query(),
    data_type: Literal["extracted_data", "hazard_data"] = Query(
        default="extracted_data"
    ),
    updated_data: dict = Body(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        column = ALLOWED_DATA_TYPES[data_type]

        stmt = (
            update(Project)
            .where(Project.id == project_id, Project.user_email == user.email)
            .values({column: updated_data})
        )
        result = await db.execute(stmt)
        await db.commit()

        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise HTTPException(status_code=404, detail="Project not found.")

        return {"status_code": 200, "data": "Data Updated Successfully."}

    except HTTPException as ht:
        if ht.detail == "Project not found":
            raise
        else:
            await report_and_raise_500(
                ht, context="update_extracted_data: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="update_extracted_data: Exception")


@router.get("/me", response_model=UserMeResponse)
async def get_current_user_profile(current_user: User = Depends(get_current_user)):
    try:
        return current_user

    except HTTPException as ht:
        await report_and_raise_500(ht, context="/me: HTTP", raise_error=False)
        raise

    except Exception as e:
        await report_and_raise_500(e, context="/me: Exception")


@router.post("/api/crud/reset-password")
async def reset_password(
    response: Response,
    password: str = Form(),
    email: str = Depends(get_reset_token_email),
    db: AsyncSession = Depends(get_db),
):
    try:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalars().first()
        if not user:
            raise HTTPException(status_code=404, detail="User doesn't exist")

        stmt = (
            update(User)
            .where(User.email == email)
            .values(password=hash_password(password))
        )
        await db.execute(stmt)
        await db.commit()

        response.delete_cookie("reset_token", path="/api/auth")
        return {"status_code": 200, "data": "Password updated successfully."}

    except HTTPException as ht:
        if ht.detail == "User doesn't exist":
            raise
        else:
            await report_and_raise_500(
                ht, context="reset_password: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="reset_password: Exception")
