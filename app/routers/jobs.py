import os
import zoneinfo
import uuid
from datetime import datetime
from pathlib import Path

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func as sqlfunc, select
from sqlalchemy import asc, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.job import Job, JobGroup
from app.models.delivery_attempt import DeliveryAttempt
from app.models.log import Log
from app.models.token import Token
from app.routers.auth import require_user
from app.services.crypto import decrypt_token
from app.services.csrf import csrf_protect
from app.services.scheduler import register_job, register_retry, remove_job, send_job
from app.services.scheduler import scheduler as apscheduler
from app.services.sender import WhatsAppSender

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(csrf_protect)])

def redirect_with_flash(url: str, success: str = "") -> RedirectResponse:
    if success:
        url += ("&" if "?" in url else "?") + urlencode({"success": success})
    return RedirectResponse(url=url, status_code=303)

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(Path(__file__).resolve().parent.parent.parent / "uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
MEDIA_SIGNATURES = {
    ".jpg": ("image/jpeg", lambda h: h.startswith(b"\xff\xd8\xff")),
    ".png": ("image/png", lambda h: h.startswith(b"\x89PNG\r\n\x1a\n")),
    ".gif": ("image/gif", lambda h: h.startswith((b"GIF87a", b"GIF89a"))),
    ".webp": ("image/webp", lambda h: h.startswith(b"RIFF") and h[8:12] == b"WEBP"),
    ".mp4": ("video/mp4", lambda h: len(h) >= 12 and h[4:8] == b"ftyp"),
    ".mov": ("video/quicktime", lambda h: len(h) >= 12 and h[4:8] == b"ftyp"),
    ".avi": ("video/x-msvideo", lambda h: h.startswith(b"RIFF") and h[8:12] == b"AVI "),
    ".mkv": ("video/x-matroska", lambda h: h.startswith(b"\x1aE\xdf\xa3")),
    ".webm": ("video/webm", lambda h: h.startswith(b"\x1aE\xdf\xa3")),
}


def local_to_utc(date_str: str, tz_name: str = "UTC") -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
    tz_obj = zoneinfo.ZoneInfo(tz_name)
    aware = dt.replace(tzinfo=tz_obj)
    utc = aware.astimezone(zoneinfo.ZoneInfo("UTC"))
    if utc.astimezone(tz_obj).replace(tzinfo=None) != dt:
        raise ValueError("This local time does not exist because of a daylight-saving transition")
    return utc.strftime("%Y-%m-%d %H:%M")


async def save_upload(file: UploadFile) -> str | None:
    if not file.filename:
        return None

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")
    expected_mime, signature_matches = MEDIA_SIGNATURES[ext]
    allowed_content_types = {expected_mime, "application/octet-stream"}
    if file.content_type and file.content_type not in allowed_content_types:
        raise HTTPException(status_code=400, detail="The uploaded file type does not match its extension")

    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    total = 0
    header = b""
    try:
        with dest.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=400, detail="File exceeds 50MB limit")
                if len(header) < 32:
                    header += chunk[:32 - len(header)]
                output.write(chunk)
        if not signature_matches(header):
            raise HTTPException(status_code=400, detail="The uploaded file content is not a supported media format")
        return str(dest.resolve())
    except Exception:
        dest.unlink(missing_ok=True)
        raise


async def delete_media_if_unreferenced(db: AsyncSession, image_path: str, excluding_job_id: int) -> None:
    references = await db.scalar(
        select(sqlfunc.count(Job.id)).where(Job.image_path == image_path, Job.id != excluding_job_id)
    )
    if not references:
        Path(image_path).unlink(missing_ok=True)


ALLOWED_SORT_COLS = {"id", "label", "group_name", "trigger_type", "status", "created_at"}


async def require_owned_active_token(db: AsyncSession, user_id: int, token_id: int) -> Token:
    result = await db.execute(
        select(Token).where(Token.id == token_id, Token.user_id == user_id, Token.is_active.is_(True))
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=400, detail="Select an active token that belongs to your account")
    return token


