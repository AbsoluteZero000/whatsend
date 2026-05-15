import os
import zoneinfo
from datetime import datetime
from pathlib import Path

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import func as sqlfunc, select
from sqlalchemy import asc, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.token import Token
from app.routers.auth import require_user
from app.services.crypto import decrypt_token
from app.services.scheduler import register_job, remove_job, send_job
from app.services.sender import WhatsAppSender

router = APIRouter(prefix="/jobs", tags=["jobs"])

def redirect_with_flash(url: str, success: str = "") -> RedirectResponse:
    if success:
        url += ("&" if "?" in url else "?") + urlencode({"success": success})
    return RedirectResponse(url=url, status_code=303)

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(Path(__file__).resolve().parent.parent.parent / "uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_SIZE = 5 * 1024 * 1024  # 5MB
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def local_to_utc(date_str: str, tz_name: str = "UTC") -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        tz_obj = zoneinfo.ZoneInfo(tz_name)
        aware = dt.replace(tzinfo=tz_obj)
        utc = aware.astimezone(zoneinfo.ZoneInfo("UTC"))
        return utc.strftime("%Y-%m-%d %H:%M")
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return date_str


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


ALLOWED_SORT_COLS = {"label", "group_name", "trigger_type", "status", "created_at"}

@router.get("")
async def list_jobs(
    request: Request,
    status: str = "active",
    q: str = "",
    sort_by: str = "created_at",
    sort_order: str = "desc",
    page: int = 1,
    per_page: int = 25,
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])

    if sort_by not in ALLOWED_SORT_COLS:
        sort_by = "created_at"
    if sort_order not in ("asc", "desc"):
        sort_order = "desc"

    base_query = select(Job).where(Job.user_id == user_id)
    if status == "active":
        base_query = base_query.where(Job.status.in_(["pending", "active", "trigger"]))
    elif status == "completed":
        base_query = base_query.where(Job.status.in_(["completed", "cancelled"]))
    elif status == "paused":
        base_query = base_query.where(Job.status == "paused")
    elif status == "failed":
        base_query = base_query.where(Job.status == "failed")
    if q:
        like = f"%{q}%"
        base_query = base_query.where(
            Job.label.ilike(like) | Job.group_name.ilike(like) | Job.group_id.ilike(like)
        )

    count_q = select(sqlfunc.count()).select_from(base_query.subquery())
    total = (await db.execute(count_q)).scalar() or 0
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    order_col = getattr(Job, sort_by)
    order_fn = desc if sort_order == "desc" else asc
    query = base_query.order_by(order_fn(order_col)).offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(query)
    jobs = result.scalars().all()

    missing = [j for j in jobs if not j.group_name]
    if missing:
        result = await db.execute(select(Token).where(Token.user_id == user_id, Token.is_active == True))
        tokens = result.scalars().all()
        group_map: dict[str, str] = {}
        for t in tokens:
            try:
                sender = WhatsAppSender(api_token=decrypt_token(t.api_token))
                groups = await sender.get_groups()
                group_map = {g["id"]: g.get("name") or g["id"] for g in groups}
                if group_map:
                    break
            except Exception:
                continue
        for j in missing:
            name = group_map.get(j.group_id)
            if name:
                j.group_name = name
        await db.commit()

    pending_triggers = [j for j in jobs if j.trigger_type == "trigger" and j.status == "pending"]
    if pending_triggers:
        for j in pending_triggers:
            j.status = "trigger"
        await db.commit()

    return request.app.state.render(request, "jobs/list.html",
                                     jobs=jobs, current_status=status, q=q,
                                     sort_by=sort_by, sort_order=sort_order,
                                     page=page, total_pages=total_pages, total=total)


@router.get("/create")
async def create_job_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])
    user_tz = user.get("tz", "UTC")

    result = await db.execute(select(Token).where(Token.user_id == user_id, Token.is_active == True))
    tokens = result.scalars().all()

    groups: list[dict] = []
    for t in tokens:
        try:
            sender = WhatsAppSender(api_token=decrypt_token(t.api_token))
            groups = await sender.get_groups()
            if groups:
                break
        except Exception:
            continue

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return request.app.state.render(request, "jobs/form.html", tokens=tokens, groups=groups, now=now, user_tz=user_tz)


