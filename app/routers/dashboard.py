from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.job import Job
from app.models.log import Log
from app.models.token import Token
from app.models.user import User
from app.routers.auth import get_current_user, require_user
from app.services.csrf import csrf_protect

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(csrf_protect)])


@router.get("")
async def dashboard_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    total = await db.scalar(select(func.count(Job.id)).where(Job.user_id == user_id))
    active = await db.scalar(select(func.count(Job.id)).where(Job.user_id == user_id, Job.status.in_(["pending", "active", "retry_scheduled"])))
    sent = await db.scalar(select(func.count(Log.id)).where(Log.job.has(user_id=user_id), Log.status == "sent"))
    failed = await db.scalar(select(func.count(Log.id)).where(Log.job.has(user_id=user_id), Log.status == "failed"))
    tokens_count = await db.scalar(select(func.count(Token.id)).where(Token.user_id == user_id))
    active_tokens = await db.scalar(
        select(func.count(Token.id)).where(Token.user_id == user_id, Token.is_active.is_(True))
    )

    total_attempts = (sent or 0) + (failed or 0)
    success_rate = round((sent or 0) / total_attempts * 100) if total_attempts > 0 else 0

    active_jobs_result = await db.execute(
        select(Job)
        .where(Job.user_id == user_id, Job.status.in_(["pending", "active", "trigger", "retry_scheduled"]))
        .order_by(Job.updated_at.desc())
        .limit(6)
    )
    active_jobs = active_jobs_result.scalars().all()

    recent_logs_result = await db.execute(
        select(Log)
        .join(Job)
        .options(selectinload(Log.job))
        .where(Job.user_id == user_id)
        .order_by(Log.sent_at.desc())
        .limit(5)
    )
    recent_logs = recent_logs_result.scalars().all()

    hour = datetime.now().hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 18:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    u_result = await db.execute(select(User).where(User.id == user_id))
    db_user = u_result.scalar_one_or_none()
    onboarded = db_user.onboarded if db_user else True

    stats = {
        "total_jobs": total or 0,
        "active_jobs": active or 0,
        "sent_count": sent or 0,
        "failed_count": failed or 0,
        "success_rate": success_rate,
        "tokens_count": tokens_count or 0,
        "active_tokens": active_tokens or 0,
    }
    return request.app.state.render(
        request,
        "dashboard/index.html",
        greeting=greeting,
        stats=stats,
        active_jobs=active_jobs,
        recent_logs=recent_logs,
        username=user["username"],
        onboarded=onboarded,
    )
