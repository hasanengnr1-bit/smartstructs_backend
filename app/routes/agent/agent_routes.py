import uuid
import aiofiles  # type: ignore[import-untyped]
import aiofiles.os  # type: ignore[import-untyped]
import aiofiles.tempfile  # type: ignore[import-untyped]
from typing import Any
from sqlalchemy import update, delete
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import UploadFile, Query, File, Depends, APIRouter, HTTPException, Request

from database import get_db
from database import User, Project
from schema import get_project_info
from services.rate_limiter import limiter
from services.auth import get_current_user
from core.llm.clients import google_client_async
from taskiq_task import data_extraction_task, broker
from services.utils import get_logger, refund_token, report_and_raise_500
from services.hazard import get_seismic_data, get_snow_data, get_wind_data

router = APIRouter()

logger = get_logger(__name__)


@router.post("/api/start_agent")
@limiter.limit("150/minute")
async def start_agent(
    request: Request,
    structure_plan: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    project_info=Depends(get_project_info),
):
    tmp_path = None
    if structure_plan.content_type != "application/pdf":
        raise HTTPException(
            status_code=400, detail="Invalid file type. Only upload PDFs."
        )

    try:
        # decrement the token.
        result = await db.execute(
            update(User)
            .where(User.email == user.email, User.tokens >= 1)
            .values(tokens=User.tokens - 1)
            .returning(User.tokens)
        )
        new_balance = result.scalar_one_or_none()
        await db.commit()

        if new_balance is None:
            raise HTTPException(status_code=402, detail="Insufficient tokens.")

        # Get Hazard Data
        try:
            wind_data = await get_wind_data(
                long=project_info.longitude_scaled, lat=project_info.latitude_scaled
            )
            snow_data = await get_snow_data(
                long=project_info.longitude_scaled,
                lat=project_info.latitude_scaled,
                risk_category=project_info.risk_category,
            )
            seismic_data = await get_seismic_data(
                long=project_info.longitude_scaled,
                lat=project_info.latitude_scaled,
                risk_category=project_info.risk_category,
                site_class=project_info.site_class,
            )
        except Exception as e:
            try:
                await refund_token(user_email=str(user.email))
            except Exception as e_:
                await report_and_raise_500(
                    e_, context="start_agent: Refund Token", raise_error=False
                )
            await report_and_raise_500(
                e, context="Start Agent: Fetching Hazard Data", raise_error=False
            )
            raise HTTPException(
                status_code=500,
                detail="Something Went wrong while fetching hazard data for this long and lat.",
            )

        if wind_data["status"] == "error":
            try:
                await refund_token(user_email=str(user.email))
            except Exception as e_:
                await report_and_raise_500(
                    e_, context="start_agent: Refund Token", raise_error=False
                )
            raise HTTPException(status_code=400, detail=wind_data["detail"])
        if snow_data["status"] == "error":
            try:
                await refund_token(user_email=str(user.email))
            except Exception as e_:
                await report_and_raise_500(
                    e_, context="start_agent: Refund Token", raise_error=False
                )
            raise HTTPException(status_code=400, detail=snow_data["detail"])
        if seismic_data["status"] == "error":
            try:
                await refund_token(user_email=str(user.email))
            except Exception as e_:
                await report_and_raise_500(
                    e_, context="start_agent: Refund Token", raise_error=False
                )
            raise HTTPException(status_code=400, detail=seismic_data["detail"])

        hazard_data = {
            "input": {
                "risk_category": project_info.risk_category,
                "site_class": project_info.site_class,
                "exposure_category": project_info.exposure_category,
                "latitude": project_info.latitude_scaled,
                "longitude": project_info.longitude_scaled,
            },
            "wind": {"wind_speed_mph": wind_data["value"]},
            "snow": {"snow_psf": snow_data["value"]},
            "seismic": seismic_data["data"],
        }

        project_id = str(uuid.uuid4())
        filename = structure_plan.filename

        file_content = await structure_plan.read()

        # Upload File to Google
        try:
            async with aiofiles.tempfile.NamedTemporaryFile(
                mode="wb", suffix=".pdf", delete=False
            ) as tmp_file:
                await tmp_file.write(file_content)
                tmp_path = tmp_file.name

            uploaded_file = await google_client_async.files.upload(
                file=tmp_path,
                config={
                    "mime_type": "application/pdf",
                    "display_name": f"project_{project_id}.pdf",
                },
            )
        except Exception as e:
            try:
                await refund_token(user_email=str(user.email))
            except Exception as e_:
                await report_and_raise_500(
                    e_, context="start_agent: Refund Token", raise_error=False
                )
            await report_and_raise_500(e, context="Start Agent: Upload File Failed")

        finally:
            if tmp_path:
                try:
                    await aiofiles.os.remove(tmp_path)
                except Exception as cleanup_error:
                    await report_and_raise_500(
                        cleanup_error,
                        context="Start Agent: Cleanup Error",
                        raise_error=False,
                    )

        # Create Project
        try:
            project = Project(
                id=project_id,
                project_name=project_info.name,
                user_email=user.email,
                filename=filename,
                status="extracting",
                longitude=project_info.longitude_scaled,
                latitude=project_info.latitude_scaled,
                risk_category=project_info.risk_category,
                design_method=project_info.design_method,
                design_code=project_info.design_code,
                site_class=project_info.site_class,
                exposure_category=project_info.exposure_category,
                hazard_data=hazard_data,
                # no task_id yet — set it after enqueueing
            )
            db.add(project)
            await db.commit()
        except Exception as e:
            try:
                await refund_token(user_email=str(user.email))
            except Exception as e_:
                await report_and_raise_500(
                    e_, context="start_agent: Refund Token", raise_error=False
                )
            await report_and_raise_500(e, context="Start Agent: Inserting in DB")

        # Enqueue the Task to Taskiq
        try:
            task = await data_extraction_task.kiq(
                file_uri=str(uploaded_file.uri or ""),
                project_id=project_id,
                user_email=str(user.email),
            )
            await db.execute(
                update(Project)
                .where(Project.id == project_id)
                .values(task_id=str(task.task_id))
            )
            await db.commit()
        except Exception as e:
            try:
                await refund_token(user_email=str(user.email))
            except Exception as e_:
                await report_and_raise_500(e_, context="start_agent: Refund Token")
            await db.execute(delete(Project).where(Project.id == project_id))
            await db.commit()
            await report_and_raise_500(e, context="Start Agent: Enqueueing task")

        return {
            "status_code": 200,
            "data": "data extraction started",
            "project_id": project_id,
            "task_id": task.task_id,
            "file_uri": uploaded_file.name,
        }

    except HTTPException as ht:
        raise

    except Exception as e:
        await report_and_raise_500(e, context="Start Agent")


@router.get("/api/check_status")
async def check_status(task_id: str = Query(), current_user=Depends(get_current_user)):
    try:
        result_backend = broker.result_backend

        if not result_backend:
            await refund_token(user_email=str(current_user.email), task_id=task_id)
            raise HTTPException(status_code=404, detail="Backend Doesn't exist")

        is_ready = await result_backend.is_result_ready(task_id)

        if not is_ready:
            return {
                "task_id": task_id,
                "status": "PENDING",
                "message": "Task is still being processed by the worker.",
            }

        result = await result_backend.get_result(task_id)

        data: Any = result.return_value

        if data is None:
            await refund_token(user_email=str(current_user.email), task_id=task_id)
            raise HTTPException(status_code=500, detail="Task Returned no data")

        if data.get("status") == "success":
            return {
                "task_id": task_id,
                "status": "SUCCESS",
                "data": data["data"],
                "project_id": data.get("project_id"),
            }

        else:
            await report_and_raise_500(
                data["data"], context="Check Status: error form taskiq task."
            )

    except HTTPException as ht:
        await report_and_raise_500(ht, context="Check Status: HTTP", raise_error=False)
        raise

    except Exception as e:
        await report_and_raise_500(e, context="Check Status")
