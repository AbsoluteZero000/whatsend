import os
import asyncio
import json
import re
import zoneinfo
import uuid
import httpx
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path

from urllib.parse import urlencode

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func as sqlfunc, select, update
from sqlalchemy import asc, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.job import Job, JobGroup
from app.models.job_series import SeriesEntry, SeriesAttachment
from app.services.series_forms import validate_plan, persist_plan, PageRange
from app.services.message_variables import ARABIC_ORDINAL_WORDS
from app.services.series_imports import import_playlist, split_pdf, MAX_PDF_BYTES, PDF_SPLIT_LOCK, delete_unreferenced_path
from app.models.delivery_attempt import DeliveryAttempt
from app.models.log import Log
from app.models.recipient_list import RecipientList
from app.models.token import Token
from app.routers.auth import require_user
from app.services.crypto import decrypt_token
from app.services.csrf import csrf_protect
from app.services.scheduler import build_trigger, register_job, register_retry, remove_job, send_job
from app.services.scheduler import scheduler as apscheduler
from app.services.sender import WhatsAppSender, get_sender
from app.services.native_sender import CONNECTION_LOST, NativeSender
from app.i18n import _
from app.services.native_api import connection_base_url

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
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".csv", ".rtf", ".odt", ".ods", ".odp", ".zip",
}
ALLOWED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | DOCUMENT_EXTENSIONS
_is_zip = lambda h: h.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
_is_ole = lambda h: h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
_is_text = lambda h: b"\x00" not in h
MEDIA_SIGNATURES = {
    ".jpg": ("image/jpeg", lambda h: h.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", lambda h: h.startswith(b"\xff\xd8\xff")),
    ".png": ("image/png", lambda h: h.startswith(b"\x89PNG\r\n\x1a\n")),
    ".gif": ("image/gif", lambda h: h.startswith((b"GIF87a", b"GIF89a"))),
    ".webp": ("image/webp", lambda h: h.startswith(b"RIFF") and h[8:12] == b"WEBP"),
    ".mp4": ("video/mp4", lambda h: len(h) >= 12 and h[4:8] == b"ftyp"),
    ".mov": ("video/quicktime", lambda h: len(h) >= 12 and h[4:8] == b"ftyp"),
    ".avi": ("video/x-msvideo", lambda h: h.startswith(b"RIFF") and h[8:12] == b"AVI "),
    ".mkv": ("video/x-matroska", lambda h: h.startswith(b"\x1aE\xdf\xa3")),
    ".webm": ("video/webm", lambda h: h.startswith(b"\x1aE\xdf\xa3")),
    ".pdf": ("application/pdf", lambda h: h.startswith(b"%PDF-")),
    ".doc": ("application/msword", _is_ole),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", _is_zip),
    ".xls": ("application/vnd.ms-excel", _is_ole),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _is_zip),
    ".ppt": ("application/vnd.ms-powerpoint", _is_ole),
    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", _is_zip),
    ".txt": ("text/plain", _is_text),
    ".csv": ("text/csv", _is_text),
    ".rtf": ("application/rtf", lambda h: h.lstrip().startswith(b"{\\rtf")),
    ".odt": ("application/vnd.oasis.opendocument.text", _is_zip),
    ".ods": ("application/vnd.oasis.opendocument.spreadsheet", _is_zip),
    ".odp": ("application/vnd.oasis.opendocument.presentation", _is_zip),
    ".zip": ("application/zip", _is_zip),
}

CONTENT_TYPE_ALIASES = {
    ".docx": {"application/zip"},
    ".xlsx": {"application/zip"},
    ".pptx": {"application/zip"},
    ".odt": {"application/zip"},
    ".ods": {"application/zip"},
    ".odp": {"application/zip"},
    ".txt": {"text/csv"},
    ".csv": {"text/plain", "application/vnd.ms-excel"},
    ".rtf": {"text/rtf"},
    ".zip": {"application/x-zip-compressed"},
}


