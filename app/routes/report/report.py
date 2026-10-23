import os
import tempfile
from sqlalchemy import select
from fastapi.responses import FileResponse
from fastapi.concurrency import run_in_threadpool
from fastapi import APIRouter, Depends, Query, HTTPException, BackgroundTasks, Request

from database import Project, get_db
from services.rate_limiter import limiter
from services.auth import get_current_user
from services.report import build_html_report, render_pdf
from services.utils import get_logger, report_and_raise_500

router = APIRouter()

logger = get_logger(__name__)


def remove_file(path: str):
    """Safely removes the temporary file after the download completes."""
    if os.path.exists(path):
        os.remove(path)


@router.get("/api/download-report")
@limiter.limit("200/minute")
async def download_report(
    request: Request,
    background_task: BackgroundTasks,
    user=Depends(get_current_user),
    db=Depends(get_db),
    project_id: str = Query(...),
):
    try:
        query = select(
            Project.project_name,
            Project.design_code,
            Project.design_method,
            Project.risk_category,
            Project.site_class,
            Project.longitude,
            Project.latitude,
            Project.analysis_data,
            Project.hazard_data,
        ).where(Project.id == project_id, Project.user_email == user.email)
        result = await db.execute(query)
        row = result.one_or_none()

        if not row:
            raise HTTPException(status_code=404, detail="Project does not exist.")

        if not row.analysis_data:
            raise HTTPException(
                status_code=400,
                detail="No analysis results for this project yet, run the analysis first.",
            )

        project_data = {
            "project_data": {
                "project_name": row.project_name,
                "design_code": row.design_code,
                "design_method": row.design_method,
                "risk_category": row.risk_category,
                "site_class": row.site_class,
                "longitude": (
                    float(row.longitude) if row.longitude is not None else None
                ),
                "latitude": float(row.latitude) if row.latitude is not None else None,
            },
            "hazard_lookup_result": row.hazard_data,
        }

        html = build_html_report(
            extracted_data=project_data, analysis_result=row.analysis_data
        )

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            temp_path = tmp.name

        await run_in_threadpool(render_pdf, html=html, target_path=temp_path)

        background_task.add_task(remove_file, temp_path)

        clean_filename = f"{row.project_name.replace(' ', '_')}_report.pdf"
        return FileResponse(
            path=temp_path, filename=clean_filename, media_type="application/pdf"
        )

    except HTTPException as ht:
        if ht.detail in [
            "Project does not exist.",
            "No analysis results for this project yet, run the analysis first.",
        ]:
            raise
        else:
            await report_and_raise_500(
                ht, context="download_report: HTTP", raise_error=False
            )
            raise

    except Exception as e:
        await report_and_raise_500(e, context="download_report: Exception")