def time_left_str(job: Job) -> str:
    if job.status not in ("pending", "active", "trigger", "retry_scheduled"):
        return ""
    if job.trigger_type in ("now", "trigger"):
        return ""
    scheduled = apscheduler.get_job(str(job.id)) or apscheduler.get_job(f"retry:{job.id}")
    if not scheduled or not scheduled.next_run_time:
        return ""
    diff = scheduled.next_run_time - datetime.now(scheduled.next_run_time.tzinfo)
    total_seconds = int(diff.total_seconds())
    if total_seconds < 0:
        return ""
    if total_seconds < 60:
        return "Now"
    minutes = total_seconds // 60
    hours = minutes // 60
    days = hours // 24
    if days > 0:
        return f"{days}d {hours % 24}h"
    if hours > 0:
        return f"{hours}h {minutes % 60}m"
    return f"{minutes}m"

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
        base_query = base_query.where(Job.status.in_(["pending", "active", "trigger", "retry_scheduled"]))
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

    result = await db.execute(query.options(selectinload(Job.job_groups)))
    jobs = result.scalars().all()

    missing_job_groups: list[JobGroup] = []
    for j in jobs:
        for jg in j.job_groups:
            if not jg.group_name:
                missing_job_groups.append(jg)
    if missing_job_groups:
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
        for jg in missing_job_groups:
            name = group_map.get(jg.group_id)
            if name:
                jg.group_name = name
        await db.commit()

    pending_triggers = [j for j in jobs if j.trigger_type == "trigger" and j.status == "pending"]
    if pending_triggers:
        for j in pending_triggers:
            j.status = "trigger"
        await db.commit()

    time_left_map = {job.id: time_left_str(job) for job in jobs}

    return request.app.state.render(request, "jobs/list.html",
                                     jobs=jobs, current_status=status, q=q,
                                     sort_by=sort_by, sort_order=sort_order,
                                     page=page, total_pages=total_pages, total=total,
                                     time_left_map=time_left_map)


@router.get("/create")
async def create_job_page(request: Request, type: str = "now", db: AsyncSession = Depends(get_db)):
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
    initial_trigger_type = type if type in {"now", "date", "cron", "trigger"} else "now"
    return request.app.state.render(request, "jobs/form.html", tokens=tokens, groups=groups, now=now, user_tz=user_tz, selected_groups=[], initial_trigger_type=initial_trigger_type)