def local_to_utc(date_str: str, tz_name: str = "UTC") -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
    tz_obj = zoneinfo.ZoneInfo(tz_name)
    aware = dt.replace(tzinfo=tz_obj)
    utc = aware.astimezone(zoneinfo.ZoneInfo("UTC"))
    if utc.astimezone(tz_obj).replace(tzinfo=None) != dt:
        raise ValueError("This local time does not exist because of a daylight-saving transition")
    return utc.strftime("%Y-%m-%d %H:%M")


async def save_upload(file: UploadFile, max_size: int = MAX_UPLOAD_SIZE) -> str | None:
    if not file.filename:
        return None
    if len(Path(file.filename.replace("\\", "/")).name.encode("utf-8")) > 255:
        raise HTTPException(400, "Attachment filename exceeds 255 UTF-8 bytes")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")
    expected_mime, signature_matches = MEDIA_SIGNATURES[ext]
    allowed_content_types = {expected_mime, "application/octet-stream"} | CONTENT_TYPE_ALIASES.get(ext, set())
    if file.content_type and file.content_type not in allowed_content_types:
        raise HTTPException(status_code=400, detail="The uploaded file type does not match its extension")

    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    total = 0
    header = b""
    try:
        with dest.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_size:
                    raise HTTPException(status_code=400, detail=f"File exceeds {max_size // (1024 * 1024)}MB limit")
                if len(header) < 32:
                    header += chunk[:32 - len(header)]
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty. Choose the file again and retry.")
        if not signature_matches(header):
            raise HTTPException(status_code=400, detail=f"The uploaded file content does not match {ext}. Re-export the file in that format and upload it again.")
        return str(dest.resolve())
    except Exception:
        dest.unlink(missing_ok=True)
        raise


async def job_error_form(request, db, user, error, group_ids, group_names, job=None):
    form = await request.form()
    result = await db.execute(select(Token).where(Token.user_id == int(user["sub"]), Token.is_active.is_(True)))
    draft = SimpleNamespace(id=job.id if job else None, token_id=int(form["token_id"]),
                            label=form.get("label", ""), message=form.get("message", ""),
                            trigger_type=form.get("trigger_type", "now"), image_path=job.image_path if job else None,
                            image_filename=job.image_filename if job else None,
                            message_mode=form.get('message_mode', 'repeat'), series_cursor=job.series_cursor if job else 0,
                            series_plan=form.get('series_definition', ''))
    response = request.app.state.render(
        request, "jobs/form.html", job=draft, edit_mode=job is not None, form_error=str(error.detail), arabic_ordinals=ARABIC_ORDINAL_WORDS,
        draft_owner=user["sub"], tokens=result.scalars().all(), groups=[], user_tz=user.get("tz", "UTC"),
        selected_groups=[{"id": gid, "name": (group_names[i] or gid) if i < len(group_names) else gid}
                         for i, gid in enumerate(group_ids)],
        initial_trigger_type=draft.trigger_type, trigger_value_date=form.get("trigger_value_date", ""),
        cron_freq=form.get("cron_freq", "daily"), cron_time=form.get("cron_time", "09:00"),
        cron_dom=form.get("cron_dom", "1"), cron_days=form.getlist("cron_days"), cron_raw=form.get("cron_raw", ""),
        image_available=bool(draft.image_path and Path(draft.image_path).exists()),
    )
    response.status_code = error.status_code
    return response


async def delete_media_if_unreferenced(db: AsyncSession, image_path: str, excluding_job_id: int) -> None:
    references = await db.scalar(
        select(sqlfunc.count(Job.id)).where(Job.image_path == image_path, Job.id != excluding_job_id)
    )
    references += await db.scalar(select(sqlfunc.count()).select_from(SeriesAttachment).where(SeriesAttachment.path == image_path)) or 0
    if not references:
        Path(image_path).unlink(missing_ok=True)


ALLOWED_SORT_COLS = {"id", "label", "group_name", "trigger_type", "status", "created_at"}


