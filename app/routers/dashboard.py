from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.log import Log
from app.models.token import Token
from app.routers.auth import get_current_user, require_user

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
async def dashboard_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    total = await db.scalar(select(func.count(Job.id)).where(Job.user_id == user_id))
    active = await db.scalar(select(func.count(Job.id)).where(Job.user_id == user_id, Job.status.in_(["pending", "active"])))
    sent = await db.scalar(select(func.count(Log.id)).where(Log.job.has(user_id=user_id), Log.status == "sent"))
    tokens_count = await db.scalar(select(func.count(Token.id)).where(Token.user_id == user_id))

    stats = {
        "total_jobs": total or 0,
        "active_jobs": active or 0,
        "sent_count": sent or 0,
        "tokens_count": tokens_count or 0,
    }
    return request.app.state.render(request, "dashboard/index.html", stats=stats)
