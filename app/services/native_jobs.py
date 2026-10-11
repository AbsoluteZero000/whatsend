"""Durable WhatsApp job submissions. WhatsApp owns delivery retries after acceptance."""
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from sqlalchemy import select, update

from app.database import async_session
from app.models.delivery_attempt import DeliveryAttempt
from app.models.job import Job
from app.models.log import Log
from app.models.token import Token
from app.services.crypto import decrypt_token
from app.services.native_api import connection_base_url
from app.services.native_sender import NativeSender

PENDING = {"submitting", "queued", "processing"}
ACTIVE_RUNS = set()


async def finish_run(db, job, run_id):
    attempts = (await db.execute(select(DeliveryAttempt).where(
        DeliveryAttempt.job_id == job.id, DeliveryAttempt.run_id == run_id,
    ))).scalars().all()
    if not attempts or any(attempt.status in PENDING for attempt in attempts):
        return
    status = "unknown" if any(a.status == "unknown" for a in attempts) else (
        "failed" if any(a.status == "failed" for a in attempts) else "sent")
    db.add(Log(job_id=job.id, status=status, response=json.dumps([
        {"recipient": a.group_id, "status": a.status} for a in attempts
    ])))
    if job.status not in {"cancelled", "paused"}:
        if job.trigger_type == "cron":
            job.status = "active"
        elif job.trigger_type == "trigger":
            job.status = "trigger"
        else:
            job.status = "completed" if status == "sent" else status


async def submit_job(db, job, token, groups):
    if job.status in {"queued", "processing", "submitting", "completed", "cancelled", "paused"}:
        return
    run_id = str(uuid4())
    # Claim one-shot/manual runs before enqueueing; concurrent button presses cannot resubmit.
    if job.trigger_type != "cron":
        claimed = await db.execute(update(Job).where(
            Job.id == job.id, Job.status.in_(["active", "pending", "trigger", "retry_scheduled"]),
        ).values(status="queued"))
        if not claimed.rowcount:
            return
    sender = NativeSender(session_id=token.relay_session_id)
    attempts = []
    for group in {g.group_id: g for g in groups}.values():
        attempt = DeliveryAttempt(
            job_id=job.id, run_id=run_id, group_id=group.group_id, group_name=group.group_name,
            status="submitting", attempt_number=1, relay_token_id=token.id,
            relay_session_id=token.relay_session_id, relay_base_url=connection_base_url(token),
        )
        db.add(attempt)
        attempts.append(attempt)
    job.retry_at = None
    job.retry_group_ids = None
    job.retry_count = 0
    # Crash recovery treats unacknowledged submissions as unknown, never enqueueing again.
    await db.commit()
    ACTIVE_RUNS.add(run_id)
    try:
        for attempt in attempts:
            try:
                if job.image_path:
                    sender.validate_media(attempt.group_id, job.image_path, job.message)
                else:
                    sender.validate_text(attempt.group_id, job.message)
            except (ValueError, OSError):
                attempt.status = "failed"
                attempt.response = "The job payload is unsupported or its attachment is unavailable. Edit the job before sending again."
                await db.commit()
                continue
            try:
                key = f"echosend-job:{run_id}:{attempt.id}"
                if job.image_path:
                    data = await sender.enqueue_media(attempt.group_id, job.image_path, job.message, key)
                else:
                    data = await sender.enqueue_text(attempt.group_id, job.message, key)
                attempt.relay_message_id = data["id"]
                attempt.status = "queued"
                attempt.response = json.dumps(data)
            except httpx.HTTPStatusError as exc:
                definitive = 400 <= exc.response.status_code < 500 and exc.response.status_code not in {408, 409}
                if exc.response.status_code == 409:
                    try:
                        definitive = exc.response.json().get("error", {}).get("code") == "not_connected"
                    except (ValueError, AttributeError):
                        pass
                attempt.status = "failed" if definitive else "unknown"
                attempt.response = (f"WhatsApp rejected the request ({exc.response.status_code})." if definitive else
                                    "WhatsApp acknowledgement is uncertain. Check WhatsApp before sending again; this recipient will not be retried automatically.")
            except (httpx.RequestError, ValueError, OSError):
                attempt.status = "unknown"
                attempt.response = "WhatsApp acknowledgement is uncertain. Check WhatsApp before sending again; this recipient will not be retried automatically."
            await db.commit()
    finally:
        ACTIVE_RUNS.discard(run_id)
    token.last_used_at = datetime.now(timezone.utc)
    if any(a.status in PENDING for a in attempts):
        db.add(Log(job_id=job.id, status="queued", response="WhatsApp accepted messages; awaiting delivery confirmation."))
    await finish_run(db, job, run_id)
    await db.commit()


async def poll_deliveries():
    async with async_session() as db:
        attempts = (await db.execute(select(DeliveryAttempt).where(
            DeliveryAttempt.relay_token_id.is_not(None), DeliveryAttempt.status.in_(PENDING),
        ).order_by(DeliveryAttempt.id).limit(200))).scalars().all()
        completed_runs = set()
        for attempt in attempts:
            if attempt.status == "submitting":
                if attempt.run_id in ACTIVE_RUNS:
                    continue
                created = attempt.created_at.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - created).total_seconds() < 120:
                    continue
                attempt.status = "unknown"
                attempt.response = "Submission interrupted before acknowledgement. Check WhatsApp before sending again."
            else:
                job = await db.get(Job, attempt.job_id)
                token = await db.get(Token, attempt.relay_token_id)
                if not job:
                    continue
                if (not token or not token.is_active or token.user_id != job.user_id or token.provider != "native"
                        or token.relay_session_id != attempt.relay_session_id
                        or connection_base_url(token) != attempt.relay_base_url):
                    attempt.status = "unknown"
                    attempt.response = "The original WhatsApp connection is unavailable. Restore it and check WhatsApp before sending again."
                else:
                    try:
                        sender = NativeSender(session_id=attempt.relay_session_id)
                        data = await sender.get_message(attempt.relay_message_id)
                    except (httpx.HTTPError, ValueError):
                        # A polling failure says nothing about delivery; retain the acknowledged ID.
                        continue
                    attempt.status = "queued" if data["status"] == "pending" else data["status"]
                    attempt.response = json.dumps(data)
            if attempt.status not in PENDING:
                completed_runs.add((attempt.job_id, attempt.run_id))
        await db.flush()
        for job_id, run_id in completed_runs:
            job = await db.get(Job, job_id)
            if job:
                await finish_run(db, job, run_id)
        await db.commit()