def job_sender(token):
    if token.provider == "whatsapp":
        return WhatsAppSender(api_token=decrypt_token(token.api_token))
    return get_sender(token.provider, "", session_id=token.relay_session_id,
                      base_url=connection_base_url(token))


def validate_job_payload(token, recipients, message, image_path):
    if token.provider != "native":
        return
    try:
        for recipient in recipients:
            if image_path:
                NativeSender.validate_media(recipient, image_path, message)
            else:
                NativeSender.validate_text(recipient, message)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/groups")
async def token_groups(request: Request, token_id: int, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    token = await require_owned_active_token(db, int(user["sub"]), token_id)
    try:
        groups = await job_sender(token).get_groups()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, "Could not load groups. Check that this token's WhatsApp session is connected.") from exc
    return JSONResponse({"groups": groups})


@router.post("/series-files")
async def upload_series_file(request: Request, file: UploadFile, db: AsyncSession = Depends(get_db)):
    user_id = int(require_user(request)['sub'])
    path = await save_upload(file)
    if not path: raise HTTPException(400, 'Choose an attachment')
    attachment = SeriesAttachment(id=str(uuid.uuid4()), user_id=user_id, path=path,
                                  filename=Path(file.filename.replace('\\', '/')).name)
    db.add(attachment)
    try: await db.commit()
    except Exception:
        Path(path).unlink(missing_ok=True); raise
    return JSONResponse({'id': attachment.id, 'filename': attachment.filename}, status_code=201)


@router.get("/series-files/{attachment_id}")
async def read_series_file(request: Request, attachment_id: str, db: AsyncSession = Depends(get_db)):
    user_id = int(require_user(request)['sub'])
    asset = await db.scalar(select(SeriesAttachment).where(SeriesAttachment.id == attachment_id, SeriesAttachment.user_id == user_id))
    if not asset or not Path(asset.path).is_file(): raise HTTPException(404, 'Attachment not found')
    media_type = MEDIA_SIGNATURES.get(Path(asset.path).suffix.lower(), ('application/octet-stream', None))[0]
    return FileResponse(asset.path, media_type=media_type, filename=asset.filename, content_disposition_type='inline', headers={'Cache-Control': 'private, max-age=300'})


@router.post("/series-pdf")
async def upload_series_pdf(request: Request, file: UploadFile, start_page: int = Form(1),
                            end_page: int | None = Form(None), pages_per_send: int = Form(5),
                            range_mode: str = Form('chunks'), custom_ranges: str = Form('[]'),
                            db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user['sub'])
    arabic = request.cookies.get('lang', user.get('lang', 'en')) == 'ar'
    if Path(file.filename or '').suffix.lower() != '.pdf': raise HTTPException(400, 'Choose a PDF file')
    if range_mode not in {'chunks', 'custom'}:
        raise HTTPException(400, 'Choose automatic chunks or custom page ranges')
    ranges = None
    if range_mode == 'custom':
        try:
            if len(custom_ranges.encode('utf-8')) > 100000: raise ValueError()
            parsed = TypeAdapter(list[PageRange]).validate_json(custom_ranges)
            if not 1 <= len(parsed) <= 1000: raise ValueError()
            ranges = [selection.model_dump() for selection in parsed]
        except (ValidationError, ValueError):
            raise HTTPException(400, 'Add 1–1000 custom ranges with whole-number start and end pages greater than zero')
    source = await save_upload(file, max_size=MAX_PDF_BYTES)
    outputs = []
    try:
        async with PDF_SPLIT_LOCK:
            outputs = await asyncio.to_thread(split_pdf, source, UPLOAD_DIR, file.filename, start_page, end_page, pages_per_send, ranges)
        entries = []
        for part in outputs:
            asset = SeriesAttachment(id=str(uuid.uuid4()), user_id=user_id, path=part['path'], filename=part['filename'])
            db.add(asset)
            text = f"من صفحة {part['first']} إلى صفحة {part['last']}" if arabic else f"Pages {part['first']}-{part['last']}"
            entries.append({'message': text, 'start_page': part['first'], 'end_page': part['last'],
                            'attachments': [{'id': asset.id, 'filename': asset.filename}]})
        await db.commit()
        return JSONResponse({'entries': entries, 'title': Path(file.filename).stem[:120],
                             'start_page': outputs[0]['first'], 'end_page': outputs[-1]['last'],
                             'range_mode': range_mode, 'ranges': ranges or [], 'pages_per_send': pages_per_send}, status_code=201)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        for part in outputs: Path(part['path']).unlink(missing_ok=True)
        raise
    finally:
        if source: Path(source).unlink(missing_ok=True)


