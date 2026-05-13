"""
WhatsApp Scheduler — Entry Point
Reads config.yaml and schedules all messages using APScheduler.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from sender import WhatsAppSender

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scheduler.log"),
    ],
)
logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────────────
def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        logger.error(f"Config file not found: {path}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    logger.info(f"Loaded config from {path}")
    return config


def resolve_image(image_path: str | None) -> str | None:
    """Return the absolute path to an image, or None if not provided."""
    if not image_path:
        return None
    p = Path(image_path)
    if not p.exists():
        logger.warning(f"Image not found, skipping: {image_path}")
        return None
    return str(p.resolve())


# ── Job callback ──────────────────────────────────────────────────────────────
def dispatch_message(
    sender: WhatsAppSender,
    group_id: str,
    message: str,
    image: str | None,
    job_label: str,
) -> None:
    logger.info(f"[{job_label}] Sending to {group_id} ...")
    try:
        result = sender.send(group_id, message, image)
        logger.info(f"[{job_label}] ✓ Sent  →  {result}")
    except Exception as exc:
        logger.error(f"[{job_label}] ✗ Failed: {exc}")


# ── Scheduler setup ───────────────────────────────────────────────────────────
def register_jobs(scheduler: BlockingScheduler, sender: WhatsAppSender, jobs: list[dict]) -> int:
    """
    Register all jobs from the config.
    Each job must have:
      - group_id   : WhatsApp group chat-id
      - message    : text to send
    And ONE of:
      - date       : "YYYY-MM-DD HH:MM"  (one-time)
      - cron       : "MIN HOUR DOM MON DOW"  (recurring, standard cron)
    Optional:
      - image      : path to image file
      - label      : friendly name for logs
    """
    registered = 0

    for i, job in enumerate(jobs, start=1):
        label = job.get("label", f"job-{i}")
        group_id = job.get("group_id", "").strip()
        message = job.get("message", "").strip()
        image = resolve_image(job.get("image"))

        if not group_id or not message:
            logger.warning(f"[{label}] Skipping — 'group_id' or 'message' is missing.")
            continue

        kwargs = dict(
            func=dispatch_message,
            args=[sender, group_id, message, image, label],
            id=label,
            name=label,
        )

        # ── One-time trigger ──────────────────────────────────────────────────
        if "date" in job:
            try:
                run_date = datetime.strptime(job["date"], "%Y-%m-%d %H:%M")
            except ValueError:
                logger.warning(f"[{label}] Invalid date format '{job['date']}'. Use YYYY-MM-DD HH:MM. Skipping.")
                continue

            if run_date < datetime.now():
                logger.warning(f"[{label}] Scheduled time {run_date} is in the past. Skipping.")
                continue

            scheduler.add_job(trigger=DateTrigger(run_date=run_date), **kwargs)
            logger.info(f"[{label}] Scheduled (one-time) → {run_date}  group={group_id}")

        # ── Recurring cron trigger ────────────────────────────────────────────
        elif "cron" in job:
            parts = job["cron"].split()
            if len(parts) != 5:
                logger.warning(f"[{label}] Invalid cron '{job['cron']}'. Use 'MIN HOUR DOM MON DOW'. Skipping.")
                continue

            minute, hour, day, month, day_of_week = parts
            scheduler.add_job(
                trigger=CronTrigger(
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=day_of_week,
                ),
                **kwargs,
            )
            logger.info(f"[{label}] Scheduled (recurring cron) → {job['cron']}  group={group_id}")

        else:
            logger.warning(f"[{label}] No 'date' or 'cron' key found. Skipping.")
            continue

        registered += 1

    return registered


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    config = load_config()

    api_cfg = config.get("api", {})
    sender = WhatsAppSender(
        api_token=str(api_cfg.get("api_token", "")),
    )

    scheduler = BlockingScheduler(timezone="UTC")

    jobs = config.get("schedule", [])
    count = register_jobs(scheduler, sender, jobs)

    if count == 0:
        logger.error("No valid jobs registered. Exiting.")
        sys.exit(1)

    logger.info(f"Starting scheduler with {count} job(s). Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user.")


if __name__ == "__main__":
    main()
