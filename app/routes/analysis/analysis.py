import json
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import APIRouter, Depends, HTTPException, Path

from services.auth import get_current_user
from database import get_db, User, Project
from services.analysis import run_structural_analysis
from services.utils import get_logger, report_and_raise_500

router = APIRouter()
logger = get_logger(__name__)


@router.get("/api/analysis/get_final_analysis/{project_id}")
async def get_final_analysis(
    project_id: str = Path(),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        stmt = select(Project.extracted_data, Project.hazard_data).where(
            Project.id == project_id, Project.user_email==user.email
        )
        result = await db.execute(stmt)
        extracted_data_row = result.one_or_none()

        if not extracted_data_row:
            raise HTTPException(status_code=401, detail="Invalid Project id.")

        analysis = run_structural_analysis(
            extracted_data = extracted_data_row.extracted_data,
            hazard_data = extracted_data_row.hazard_data if extracted_data_row.hazard_data else {}
        )
        status = analysis["summary"]["overall"].lower()

        update_stmt = (
            update(Project)
            .where(Project.id == project_id)
            .values(
                status=(status if status != "error" else "analysis_error"),
                analysis_data=analysis,
            )
        )
        await db.execute(update_stmt)
        await db.commit()

        return {"status_code": 200, "data": json.dumps(analysis)}
    
    except HTTPException as ht:
        await report_and_raise_500(ht, context="get_final_analysis: HTTP", raise_error=False)
        raise
    
    except Exception as e:
        await report_and_raise_500(e, context="Get Final Analysis: Exception")