class PlaylistImport(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    items_per_send: int = Field(default=2, ge=1, le=20)


@router.post("/series-playlist")
async def upload_series_playlist(request: Request, payload: PlaylistImport):
    require_user(request)
    try: data = await import_playlist(payload.url, payload.items_per_send)
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc
    return JSONResponse(data)


class SavedRecipient(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)


class SavedRecipientList(BaseModel):
    token_id: int
    name: str = Field(min_length=1, max_length=80)
    recipients: list[SavedRecipient] = Field(min_length=1, max_length=1000)


@router.get("/recipient-lists")
async def recipient_lists(request: Request, token_id: int, db: AsyncSession = Depends(get_db)):
    user_id = int(require_user(request)["sub"])
    await require_owned_active_token(db, user_id, token_id)
    rows = (await db.execute(select(RecipientList).where(
        RecipientList.user_id == user_id, RecipientList.token_id == token_id,
    ).order_by(RecipientList.name, RecipientList.id))).scalars().all()
    return JSONResponse({"lists": [{"id": row.id, "name": row.name, "recipients": json.loads(row.recipients)}
                                   for row in rows]}, headers={"Cache-Control": "no-store, private"})


@router.post("/recipient-lists")
async def save_recipient_list(request: Request, payload: SavedRecipientList, db: AsyncSession = Depends(get_db)):
    user_id = int(require_user(request)["sub"])
    token = await require_owned_active_token(db, user_id, payload.token_id)
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Enter a list name")
    recipients = {}
    for recipient in payload.recipients:
        identifier = recipient.id.strip()
        if not identifier:
            raise HTTPException(400, "Enter a valid recipient")
        if token.provider == "native":
            # Phone numbers and group/LID IDs supported by the built-in worker.
            if not re.fullmatch(r"(?:\+?[0-9]{7,15}|[0-9]+(?:-[0-9]+)?@g\.us|[0-9]+@(?:s\.whatsapp\.net|lid))", identifier):
                raise HTTPException(400, "Enter a phone number or WhatsApp group ID")
        recipients[identifier] = {"id": identifier, "name": recipient.name.strip() or identifier}
    row = RecipientList(user_id=user_id, token_id=token.id, name=name,
                        recipients=json.dumps(list(recipients.values()), ensure_ascii=False))
    db.add(row)
    await db.commit()
    return JSONResponse({"id": row.id, "name": row.name, "recipients": list(recipients.values())}, status_code=201)


@router.delete("/recipient-lists/{list_id}")
async def delete_recipient_list(request: Request, list_id: int, db: AsyncSession = Depends(get_db)):
    user_id = int(require_user(request)["sub"])
    row = (await db.execute(select(RecipientList).where(
        RecipientList.id == list_id, RecipientList.user_id == user_id,
    ))).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Recipient list not found")
    await db.delete(row)
    await db.commit()
    return JSONResponse({"deleted": True})


@router.get("/{job_id}/progress")
async def job_progress(request: Request, job_id: int, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])
    lang = request.cookies.get('lang', user.get('lang', 'en'))
    job = (await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))).scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")
    run_id = await db.scalar(select(DeliveryAttempt.run_id).where(DeliveryAttempt.job_id == job_id)
                             .order_by(DeliveryAttempt.id.desc()).limit(1))
    attempt_query = select(DeliveryAttempt).where(DeliveryAttempt.job_id == job_id)
    if job.message_mode != 'series':
        attempt_query = attempt_query.where(DeliveryAttempt.run_id == run_id)
    attempts = (await db.execute(attempt_query.order_by(DeliveryAttempt.id))).scalars().all()
    entries = (await db.execute(select(SeriesEntry).where(SeriesEntry.job_id == job.id)
                               .order_by(SeriesEntry.position))).scalars().all() if job.message_mode == 'series' else []
    positions = {entry.id: entry.position for entry in entries}
    counts = {"sent": 0, "failed": 0, "unknown": 0, "pending": 0}
    recipients = []
    grouped = {}
    for attempt in attempts:
        key = (attempt.run_id, attempt.group_id) if job.message_mode == 'series' else attempt.id
        grouped.setdefault(key, []).append(attempt)
    for parts in grouped.values():
        states = {part.status for part in parts}
        state = 'pending' if states & {'pending', 'submitting', 'queued', 'processing'} else (
            'unknown' if 'unknown' in states else 'failed' if 'failed' in states else 'sent')
        counts[state] += 1
        errors = []
        for attempt in parts:
            if attempt.status not in {'failed', 'unknown'} or not attempt.response: continue
            try:
                data = json.loads(attempt.response)
                error = (data.get('error') or '') if isinstance(data, dict) else ''
                if isinstance(error, dict): error = error.get('message', '')
            except ValueError: error = attempt.response
            if isinstance(error, str) and error:
                if error in {CONNECTION_LOST, 'WhatsApp rejected this part (409).', 'WhatsApp rejected the request (409).'}:
                    # Legacy native 409 failures were classified as failed only for not_connected.
                    note = _(CONNECTION_LOST, lang)
                    if note not in errors: errors.append(note)
                else:
                    errors.append((attempt.part_label + ': ' if attempt.part_label else '') + error)
        recipients.append({'id': parts[0].group_id, 'name': parts[0].group_name or parts[0].group_id,
                           'status': state if job.message_mode == 'series' else parts[0].status, 'error': '\n'.join(errors),
                           'send_number': positions.get(parts[0].series_entry_id)})
    series = None
    if job.message_mode == 'series':
        series = {'cursor': job.series_cursor, 'total': len(entries), 'active': bool(job.series_active_run),
                  'entries': [{'position': entry.position, 'status': entry.status} for entry in entries]}
    return JSONResponse({'job_status': job.status, 'trigger_type': job.trigger_type,
                         'run_id': run_id, 'total': len(recipients), 'counts': counts, 'recipients': recipients,
                         'series': series}, headers={'Cache-Control': 'no-store, private'})


