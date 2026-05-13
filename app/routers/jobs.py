from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.token import Token
from app.routers.auth import require_user

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
async def list_jobs(request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(
        select(Job).where(Job.user_id == user_id).order_by(Job.created_at.desc())
    )
    jobs = result.scalars().all()
    return request.app.state.render(request, "jobs/list.html", jobs=jobs)


@router.get("/create")
async def create_job_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Token).where(Token.user_id == user_id, Token.is_active == True))
    tokens = result.scalars().all()

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return request.app.state.render(request, "jobs/form.html", tokens=tokens, now=now)


@router.post("/create")
async def create_job(
    request: Request,
    token_id: int = Form(...),
    label: str = Form(default=""),
    group_id: str = Form(...),
    message: str = Form(...),
    image_path: str = Form(default=""),
    trigger_type: str = Form(...),
    trigger_value: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])

    job = Job(
        user_id=user_id,
        token_id=token_id,
        label=label or None,
        group_id=group_id,
        message=message,
        image_path=image_path or None,
        trigger_type=trigger_type,
        trigger_value=trigger_value,
        status="pending",
    )
    db.add(job)
    await db.commit()
    return RedirectResponse(url="/jobs", status_code=303)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and job.status in ("pending", "active"):
        job.status = "cancelled"
        await db.commit()
    return RedirectResponse(url="/jobs", status_code=303)


@router.post("/{job_id}/delete")
async def delete_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job:
        await db.delete(job)
        await db.commit()
    return RedirectResponse(url="/jobs", status_code=303)
