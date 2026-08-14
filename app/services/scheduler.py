import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.delivery_attempt import DeliveryAttempt
from app.models.job import Job
from app.models.log import Log
from app.models.token import Token
from app.services.crypto import decrypt_token
from app.services.sender import WhatsAppSender

logger = logging.getLogger(__name__)

MISFIRE_GRACE_SECONDS = 10 * 60
MAX_RETRIES = 3
RETRY_BASE_SECONDS = 60

scheduler = AsyncIOScheduler(
    timezone=timezone.utc,
    job_defaults={
        "misfire_grace_time": MISFIRE_GRACE_SECONDS,
        "coalesce": True,
        "max_instances": 1,
    },
)


def build_trigger(job: Job):
    now = datetime.now(timezone.utc)
    if job.trigger_type == "now":
        return DateTrigger(run_date=now + timedelta(seconds=2), timezone=timezone.utc)
    if job.trigger_type == "date":
        run_date = datetime.strptime(job.trigger_value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        if run_date < now - timedelta(seconds=MISFIRE_GRACE_SECONDS):
            raise ValueError("Scheduled time is too far in the past")
        if run_date < now:
            run_date = now + timedelta(seconds=1)
        return DateTrigger(run_date=run_date, timezone=timezone.utc)
    if job.trigger_type == "cron":
        return CronTrigger.from_crontab(job.trigger_value, timezone=job.schedule_timezone or "UTC")
    if job.trigger_type == "trigger":
        return None
    raise ValueError("Unsupported trigger type")


def _retry_groups(job: Job) -> list[str]:
    if not job.retry_group_ids:
        return []
    try:
        value = json.loads(job.retry_group_ids)
        return [str(group_id) for group_id in value if group_id]
    except (TypeError, ValueError):
        return []


async def _schedule_retry(job: Job, failed_group_ids: list[str]) -> bool:
    if not failed_group_ids or job.retry_count >= MAX_RETRIES:
        job.retry_at = None
        job.retry_group_ids = None
        if job.trigger_type in {"date", "now"}:
            job.status = "failed"
        existing = scheduler.get_job(f"retry:{job.id}")
        if existing:
            scheduler.remove_job(f"retry:{job.id}")
        return False

    job.retry_count += 1
    delay = RETRY_BASE_SECONDS * (2 ** (job.retry_count - 1))
    retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
    job.retry_at = retry_at.isoformat()
    job.retry_group_ids = json.dumps(failed_group_ids)
    if job.trigger_type in {"date", "now"}:
        job.status = "retry_scheduled"
    scheduler.add_job(
        send_job,
        trigger=DateTrigger(run_date=retry_at, timezone=timezone.utc),
        args=[job.id, True],
        id=f"retry:{job.id}",
        replace_existing=True,
    )
    return True


async def send_job(job_id: int, retry_only: bool = False):
    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.id == job_id).options(selectinload(Job.job_groups)))
        job = result.scalar_one_or_none()
        if not job:
            return

        if job.skip_count > 0 and job.trigger_type not in ("trigger", "now") and not retry_only:
            if job.trigger_type == "date":
                skipped = job.skip_count
                job.skip_count = 0
                log = Log(job_id=job.id, status="skipped", response=f"Skipped {skipped} time(s)")
                job.status = "completed"
            else:
                job.skip_count -= 1
                log = Log(job_id=job.id, status="skipped", response=f"Skipped ({job.skip_count} remaining)")
            db.add(log)
            await db.commit()
            return

        groups = job.job_groups if job.job_groups else [type("G", (object,), {"group_id": job.group_id, "group_name": job.group_name})()]
        groups = [group for group in groups if group.group_id]
        if retry_only:
            allowed = set(_retry_groups(job))
            groups = [group for group in groups if group.group_id in allowed]
        if not groups:
            logger.warning("send_job has no recipients", extra={"job_id": job.id, "retry_only": retry_only})
            return

        token_result = await db.execute(
            select(Token).where(Token.id == job.token_id, Token.user_id == job.user_id, Token.is_active.is_(True))
        )
        token = token_result.scalar_one_or_none()
        run_id = str(uuid.uuid4())
        attempt_number = job.retry_count + 1 if retry_only else 1
        results: list[str] = []
        failed_group_ids: list[str] = []

        if not token:
            for group in groups:
                failed_group_ids.append(group.group_id)
                db.add(DeliveryAttempt(
                    job_id=job.id, run_id=run_id, group_id=group.group_id, group_name=group.group_name,
                    attempt_number=attempt_number, status="failed", response="Token not found, inactive, or owned by another user",
                ))
            results.append("Token not found, inactive, or owned by another user")
        else:
            sender = WhatsAppSender(api_token=decrypt_token(token.api_token))
            for group in groups:
                try:
                    response = await sender.send(group.group_id, job.message, job.image_path)
                    results.append(f"{group.group_id}: sent")
                    db.add(DeliveryAttempt(
                        job_id=job.id, run_id=run_id, group_id=group.group_id, group_name=group.group_name,
                        attempt_number=attempt_number, status="sent", response=json.dumps(response, default=str)[:4000],
                    ))
                except Exception as exc:
                    failed_group_ids.append(group.group_id)
                    results.append(f"{group.group_id}: {exc}")
                    db.add(DeliveryAttempt(
                        job_id=job.id, run_id=run_id, group_id=group.group_id, group_name=group.group_name,
                        attempt_number=attempt_number, status="failed", response=str(exc)[:4000],
                    ))
            token.last_used_at = datetime.now(timezone.utc)

        status = "failed" if failed_group_ids else "sent"
        db.add(Log(job_id=job.id, status=status, response=json.dumps(results, indent=2, default=str)))

        if failed_group_ids:
            await _schedule_retry(job, failed_group_ids)
        else:
            existing_retry = scheduler.get_job(f"retry:{job.id}")
            if existing_retry:
                scheduler.remove_job(f"retry:{job.id}")
            job.retry_count = 0
            job.retry_at = None
            job.retry_group_ids = None
            if job.trigger_type == "trigger":
                job.status = "trigger"
            elif job.trigger_type == "cron":
                job.status = "active"
            else:
                job.status = "completed"

        await db.commit()
        logger.info(
            "delivery run completed",
            extra={"job_id": job.id, "run_id": run_id, "status": status, "failed_recipients": len(failed_group_ids)},
        )


