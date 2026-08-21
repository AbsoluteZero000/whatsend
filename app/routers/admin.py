from datetime import datetime, timedelta, timezone
from math import ceil
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.log import Log
from app.models.token import Token
from app.models.user import User
from app.routers.auth import require_user
from app.services.csrf import csrf_protect


router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(csrf_protect)])
PAGE_SIZE = 20


async def require_admin(request: Request, db: AsyncSession) -> User:
    payload = require_user(request)
    user = await db.scalar(select(User).where(User.id == int(payload["sub"])))
    if user is None or not user.is_active or not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


@router.post("/users/{user_id}/toggle-active")
async def toggle_user_active(
    user_id: int,
    request: Request,
    q: str = Form(""),
    page: int = Form(1),
    db: AsyncSession = Depends(get_db),
):
    admin = await require_admin(request, db)
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account")
    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = not user.is_active
    await db.commit()

    query = urlencode({"page": max(1, page), "q": q.strip()})
    return RedirectResponse(url=f"/admin?{query}", status_code=303)


@router.get("")
async def admin_dashboard(
    request: Request,
    q: str = Query("", max_length=80),
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    admin = await require_admin(request, db)
    search = q.strip()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    chart_start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)

    total_users = await db.scalar(select(func.count(User.id))) or 0
    active_users = await db.scalar(select(func.count(User.id)).where(User.is_active.is_(True))) or 0
    all_time_sent = await db.scalar(select(func.count(Log.id)).where(Log.status == "sent")) or 0
    sent_this_week = await db.scalar(
        select(func.count(Log.id)).where(Log.status == "sent", Log.sent_at >= week_start)
    ) or 0

    daily_result = await db.execute(
        select(func.date(Log.sent_at), func.count(Log.id))
        .where(Log.status == "sent", Log.sent_at >= chart_start)
        .group_by(func.date(Log.sent_at))
    )
    daily_counts = {str(day): count for day, count in daily_result.all()}
    daily_activity = []
    for offset in range(7):
        day = chart_start + timedelta(days=offset)
        daily_activity.append({"label": day.strftime("%a"), "count": daily_counts.get(day.date().isoformat(), 0)})
    chart_max = max((item["count"] for item in daily_activity), default=0) or 1
    for item in daily_activity:
        item["height"] = round(item["count"] / chart_max * 100)

    jobs_count = (
        select(func.count(Job.id)).where(Job.user_id == User.id).correlate(User).scalar_subquery()
    )
    sent_count = (
        select(func.count(Log.id))
        .join(Job, Log.job_id == Job.id)
        .where(Job.user_id == User.id, Log.status == "sent")
        .correlate(User)
        .scalar_subquery()
    )
    tokens_count = (
        select(func.count(Token.id)).where(Token.user_id == User.id).correlate(User).scalar_subquery()
    )
    filters = []
    if search:
        filters.append(or_(User.username.ilike(f"%{search}%"), User.timezone.ilike(f"%{search}%")))

    filtered_total = await db.scalar(select(func.count(User.id)).where(*filters)) or 0
    pages = max(1, ceil(filtered_total / PAGE_SIZE))
    page = min(page, pages)
    result = await db.execute(
        select(
            User,
            jobs_count.label("jobs_count"),
            sent_count.label("sent_count"),
            tokens_count.label("tokens_count"),
        )
        .where(*filters)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    users = [
        {"user": row[0], "jobs_count": row[1], "sent_count": row[2], "tokens_count": row[3]}
        for row in result.all()
    ]

    return request.app.state.render(
        request,
        "admin/index.html",
        admin=admin,
        stats={
            "all_time_sent": all_time_sent,
            "sent_this_week": sent_this_week,
            "total_users": total_users,
            "active_users": active_users,
        },
        daily_activity=daily_activity,
        users=users,
        search=search,
        page=page,
        pages=pages,
        filtered_total=filtered_total,
    )