def build_trigger_value(trigger_type: str, user_tz: str, **kw) -> str:
    if trigger_type not in {"now", "trigger", "date", "cron"}:
        raise ValueError("Unsupported trigger type")
    if trigger_type == "now":
        return datetime.now(zoneinfo.ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M")
    elif trigger_type == "trigger":
        return ""
    elif trigger_type == "date":
        tv = kw.get("trigger_value_date", "").replace("T", " ")
        if not tv:
            raise ValueError("Choose a date and time")
        return local_to_utc(tv, user_tz)
    elif trigger_type == "cron":
        cron_time = kw.get("cron_time", "09:00")
        try:
            hour, minute = cron_time.split(":")
            if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                raise ValueError
        except (AttributeError, ValueError):
            raise ValueError("Choose a valid recurring time")
        cron_freq = kw.get("cron_freq", "daily")
        if cron_freq == "daily":
            return f"{minute} {hour} * * *"
        elif cron_freq == "weekdays":
            return f"{minute} {hour} * * 0-4"
        elif cron_freq == "custom":
            days = kw.get("cron_days", [])
            if not days or any(str(day) not in {"0", "1", "2", "3", "4", "5", "6"} for day in days):
                raise ValueError("Select at least one valid weekday")
            fixed = sorted((int(d) + 6) % 7 for d in days)
            return f"{minute} {hour} * * {','.join(str(d) for d in fixed)}"
        elif cron_freq == "monthly":
            day_of_month = int(kw.get("cron_dom", 1))
            if not 1 <= day_of_month <= 31:
                raise ValueError("Day of month must be between 1 and 31")
            return f"{minute} {hour} {day_of_month} * *"
        elif cron_freq == "raw":
            raw = kw.get("cron_raw", "").strip()
            CronTrigger.from_crontab(raw, timezone=user_tz)
            return raw
        raise ValueError("Unsupported recurring frequency")
    raise ValueError("Unsupported trigger type")


@router.post("/create")
async def create_job(
    request: Request,
    token_id: int = Form(...),
    label: str = Form(default=""),
    group_ids: list[str] = Form(default=[]),
    group_names: list[str] = Form(default=[]),
    group_id_manual: str = Form(default=""),
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

    await require_owned_active_token(db, user_id, token_id)

    if group_id_manual:
        group_ids.append(group_id_manual)
        group_names.append("")

    if not group_ids:
        return redirect_with_flash("/jobs/create", success="Please select at least one group.")

    if trigger_type == "cron" and cron_freq == "custom" and not cron_days:
        return redirect_with_flash("/jobs/create", success="Select at least one day.")

    try:
        trigger_value = build_trigger_value(
            trigger_type, user_tz,
            trigger_value_date=trigger_value_date,
            cron_time=cron_time,
            cron_freq=cron_freq,
            cron_dom=cron_dom,
            cron_days=cron_days,
            cron_raw=cron_raw,
        )
    except (ValueError, zoneinfo.ZoneInfoNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    image_path = await save_upload(image) if image else None

    job = Job(
        user_id=user_id,
        token_id=token_id,
        label=label or None,
        group_id=group_ids[0] if group_ids else "",
        group_name=group_names[0] if group_names else None,
        message=message,
        image_path=image_path,
        trigger_type=trigger_type,
        trigger_value=trigger_value,
        schedule_timezone=user_tz,
        status="trigger" if trigger_type == "trigger" else "pending",
    )
    try:
        db.add(job)
        await db.flush()
        for i, gid in enumerate(group_ids):
            gname = group_names[i] if i < len(group_names) else None
            db.add(JobGroup(job_id=job.id, group_id=gid, group_name=gname or None))
        await register_job(job)
        await db.commit()
    except Exception:
        await db.rollback()
        if image_path:
            Path(image_path).unlink(missing_ok=True)
        raise
    return redirect_with_flash("/jobs", success="Job created")


@router.post("/{job_id}/pause")
async def pause_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and job.status in ("active", "retry_scheduled"):
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
        if job.retry_at and job.trigger_type in {"date", "now"}:
            job.status = "retry_scheduled"
            await register_retry(job)
        else:
            job.status = "pending"
            await register_job(job)
            if job.retry_at:
                await register_retry(job)
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
        await remove_job(job_id)
        job.retry_count = 0
        job.retry_at = None
        job.retry_group_ids = None
        await db.commit()
        await send_job(job_id)
    return redirect_with_flash("/jobs", success="Job sent")


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and job.status in ("pending", "active", "trigger", "retry_scheduled"):
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
            await delete_media_if_unreferenced(db, job.image_path, job.id)
        await remove_job(job_id)
        await db.delete(job)
        await db.commit()
    return redirect_with_flash("/jobs", success="Job deleted")


@router.post("/{job_id}/clone")
async def clone_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id).options(selectinload(Job.job_groups)))
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
        schedule_timezone=job.schedule_timezone,
        status="trigger" if job.trigger_type == "trigger" else "pending",
    )
    db.add(clone)
    await db.commit()
    await db.refresh(clone)
    for jg in job.job_groups:
        db.add(JobGroup(job_id=clone.id, group_id=jg.group_id, group_name=jg.group_name))
    await db.commit()
    await register_job(clone)
    await db.commit()
    return redirect_with_flash("/jobs", success="Job cloned")


def parse_cron_for_form(expr: str, tz_name: str = "UTC") -> dict:
    parts = expr.split()
    if len(parts) != 5:
        return {}
    minute, hour, dom, month, dow = parts
    cron_time = f"{hour.zfill(2)}:{minute.zfill(2)}"
    if dow == "*" and dom == "*":
        return {"cron_freq": "daily", "cron_time": cron_time}
    if dow == "0-4" and dom == "*":
        return {"cron_freq": "weekdays", "cron_time": cron_time}
    if dom == "*" and "," in dow:
        days = [str((int(d) + 1) % 7) for d in dow.split(",")]
        return {"cron_freq": "custom", "cron_time": cron_time, "cron_days": days}
    if dom == "*" and dow != "*" and "-" not in dow:
        return {"cron_freq": "custom", "cron_time": cron_time, "cron_days": [str((int(dow) + 1) % 7)]}
    if dom != "*" and dow == "*":
        return {"cron_freq": "monthly", "cron_time": cron_time, "cron_dom": int(dom)}
    return {"cron_freq": "raw", "cron_raw": expr}


