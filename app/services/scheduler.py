import json
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select

from app.database import async_session
from app.models.job import Job
from app.models.log import Log
from app.models.token import Token
from app.services.crypto import decrypt_token
from app.services.sender import WhatsAppSender

scheduler = AsyncIOScheduler(timezone="UTC")


async def send_job(job_id: int):
    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if not job:
            return

        if job.skip_count > 0:
            job.skip_count -= 1
            log = Log(job_id=job.id, status="skipped", response=f"Skipped ({job.skip_count} remaining)")
            db.add(log)
            if job.trigger_type in ("now", "date") and job.skip_count == 0:
                job.status = "completed"
            await db.commit()
            return

        result = await db.execute(select(Token).where(Token.id == job.token_id))
        token = result.scalar_one_or_none()
        if not token or not token.is_active:
            log = Log(job_id=job.id, status="failed", response="Token not found or inactive")
            db.add(log)
            await db.commit()
            return

        sender = WhatsAppSender(api_token=decrypt_token(token.api_token))
        try:
            resp = await sender.send(job.group_id, job.message, job.image_path)
            status = "sent"
            response = json.dumps(resp, indent=2, default=str)
        except Exception as e:
            status = "failed"
            response = str(e)

        log = Log(job_id=job.id, status=status, response=response)
        db.add(log)

        job.status = "active" if job.trigger_type == "cron" else ("completed" if status == "sent" else "pending")
        token.last_used_at = datetime.now()
        await db.commit()


async def register_job(job: Job):
    if job.status not in ("pending", "active"):
        return
    if job.trigger_type == "trigger":
        return

    trigger = None
    if job.trigger_type == "now":
        trigger = DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=2))
    elif job.trigger_type == "date":
        try:
            run_date = datetime.strptime(job.trigger_value, "%Y-%m-%d %H:%M")
        except ValueError:
            return
        if run_date < datetime.now():
            return
        trigger = DateTrigger(run_date=run_date)
    elif job.trigger_type == "cron":
        parts = job.trigger_value.split()
        if len(parts) != 5:
            return
        trigger = CronTrigger(minute=parts[0], hour=parts[1], day=parts[2], month=parts[3], day_of_week=parts[4])

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
