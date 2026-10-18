"""One durable series entry per scheduled occurrence; never replay uncertain sends."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import select, update, func
from sqlalchemy.orm import selectinload
from app.models.job import Job, JobGroup
from app.models.token import Token
from types import SimpleNamespace
from app.models.job_series import SeriesEntry
from app.models.delivery_attempt import DeliveryAttempt
from app.models.log import Log
from app.services.native_sender import CONNECTION_LOST, NativeSender, worker_error_code
from app.services.native_api import connection_base_url
from app.services.sender import WhatsAppSender
from app.services.crypto import decrypt_token
from app.services.message_variables import render_message
from app.services.schedule_dates import remaining_dates

PENDING = {"submitting", "queued", "processing"}


def terminal_status(attempts):
    return "unknown" if any(a.status == 'unknown' for a in attempts) else (
        "failed" if any(a.status == 'failed' for a in attempts) else "sent")


async def series_result(db, job):
    statuses = (await db.execute(select(SeriesEntry.status).where(SeriesEntry.job_id == job.id))).scalars().all()
    return 'unknown' if 'unknown' in statuses else 'failed' if 'failed' in statuses else 'completed'


async def finish_series_run(db, job, run_id):
    entry = (await db.execute(select(SeriesEntry).where(SeriesEntry.job_id == job.id, SeriesEntry.run_id == run_id))).scalar_one_or_none()
    if not entry:
        return
    attempts = (await db.execute(select(DeliveryAttempt).where(DeliveryAttempt.job_id == job.id, DeliveryAttempt.run_id == run_id))).scalars().all()
    if not attempts or any(a.status in PENDING for a in attempts):
        return
    status = terminal_status(attempts)
    if entry.status != status:
        entry.status = status
        db.add(Log(job_id=job.id, status=status, response=json.dumps({
            'send_number': entry.position,
            'recipients': [{'recipient': a.group_id, 'part': a.part_label, 'status': a.status} for a in attempts],
        })))
    await db.execute(update(Job).where(Job.id == job.id, Job.series_active_run == run_id).values(series_active_run=None))
    await db.flush()
    await db.refresh(job)
    if job.series_active_run or job.status == 'cancelled':
        return
    total = await db.scalar(select(func.count()).select_from(SeriesEntry).where(SeriesEntry.job_id == job.id))
    if job.series_cursor >= total or (job.trigger_type == 'dates' and not remaining_dates(job)):
        # Finished every part, or ran out of picked dates.
        job.status = await series_result(db, job)
        from app.services.scheduler import remove_job
        await remove_job(job.id)
    elif job.status != 'paused':
        job.status = 'trigger' if job.trigger_type == 'trigger' else 'active'


async def submit_series(db, job, token, groups):
    if job.series_active_run or job.status not in {'pending', 'active', 'trigger'}:
        return
    position = job.series_cursor + 1
    run_id = str(uuid4())
    claimed = await db.execute(update(Job).where(
        Job.id == job.id, Job.message_mode == 'series', Job.series_cursor == job.series_cursor,
        Job.series_active_run.is_(None), Job.status.in_(['pending', 'active', 'trigger']),
    ).values(series_cursor=position, series_active_run=run_id, status='queued'))
    if not claimed.rowcount:
        return
    # Fetch content and recipients after obtaining the write claim. An edit may
    # have replaced the unstarted plan while this scheduler callback was reading.
    await db.refresh(job)
    entry = (await db.execute(select(SeriesEntry).where(
        SeriesEntry.job_id == job.id, SeriesEntry.position == position,
    ).options(selectinload(SeriesEntry.attachments)))).scalar_one_or_none()
    if not entry:
        await db.rollback()
        return
    groups = (await db.execute(select(JobGroup).where(JobGroup.job_id == job.id))).scalars().all() or [
        SimpleNamespace(group_id=job.group_id, group_name=job.group_name)]
    token = await db.scalar(select(Token).where(Token.id == job.token_id, Token.user_id == job.user_id,
                           Token.is_active.is_(True)).execution_options(populate_existing=True))
    entry.status, entry.run_id = 'submitting', run_id
    parts = []
    total = await db.scalar(select(func.count()).select_from(SeriesEntry).where(SeriesEntry.job_id == job.id))
    footer = json.loads(job.series_plan or '{}').get('footer', '')
    text = render_message('\n\n'.join(part for part in [job.message, entry.message, footer] if part.strip()), entry.position, total, job.schedule_timezone)
    if text:
        parts.append((text, None, None, 'Message'))
    for attachment in entry.attachments:
        parts.append(('', attachment.path, attachment.filename, attachment.filename))
    attempts = []
    for group in {g.group_id: g for g in groups}.values():
        for text, path, filename, label in parts:
            attempt = DeliveryAttempt(job_id=job.id, run_id=run_id, group_id=group.group_id,
                group_name=group.group_name, status='submitting', attempt_number=1,
                series_entry_id=entry.id, part_label=label,
                relay_token_id=token.id if token and token.provider == 'native' else None,
                relay_session_id=token.relay_session_id if token and token.provider == 'native' else None,
                relay_base_url=connection_base_url(token) if token and token.provider == 'native' else None)
            db.add(attempt)
            attempts.append((attempt, text, path, filename))
    # Claim, cursor, entry and every part are committed together before any send.
    await db.commit()
    from app.services.native_jobs import ACTIVE_RUNS
    ACTIVE_RUNS.add(run_id)
    try:
        sender = NativeSender(session_id=token.relay_session_id) if token and token.provider == 'native' else (
            WhatsAppSender(api_token=decrypt_token(token.api_token)) if token and token.provider == 'whatsapp' else None)
        for attempt, text, path, filename in attempts:
            try:
                if not sender:
                    raise ValueError('Connection unavailable')
                if path and not Path(path).is_file():
                    raise ValueError('Attachment unavailable')
                if isinstance(sender, NativeSender):
                    if path: sender.validate_media(attempt.group_id, path, '')
                    else: sender.validate_text(attempt.group_id, text)
            except (ValueError, OSError):
                attempt.status = 'failed'
                attempt.response = 'The connection or attachment is unavailable. This part was not sent.'
                await db.commit()
                continue
            try:
                if isinstance(sender, NativeSender):
                    key = f'echosend-series:{run_id}:{attempt.id}'
                    data = await sender.enqueue_media(attempt.group_id, path, '', key, filename=filename) if path else await sender.enqueue_text(attempt.group_id, text, key)
                    attempt.relay_message_id, attempt.status = data['id'], 'queued'
                else:
                    data = await sender.send(attempt.group_id, text, path, filename)
                    attempt.status = 'sent'
                attempt.response = json.dumps(data, default=str)
            except httpx.HTTPStatusError as exc:
                disconnected = isinstance(sender, NativeSender) and exc.response.status_code == 409 and worker_error_code(exc.response) == 'not_connected'
                definitive = 400 <= exc.response.status_code < 500 and exc.response.status_code not in {408, 409, 429}
                if exc.response.status_code == 409:
                    definitive = disconnected
                attempt.status = 'failed' if definitive else 'unknown'
                attempt.response = CONNECTION_LOST if disconnected else (f'WhatsApp rejected this part ({exc.response.status_code}).' if definitive else 'Send outcome is uncertain. This part will not be resent automatically.')
            except Exception:
                attempt.status = 'unknown'
                attempt.response = 'Send outcome is uncertain. This part will not be resent automatically.'
            await db.commit()
    finally:
        ACTIVE_RUNS.discard(run_id)
    if token: token.last_used_at = datetime.now(timezone.utc)
    entry.status = 'queued' if any(a.status in PENDING for a, *_ in attempts) else terminal_status([a for a, *_ in attempts])
    # finish_series_run records the first terminal result, including immediate failures.
    if entry.status not in PENDING: entry.status = 'submitting'
    await finish_series_run(db, job, run_id)
    await db.commit()