@router.get("/{job_id}")
async def job_detail(request: Request, job_id: int, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])
    user_tz = user.get("tz", "UTC")

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id).options(selectinload(Job.job_groups)))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    scheduled = apscheduler.get_job(str(job.id)) or apscheduler.get_job(f"retry:{job.id}")
    next_run = ""
    if scheduled and scheduled.next_run_time:
        diff = scheduled.next_run_time - datetime.now(scheduled.next_run_time.tzinfo)
        total_seconds = int(diff.total_seconds())
        if total_seconds >= 0:
            if total_seconds < 60:
                next_run = "Now"
            else:
                minutes = total_seconds // 60
                hours = minutes // 60
                days = hours // 24
                if days > 0:
                    next_run = f"{days}d {hours % 24}h"
                elif hours > 0:
                    next_run = f"{hours}h {minutes % 60}m"
                else:
                    next_run = f"{minutes}m"

    log_result = await db.execute(
        select(Log).where(Log.job_id == job_id).order_by(Log.sent_at.desc()).limit(20)
    )
    logs = log_result.scalars().all()

    attempt_result = await db.execute(
        select(DeliveryAttempt)
        .where(DeliveryAttempt.job_id == job_id)
        .order_by(DeliveryAttempt.created_at.desc())
        .limit(50)
    )
    delivery_attempts = attempt_result.scalars().all()

    media_available = bool(job.image_path and Path(job.image_path).exists())
    media_filename = Path(job.image_path).name if media_available else ""
    media_is_video = bool(media_available and Path(job.image_path).suffix.lower() in VIDEO_EXTENSIONS)

    return request.app.state.render(request, "jobs/detail.html",
                                     job=job, logs=logs, delivery_attempts=delivery_attempts, next_run=next_run,
                                     media_available=media_available, media_filename=media_filename,
                                     media_is_video=media_is_video)


@router.get("/{job_id}/edit")
async def edit_job_page(request: Request, job_id: int, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])
    user_tz = user.get("tz", "UTC")

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id).options(selectinload(Job.job_groups)))
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
        form_data.update(parse_cron_for_form(job.trigger_value, user_tz))

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    image_available = bool(job.image_path and Path(job.image_path).exists())
    selected_groups = [{"id": jg.group_id, "name": jg.group_name or jg.group_id} for jg in job.job_groups]
    if not selected_groups and job.group_id:
        selected_groups = [{"id": job.group_id, "name": job.group_name or job.group_id}]
    return request.app.state.render(request, "jobs/form.html",
                                     job=job, tokens=tokens, groups=groups,
                                     now=now, user_tz=user_tz, edit_mode=True,
                                     image_available=image_available,
                                     selected_groups=selected_groups, **form_data)


@router.post("/{job_id}/edit")
async def edit_job(
    request: Request,
    job_id: int,
    token_id: int = Form(...),
    label: str = Form(default=""),
    group_ids: list[str] = Form(default=[]),
    group_names: list[str] = Form(default=[]),
    group_id_manual: str = Form(default=""),
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

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id).options(selectinload(Job.job_groups)))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    await require_owned_active_token(db, user_id, token_id)

    if group_id_manual:
        group_ids.append(group_id_manual)
        group_names.append("")

    if not group_ids:
        return redirect_with_flash(f"/jobs/{job_id}/edit", success="Please select at least one group.")

    if trigger_type == "cron" and cron_freq == "custom" and not cron_days:
        return redirect_with_flash(f"/jobs/{job_id}/edit", success="Select at least one day.")

    image_path = job.image_path
    if image and image.filename:
        new_path = await save_upload(image)
        if new_path:
            if job.image_path:
                await delete_media_if_unreferenced(db, job.image_path, job.id)
            image_path = new_path

    try:
        trigger_value = build_trigger_value(
            trigger_type, user_tz,
            trigger_value_date=trigger_value_date,
            cron_time=cron_time,
            cron_freq=cron_freq,
            cron_dom=cron_dom,
            cron_days=cron_days,
            cron_raw=cron_raw,
        )
    except (ValueError, zoneinfo.ZoneInfoNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job.token_id = token_id
    job.label = label or None
    job.group_id = group_ids[0] if group_ids else ""
    job.group_name = group_names[0] if group_names else None
    job.message = message
    job.image_path = image_path
    job.trigger_type = trigger_type
    job.trigger_value = trigger_value
    job.schedule_timezone = user_tz
    job.status = "trigger" if trigger_type == "trigger" else "pending"
    job.skip_count = 0
    job.retry_count = 0
    job.retry_at = None
    job.retry_group_ids = None

    for old in job.job_groups:
        await db.delete(old)
    for i, gid in enumerate(group_ids):
        gname = group_names[i] if i < len(group_names) else None
        db.add(JobGroup(job_id=job.id, group_id=gid, group_name=gname or None))
    await remove_job(job_id)
    await register_job(job)
    await db.commit()

    return redirect_with_flash("/jobs", success="Job updated")
