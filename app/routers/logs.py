from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.log import Log
from app.routers.auth import require_user

router = APIRouter(prefix="/logs", tags=["logs"])


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
