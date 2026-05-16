from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.log import Log
from app.routers.auth import require_user

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("/{log_id}")
async def log_detail(request: Request, log_id: int, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(
        select(Log).join(Job).where(Log.id == log_id, Job.user_id == user_id)
    )
    log_entry = result.scalar_one_or_none()
    if not log_entry:
        raise HTTPException(status_code=404, detail="Log not found")

    result = await db.execute(select(Job).where(Job.id == log_entry.job_id))
    job = result.scalar_one()

    return request.app.state.render(request, "logs/detail.html", log=log_entry, job=job)


@router.get("")
async def list_logs(
    request: Request,
    job_id: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])

    query = select(Log).join(Job).where(Job.user_id == user_id)
    if job_id is not None:
        query = query.where(Log.job_id == job_id)
    query = query.order_by(Log.sent_at.desc())

    result = await db.execute(query)
    logs = result.scalars().all()
    return request.app.state.render(request, "logs/list.html", logs=logs, job_id=job_id)