async def require_owned_active_token(db: AsyncSession, user_id: int, token_id: int) -> Token:
    result = await db.execute(
        select(Token).where(Token.id == token_id, Token.user_id == user_id, Token.is_active.is_(True))
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=400, detail="Select an active token that belongs to your account")
    if token.provider == "native" and not token.relay_session_id:
        raise HTTPException(400, "Finish setting up your WhatsApp session before creating a job")
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
        base_query = base_query.where(Job.status.in_(["pending", "active", "trigger", "retry_scheduled", "queued", "processing"]))
    elif status == "completed":
        base_query = base_query.where(Job.status.in_(["completed", "cancelled"]))
    elif status == "paused":
        base_query = base_query.where(Job.status == "paused")
    elif status == "failed":
        base_query = base_query.where(Job.status.in_(["failed", "unknown"]))
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
                sender = job_sender(t)
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

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    initial_trigger_type = type if type in {"now", "date", "cron", "trigger"} else "now"
    return request.app.state.render(request, "jobs/form.html", draft_owner=user["sub"], tokens=tokens, groups=groups, now=now, user_tz=user_tz, selected_groups=[], initial_trigger_type=initial_trigger_type, arabic_ordinals=ARABIC_ORDINAL_WORDS)


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
    message: str = Form(default=""),
    message_mode: str = Form(default="repeat"),
    series_definition: str = Form(default=""),
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

    token = await require_owned_active_token(db, user_id, token_id)

    if group_id_manual:
        group_ids.append(group_id_manual)
        group_names.append("")

    if not group_ids:
        return redirect_with_flash("/jobs/create", success="Please select at least one group.")

    if trigger_type == "cron" and cron_freq == "custom" and not cron_days:
        return redirect_with_flash("/jobs/create", success="Select at least one day.")


    plan_data = None
    try:
        if message_mode not in {'repeat', 'series'}: raise ValueError('Choose a valid message mode')
        if message_mode == 'series':
            if trigger_type not in {'cron', 'trigger'}:
                raise ValueError('Message series need a recurring schedule or an external trigger')
            plan_data = await validate_plan(db, series_definition, user_id, token, group_ids, message, None)
        elif not message:
            raise ValueError('Enter a message')
    except (ValueError, HTTPException) as exc:
        error = exc if isinstance(exc, HTTPException) else HTTPException(400, str(exc))
        return await job_error_form(request, db, user, error, group_ids, group_names, None)
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
        return await job_error_form(request, db, user, HTTPException(400, str(exc)), group_ids, group_names)

    try:
        image_path = await save_upload(image) if image and message_mode == "repeat" else None
    except HTTPException as exc:
        return await job_error_form(request, db, user, exc, group_ids, group_names)
    try:
        if message_mode == "repeat": validate_job_payload(token, group_ids, message, image_path)
    except HTTPException as exc:
        if image_path:
            Path(image_path).unlink(missing_ok=True)
        return await job_error_form(request, db, user, exc, group_ids, group_names)

    job = Job(
        user_id=user_id,
        token_id=token_id,
        label=label or None,
        group_id=group_ids[0] if group_ids else "",
        group_name=group_names[0] if group_names else None,
        message=message,
        image_path=image_path,
        image_filename=Path(image.filename.replace("\\", "/")).name if image_path and image else None,
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
        if plan_data: await persist_plan(db, job, *plan_data)
        await register_job(job)
        await db.commit()
    except Exception:
        await db.rollback()
        if image_path:
            Path(image_path).unlink(missing_ok=True)
        raise
    return redirect_with_flash(f"/jobs/{job.id}" if trigger_type == "now" else "/jobs", success="Job created")


@router.post("/{job_id}/pause")
async def pause_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and (job.status in ("active", "retry_scheduled") or (job.message_mode == "series" and job.status == "queued")):
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
            job.status = "trigger" if job.trigger_type == "trigger" else "pending"
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
    if job and job.message_mode == 'series':
        if job.status not in {'pending', 'active', 'trigger'} or job.series_active_run or job.series_cursor >= job.series_total:
            return redirect_with_flash(f"/jobs/{job_id}", success="This plan is not ready to send. Check its delivery status or resume it first.")
        await send_job(job_id, send_now=True)
    elif job and job.status in ("trigger",):
        await remove_job(job_id)
        job.retry_count = 0
        job.retry_at = None
        job.retry_group_ids = None
        await db.commit()
        await send_job(job_id)
    return redirect_with_flash(f"/jobs/{job_id}", success="Job submitted. Check delivery status in the job details.")


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id))
    job = result.scalar_one_or_none()
    if job and (job.status in ("pending", "active", "trigger", "retry_scheduled") or (job.message_mode == "series" and job.status == "queued")):
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
        paths = (await db.execute(select(SeriesAttachment.path).join(SeriesEntry).where(SeriesEntry.job_id == job.id))).scalars().all()
        paths = set(paths) | ({job.image_path} if job.image_path else set())
        await remove_job(job_id)
        await db.delete(job)
        await db.commit()
        for path in paths: await delete_unreferenced_path(db, path)
    return redirect_with_flash("/jobs", success="Job deleted")


