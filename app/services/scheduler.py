import json
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.job import Job
from app.models.log import Log
from app.models.token import Token
from app.services.crypto import decrypt_token
from app.services.sender import WhatsAppSender

MISFIRE_GRACE_SECONDS = 10 * 60

scheduler = AsyncIOScheduler(
    timezone=timezone.utc,
    job_defaults={
        "misfire_grace_time": MISFIRE_GRACE_SECONDS,
        "coalesce": True,
        "max_instances": 1,
    },
)


async def send_job(job_id: int):
    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.id == job_id).options(selectinload(Job.job_groups)))
        job = result.scalar_one_or_none()
        if not job:
            return

        if job.skip_count > 0 and job.trigger_type not in ("trigger", "now"):
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

        result = await db.execute(select(Token).where(Token.id == job.token_id))
        token = result.scalar_one_or_none()
        if not token or not token.is_active:
            log = Log(job_id=job.id, status="failed", response="Token not found or inactive")
            db.add(log)
            await db.commit()
            return

        groups = job.job_groups if job.job_groups else [type("G", (object,), {"group_id": job.group_id, "group_name": job.group_name})()]
        groups = [g for g in groups if g.group_id]
        if not groups:
            log = Log(job_id=job.id, status="failed", response="No groups assigned to this job")
            db.add(log)
            await db.commit()
            return

        sender = WhatsAppSender(api_token=decrypt_token(token.api_token))
        results: list[str] = []
        overall_status = "sent"
        for g in groups:
            try:
                resp = await sender.send(g.group_id, job.message, job.image_path)
                results.append(f"{g.group_id}: sent")
            except Exception as e:
                overall_status = "failed"
                results.append(f"{g.group_id}: {e}")

        status = overall_status
        response = json.dumps(results, indent=2, default=str)

        log = Log(job_id=job.id, status=status, response=response)
        db.add(log)

        if job.trigger_type == "trigger":
            job.status = "trigger"
        elif job.trigger_type == "cron":
            job.status = "active"
        else:
            job.status = "completed" if status == "sent" else "pending"
        token.last_used_at = datetime.now(timezone.utc)
        await db.commit()


async def register_job(job: Job):
    if job.status not in ("pending", "active"):
        return
    if job.trigger_type == "trigger":
        return

    trigger = None
    now = datetime.now(timezone.utc)
    if job.trigger_type == "now":
        trigger = DateTrigger(run_date=now + timedelta(seconds=2), timezone=timezone.utc)
    elif job.trigger_type == "date":
        try:
            run_date = datetime.strptime(job.trigger_value, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            return
        if run_date < now - timedelta(seconds=MISFIRE_GRACE_SECONDS):
            return
        if run_date < now:
            run_date = now + timedelta(seconds=1)
        trigger = DateTrigger(run_date=run_date, timezone=timezone.utc)
    elif job.trigger_type == "cron":
        parts = job.trigger_value.split()
        if len(parts) != 5:
            return
        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
            timezone=timezone.utc,
        )

    if trigger:
        scheduler.add_job(send_job, trigger=trigger, args=[job.id], id=str(job.id), replace_existing=True)
        job.status = "active"


async def remove_job(job_id: int):
    job_id_str = str(job_id)
    existing = scheduler.get_job(job_id_str)
    if existing:
        scheduler.remove_job(job_id_str)


async def load_all_jobs():
    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.status.in_(["pending", "active"])))
        jobs = result.scalars().all()
        for job in jobs:
            await register_job(job)
        await db.commit()
