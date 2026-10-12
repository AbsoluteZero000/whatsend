from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
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
    status: str = Query("all", pattern="^(all|sent|failed)$"),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])

    query = select(Log).join(Job).where(Job.user_id == user_id)
    if job_id is not None:
        query = query.where(Log.job_id == job_id)
    if status != "all":
        query = query.where(Log.status == status)
    total = await db.scalar(select(func.count()).select_from(query.subquery())) or 0
    pages = max(1, (total + 49) // 50)
    page = min(page, pages)
    query = query.options(selectinload(Log.job)).order_by(Log.sent_at.desc(), Log.id.desc()).offset((page - 1) * 50).limit(50)

    result = await db.execute(query)
    logs = result.scalars().all()
    return request.app.state.render(request, "logs/list.html", logs=logs, job_id=job_id,
                                    current_status=status, page=page, pages=pages, total=total)