@router.post("/{job_id}/clone")
async def clone_job(job_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id).options(selectinload(Job.job_groups), selectinload(Job.series_entries).selectinload(SeriesEntry.attachments)))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(404, "Job not found")

    clone = Job(
        user_id=job.user_id,
        token_id=job.token_id,
        label=(job.label[:113] + " (copy)") if job.label else None,
        group_id=job.group_id,
        group_name=job.group_name,
        message=job.message,
        message_mode=job.message_mode,
        series_plan=job.series_plan,
        series_total=len(job.series_entries) if job.message_mode == 'series' else 0,
        image_path=job.image_path,
        image_filename=job.image_filename,
        trigger_type=job.trigger_type,
        trigger_value=job.trigger_value,
        schedule_timezone=job.schedule_timezone,
        status="trigger" if job.trigger_type == "trigger" else "pending",
    )
    # A completed one-time job can still be copied and given a new date.
    expired_date = False
    if clone.trigger_type == 'date':
        try:
            build_trigger(clone)
        except ValueError:
            clone.status = 'paused'
            expired_date = True
    db.add(clone)
    await db.flush()
    for jg in job.job_groups:
        db.add(JobGroup(job_id=clone.id, group_id=jg.group_id, group_name=jg.group_name))
    mapping = {}
    for original in job.series_entries:
        entry = SeriesEntry(job_id=clone.id, position=original.position, message=original.message, status='pending')
        db.add(entry); await db.flush()
        for asset in original.attachments:
            copied = SeriesAttachment(id=str(uuid.uuid4()), user_id=job.user_id, entry_id=entry.id,
                                      path=asset.path, filename=asset.filename, position=asset.position)
            db.add(copied); mapping[asset.id] = copied.id
    if clone.series_plan:
        plan = json.loads(clone.series_plan)
        for source in plan.get('sources', []):
            for entry in source.get('entries', []):
                for asset in entry.get('attachments', []): asset['id'] = mapping[asset['id']]
        clone.series_plan = json.dumps(plan, ensure_ascii=False)
    await db.flush()
    await register_job(clone)
    await db.commit()
    if expired_date:
        return redirect_with_flash(f"/jobs/{clone.id}/edit", success="Job cloned. Choose a new date before resuming.")
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

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id).options(selectinload(Job.job_groups), selectinload(Job.series_entries).selectinload(SeriesEntry.attachments)))
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
    media_filename = (job.image_filename or Path(job.image_path).name) if media_available else ""
    media_is_video = bool(media_available and Path(job.image_path).suffix.lower() in VIDEO_EXTENSIONS)
    media_is_document = bool(media_available and Path(job.image_path).suffix.lower() in DOCUMENT_EXTENSIONS)

    return request.app.state.render(request, "jobs/detail.html",
                                     job=job, logs=logs, delivery_attempts=delivery_attempts, next_run=next_run,
                                     series_footer=json.loads(job.series_plan or '{}').get('footer', ''),
                                     media_available=media_available, media_filename=media_filename,
                                     media_is_video=media_is_video, media_is_document=media_is_document)