async def register_job(job: Job):
    if job.status not in ("pending", "active", "retry_scheduled"):
        return
    trigger = build_trigger(job)
    if trigger:
        scheduler.add_job(send_job, trigger=trigger, args=[job.id, False], id=str(job.id), replace_existing=True)
        job.status = "active"


async def register_retry(job: Job):
    if not job.retry_at or not _retry_groups(job):
        return
    retry_at = datetime.fromisoformat(job.retry_at)
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    if retry_at < datetime.now(timezone.utc):
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    scheduler.add_job(
        send_job,
        trigger=DateTrigger(run_date=retry_at, timezone=timezone.utc),
        args=[job.id, True],
        id=f"retry:{job.id}",
        replace_existing=True,
    )


async def remove_job(job_id: int):
    for scheduler_id in (str(job_id), f"retry:{job_id}"):
        existing = scheduler.get_job(scheduler_id)
        if existing:
            scheduler.remove_job(scheduler_id)


async def load_all_jobs():
    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.status.in_(["pending", "active", "retry_scheduled"])))
        jobs = result.scalars().all()
        for job in jobs:
            if job.trigger_type in {"now", "date"} and job.status == "retry_scheduled":
                await register_retry(job)
            else:
                try:
                    await register_job(job)
                except ValueError:
                    job.status = "failed"
            if job.retry_at and job.trigger_type == "cron":
                await register_retry(job)
        await db.commit()
