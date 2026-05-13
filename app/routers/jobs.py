import os
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.token import Token
from app.routers.auth import require_user
from app.services.scheduler import register_job, remove_job

router = APIRouter(prefix="/jobs", tags=["jobs"])

UPLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5MB
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


async def save_upload(file: UploadFile) -> str | None:
    if not file.filename:
        return None

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=400, detail="Image exceeds 5MB limit")

    stem = datetime.now().strftime("%Y%m%d%H%M%S%f")
    dest = UPLOAD_DIR / f"{stem}{ext}"
    dest.write_bytes(content)
    return str(dest.resolve())


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
    image: UploadFile | None = None,
    trigger_type: str = Form(...),
    trigger_value: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])

    image_path = await save_upload(image) if image else None

    job = Job(
        user_id=user_id,
        token_id=token_id,
        label=label or None,
        group_id=group_id,
        message=message,
        image_path=image_path,
        trigger_type=trigger_type,
        trigger_value=trigger_value,
        status="pending",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    await register_job(job)
    await db.commit()
    return RedirectResponse(url="/jobs", status_code=303)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and job.status in ("pending", "active"):
        await remove_job(job_id)
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
        if job.image_path:
            p = Path(job.image_path)
            if p.exists():
                p.unlink()
        await remove_job(job_id)
        await db.delete(job)
        await db.commit()
    return RedirectResponse(url="/jobs", status_code=303)