def build_trigger_value(trigger_type: str, user_tz: str, **kw) -> str:
    if trigger_type == "now":
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    elif trigger_type == "trigger":
        return ""
    elif trigger_type == "date":
        tv = kw.get("trigger_value_date", "").replace("T", " ")
        return local_to_utc(tv, user_tz)
    elif trigger_type == "cron":
        cron_time = kw.get("cron_time", "09:00")
        hour, minute = cron_time.split(":")
        cron_freq = kw.get("cron_freq", "daily")
        if cron_freq == "daily":
            return f"{minute} {hour} * * *"
        elif cron_freq == "weekdays":
            return f"{minute} {hour} * * 1-5"
        elif cron_freq == "custom":
            days = kw.get("cron_days", [])
            return f"{minute} {hour} * * {','.join(sorted(days, key=int))}"
        elif cron_freq == "monthly":
            return f"{minute} {hour} {kw.get('cron_dom', 1)} * *"
        elif cron_freq == "raw":
            return kw.get("cron_raw", "")
    return ""


@router.post("/create")
async def create_job(
    request: Request,
    token_id: int = Form(...),
    label: str = Form(default=""),
    group_id: str = Form(...),
    group_id_manual: str = Form(default=""),
    group_name: str = Form(default=""),
    message: str = Form(...),
    image: UploadFile | None = None,
    trigger_type: str = Form(...),
    trigger_value: str = Form(default=""),
    trigger_value_date: str = Form(default=""),
    cron_freq: str = Form(default="daily"),
    cron_time: str = Form(default="09:00"),
    cron_dom: int = Form(default=1),
    cron_days: list[str] = Form(default=[]),
    cron_raw: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])
    user_tz = user.get("tz", "UTC")

    if group_id == "__manual__":
        group_id = group_id_manual

    image_path = await save_upload(image) if image else None

    trigger_value = build_trigger_value(
        trigger_type, user_tz,
        trigger_value_date=trigger_value_date,
        cron_time=cron_time,
        cron_freq=cron_freq,
        cron_dom=cron_dom,
        cron_days=cron_days,
        cron_raw=cron_raw,
    )

    job = Job(
        user_id=user_id,
        token_id=token_id,
        label=label or None,
        group_id=group_id,
        group_name=group_name or None,
        message=message,
        image_path=image_path,
        trigger_type=trigger_type,
        trigger_value=trigger_value,
        status="trigger" if trigger_type == "trigger" else "pending",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    await register_job(job)
    await db.commit()
    return redirect_with_flash("/jobs", success="Job created")


@router.post("/{job_id}/pause")
async def pause_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and job.status == "active":
        await remove_job(job_id)
        job.status = "paused"
        await db.commit()
    return redirect_with_flash("/jobs", success="Job paused")


@router.post("/{job_id}/resume")
async def resume_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and job.status == "paused":
        job.status = "pending"
        await db.commit()
        await register_job(job)
        await db.commit()
    return redirect_with_flash("/jobs", success="Job resumed")


@router.post("/{job_id}/skip")
async def skip_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and job.status in ("pending", "active"):
        job.skip_count += 1
        await db.commit()
    return redirect_with_flash("/jobs", success="Job skipped")


@router.post("/{job_id}/skip-clear")
async def skip_clear_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and job.skip_count > 0:
        job.skip_count = 0
        await db.commit()
    return redirect_with_flash("/jobs", success="Skips cleared")


@router.post("/{job_id}/send-now")
async def send_now_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and job.status in ("trigger",):
        await send_job(job_id)
    return redirect_with_flash("/jobs", success="Job sent")


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and job.status in ("pending", "active", "trigger"):
        await remove_job(job_id)
        job.status = "cancelled"
        await db.commit()
    return redirect_with_flash("/jobs", success="Job cancelled")


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
    return redirect_with_flash("/jobs", success="Job deleted")


@router.post("/{job_id}/clone")
async def clone_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if not job:
        return redirect_with_flash("/jobs", success="Job cloned")  # will just redirect with message

    clone = Job(
        user_id=job.user_id,
        token_id=job.token_id,
        label=(job.label + " (copy)") if job.label else None,
        group_id=job.group_id,
        group_name=job.group_name,
        message=job.message,
        image_path=job.image_path,
        trigger_type=job.trigger_type,
        trigger_value=job.trigger_value,
        status="trigger" if job.trigger_type == "trigger" else "pending",
    )
    db.add(clone)
    await db.commit()
    await db.refresh(clone)
    await register_job(clone)
    await db.commit()
    return redirect_with_flash("/jobs", success="Job cloned")


def parse_cron_for_form(expr: str) -> dict:
    parts = expr.split()
    if len(parts) != 5:
        return {}
    minute, hour, dom, month, dow = parts
    cron_time = f"{hour.zfill(2)}:{minute.zfill(2)}"
    if dow == "*" and dom == "*":
        return {"cron_freq": "daily", "cron_time": cron_time}
    if dow == "1-5" and dom == "*":
        return {"cron_freq": "weekdays", "cron_time": cron_time}
    if dom == "*" and dow != "*" and "," not in dow and "-" not in dow:
        return {"cron_freq": "custom", "cron_time": cron_time, "cron_days": [dow]}
    if dom == "*" and dow != "*" and "," in dow:
        return {"cron_freq": "custom", "cron_time": cron_time, "cron_days": dow.split(",")}
    if dom != "*" and dow == "*":
        return {"cron_freq": "monthly", "cron_time": cron_time, "cron_dom": int(dom)}
    return {"cron_freq": "raw", "cron_raw": expr}


@router.get("/{job_id}/edit")
async def edit_job_page(request: Request, job_id: int, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])
    user_tz = user.get("tz", "UTC")

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = await db.execute(select(Token).where(Token.user_id == user_id, Token.is_active == True))
    tokens = result.scalars().all()

    groups: list[dict] = []
    for t in tokens:
        try:
            sender = WhatsAppSender(api_token=decrypt_token(t.api_token))
            groups = await sender.get_groups()
            if groups:
                break
        except Exception:
            continue

    form_data = {}
    if job.trigger_type == "date":
        try:
            utc_dt = datetime.strptime(job.trigger_value, "%Y-%m-%d %H:%M").replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
            local_dt = utc_dt.astimezone(zoneinfo.ZoneInfo(user_tz))
            form_data["trigger_value_date"] = local_dt.strftime("%Y-%m-%dT%H:%M")
        except (ValueError, zoneinfo.ZoneInfoNotFoundError):
            form_data["trigger_value_date"] = ""
    elif job.trigger_type == "cron":
        form_data.update(parse_cron_for_form(job.trigger_value))

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    image_available = bool(job.image_path and Path(job.image_path).exists())
    return request.app.state.render(request, "jobs/form.html",
                                     job=job, tokens=tokens, groups=groups,
                                     now=now, user_tz=user_tz, edit_mode=True,
                                     image_available=image_available, **form_data)