@router.get("/{job_id}/edit")
async def edit_job_page(request: Request, job_id: int, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])
    user_tz = user.get("tz", "UTC")

    result = await db.execute(select(Job).where(Job.id == job_id, Job.user_id == user_id).options(selectinload(Job.job_groups)))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.message_mode == 'series' and job.series_cursor:
        return redirect_with_flash(f'/jobs/{job.id}', success='This series has started. Pause it or clone it to create a new sequence.')

    result = await db.execute(select(Token).where(Token.user_id == user_id, Token.is_active == True))
    tokens = result.scalars().all()

    groups: list[dict] = []

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
                                     arabic_ordinals=ARABIC_ORDINAL_WORDS,
                                     job=job, draft_owner=user["sub"], tokens=tokens, groups=groups,
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
    message: str = Form(default=""),
    message_mode: str = Form(default="repeat"),
    series_definition: str = Form(default=""),
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

    token = await require_owned_active_token(db, user_id, token_id)

    if group_id_manual:
        group_ids.append(group_id_manual)
        group_names.append("")

    if not group_ids:
        return redirect_with_flash(f"/jobs/{job_id}/edit", success="Please select at least one group.")

    if trigger_type == "cron" and cron_freq == "custom" and not cron_days:
        return redirect_with_flash(f"/jobs/{job_id}/edit", success="Select at least one day.")

    if job.message_mode == 'series' and job.series_cursor:
        return await job_error_form(request, db, user, HTTPException(400, 'This series has started. Pause it or clone it to create a new sequence.'), group_ids, group_names, job)

    if job.message_mode == 'series' or message_mode == 'series':
        edit_claim = await db.execute(update(Job).where(
            Job.id == job.id, Job.series_cursor == 0, Job.series_active_run.is_(None),
        ).values(series_active_run='edit-' + uuid.uuid4().hex[:31]))
        if not edit_claim.rowcount:
            return await job_error_form(request, db, user, HTTPException(409, 'This series has started. Refresh the page before editing.'), group_ids, group_names, job)
    plan_data = None
    try:
        if message_mode not in {'repeat', 'series'}: raise ValueError('Choose a valid message mode')
        if message_mode == 'series':
            if trigger_type not in {'cron', 'trigger'}:
                raise ValueError('Message series need a recurring schedule or an external trigger')
            plan_data = await validate_plan(db, series_definition, user_id, token, group_ids, message, job)
        elif not message:
            raise ValueError('Enter a message')
    except (ValueError, HTTPException) as exc:
        error = exc if isinstance(exc, HTTPException) else HTTPException(400, str(exc))
        return await job_error_form(request, db, user, error, group_ids, group_names, job)
    old_image_path = job.image_path
    image_path = job.image_path if message_mode == "repeat" else None
    new_path = None
    if image and image.filename and message_mode == "repeat":
        try:
            new_path = await save_upload(image)
        except HTTPException as exc:
            return await job_error_form(request, db, user, exc, group_ids, group_names, job)
        if new_path:
            image_path = new_path

    try:
        if message_mode == "repeat": validate_job_payload(token, group_ids, message, image_path)
        trigger_value = build_trigger_value(
            trigger_type, user_tz,
            trigger_value_date=trigger_value_date,
            cron_time=cron_time,
            cron_freq=cron_freq,
            cron_dom=cron_dom,
            cron_days=cron_days,
            cron_raw=cron_raw,
        )
    except (ValueError, zoneinfo.ZoneInfoNotFoundError, HTTPException) as exc:
        if new_path:
            Path(new_path).unlink(missing_ok=True)
        error = exc if isinstance(exc, HTTPException) else HTTPException(400, str(exc))
        return await job_error_form(request, db, user, error, group_ids, group_names, job)

    if new_path and job.image_path:
        await delete_media_if_unreferenced(db, job.image_path, job.id)
    job.token_id = token_id
    job.label = label or None
    job.group_id = group_ids[0] if group_ids else ""
    job.group_name = group_names[0] if group_names else None
    job.message = message
    job.image_path = image_path
    if new_path:
        job.image_filename = Path(image.filename.replace("\\", "/")).name
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
    removed_paths = []
    if plan_data:
        removed_paths = await persist_plan(db, job, *plan_data)
    elif job.message_mode == 'series':
        assets = (await db.execute(select(SeriesAttachment).join(SeriesEntry).where(SeriesEntry.job_id == job.id))).scalars().all()
        removed_paths = [asset.path for asset in assets]
        entries = (await db.execute(select(SeriesEntry).where(SeriesEntry.job_id == job.id))).scalars().all()
        for entry in entries: await db.delete(entry)
        job.message_mode, job.series_plan = 'repeat', None
        job.series_total = 0
        job.series_cursor = 0
        job.series_active_run = None
    await remove_job(job_id)
    await register_job(job)
    await db.commit()
    if old_image_path and old_image_path != image_path: removed_paths.append(old_image_path)
    for path in set(removed_paths): await delete_unreferenced_path(db, path)

    return redirect_with_flash("/jobs", success="Job updated")