@router.post("/{job_id}/edit")
async def edit_job(
    request: Request,
    job_id: int,
    token_id: int = Form(...),
    label: str = Form(default=""),
    group_id: str = Form(...),
    group_id_manual: str = Form(default=""),
    group_name: str = Form(default=""),
    message: str = Form(...),
    image: UploadFile | None = None,
    trigger_type: str = Form(...),
    trigger_value: str = Form(default=""),
    trigger_value_date: str = Form(default=""),
    cron_freq: str = Form(default="daily"),
    cron_time: str = Form(default="09:00"),
    cron_dom: int = Form(default=1),
    cron_days: list[str] = Form(default=[]),
    cron_raw: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])
    user_tz = user.get("tz", "UTC")

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if group_id == "__manual__":
        group_id = group_id_manual

    image_path = job.image_path
    if image and image.filename:
        new_path = await save_upload(image)
        if new_path:
            if job.image_path:
                p = Path(job.image_path)
                if p.exists():
                    p.unlink()
            image_path = new_path

    trigger_value = build_trigger_value(
        trigger_type, user_tz,
        trigger_value_date=trigger_value_date,
        cron_time=cron_time,
        cron_freq=cron_freq,
        cron_dom=cron_dom,
        cron_days=cron_days,
        cron_raw=cron_raw,
    )

    job.token_id = token_id
    job.label = label or None
    job.group_id = group_id
    job.group_name = group_name or None
    job.message = message
    job.image_path = image_path
    job.trigger_type = trigger_type
    job.trigger_value = trigger_value
    job.skip_count = 0
    await db.commit()

    await remove_job(job_id)
    await register_job(job)
    await db.commit()

    return redirect_with_flash("/jobs", success="Job updated")
