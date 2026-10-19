"""What Claude can do in EchoSend through the connector (see routers/mcp.py).

Every tool acts as one signed-in user and goes through the same checks and scheduling code as the website.
"""
import json
import re
import zoneinfo
from datetime import datetime, timedelta, timezone
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.delivery_attempt import DeliveryAttempt
from app.models.job import Job, JobGroup
from app.models.recipient_list import RecipientList
from app.models.token import Token
from app.models.user import User
from app.routers import jobs as job_routes
from app.services.schedule_dates import parse_dates
from app.services.scheduler import register_job, scheduler as apscheduler

STATUS_GROUPS = {
    "upcoming": ["pending", "active", "trigger", "retry_scheduled", "queued", "processing"],
    "paused": ["paused"],
    "finished": ["completed", "cancelled"],
    "failed": ["failed", "unknown"],
}
# Same recipient formats the saved recipient lists accept.
RECIPIENT_ID = re.compile(r"\+?[0-9]{7,15}|[0-9]+(?:-[0-9]+)?@g\.us|[0-9]+@(?:s\.whatsapp\.net|lid)")
PHONE_LIKE = re.compile(r"\+?[0-9][0-9 ()\-]{5,24}")
WEEKDAY_NUMBERS = {"sunday": "0", "monday": "1", "tuesday": "2", "wednesday": "3", "thursday": "4", "friday": "5", "saturday": "6"}
MAX_RECIPIENTS = 256


class ToolError(Exception):
    """A problem Claude should explain to the user or fix and retry."""


class Context:
    def __init__(self, db: AsyncSession, user: User, base_url: str):
        self.db = db
        self.user = user
        self.base_url = base_url
        self.tz = user.timezone or "UTC"

    def local(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        try:
            return value.astimezone(zoneinfo.ZoneInfo(self.tz)).strftime("%Y-%m-%d %H:%M")
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            return value.strftime("%Y-%m-%d %H:%M UTC")

    def now_local(self) -> datetime:
        try:
            return datetime.now(zoneinfo.ZoneInfo(self.tz))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            return datetime.now(timezone.utc)


# ---------- arguments ----------

class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArguments(Arguments):
    pass


class ListRecipients(Arguments):
    connection_id: int | None = Field(None, description="Which WhatsApp connection to use. Only needed when the account has more than one.")
    search: str = Field("", max_length=100, description="Only return groups and lists whose name contains this text.")


class ListMessages(Arguments):
    status: Literal["upcoming", "paused", "finished", "failed", "all"] = Field(
        "upcoming", description="upcoming = scheduled or sending, finished = sent or cancelled.")
    search: str = Field("", max_length=100, description="Only return messages whose label or recipient contains this text.")
    limit: int = Field(20, ge=1, le=50)


class MessageId(Arguments):
    message_id: int = Field(description="The message ID from list_messages or create_message.")


class CreateMessage(Arguments):
    message: str = Field(min_length=1, max_length=4096, description="The text to send. WhatsApp formatting such as *bold* and _italic_ works.")
    recipients: list[str] = Field(default_factory=list, max_length=MAX_RECIPIENTS, description=(
        "Who to send to. Each item is a WhatsApp group name exactly as list_recipients shows it, a group ID ending in @g.us, "
        "or a phone number with its country code, e.g. +201001234567."))
    recipient_list_ids: list[int] = Field(default_factory=list, max_length=20, description="Saved recipient list IDs from list_recipients. Everyone on them is added.")
    when: Literal["now", "once", "repeating", "specific_dates"] = Field(description=(
        "now = send right away. once = one date and time (send_at). repeating = daily/weekdays/weekly/monthly (repeat + time). "
        "specific_dates = the same time on hand-picked days (dates + time)."))
    send_at: str | None = Field(None, description='For "once": local date and time as "YYYY-MM-DD HH:MM" in the account timezone.')
    time: str | None = Field(None, description='For "repeating" and "specific_dates": local time as "HH:MM", 24-hour clock.')
    repeat: Literal["daily", "weekdays", "weekly", "monthly", "cron"] | None = Field(None, description=(
        'For "repeating". weekdays = Monday to Friday. weekly needs weekdays. monthly needs day_of_month. cron needs cron.'))
    weekdays: list[Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]] = Field(
        default_factory=list, description='For repeat "weekly": the days to send on.')
    day_of_month: int | None = Field(None, ge=1, le=31, description='For repeat "monthly": the day of the month. Months without that day are skipped.')
    cron: str | None = Field(None, max_length=100, description='For repeat "cron": a 5-field cron expression in the account timezone (days of week 0 = Monday).')
    dates: list[str] = Field(default_factory=list, max_length=100, description='For "specific_dates": local days as "YYYY-MM-DD", up to 100.')
    label: str | None = Field(None, max_length=120, description="A short name to find this message later. Optional.")
    connection_id: int | None = Field(None, description="Which WhatsApp connection sends it. Only needed when the account has more than one.")


# ---------- helpers ----------

def connection_ready(token: Token) -> bool:
    return token.provider != "native" or bool(token.relay_session_id)


def connection_info(token: Token) -> dict:
    return {
        "id": token.id,
        "name": token.name or f"Connection {token.id}",
        "type": "WhatsApp (QR code)" if token.provider == "native" else "Whapi.Cloud",
        "ready": connection_ready(token),
    }


async def active_connections(ctx: Context) -> list[Token]:
    return (await ctx.db.execute(select(Token).where(
        Token.user_id == ctx.user.id, Token.is_active.is_(True)).order_by(Token.id))).scalars().all()


async def pick_connection(ctx: Context, connection_id: int | None) -> Token:
    if connection_id is not None:
        try:
            return await job_routes.require_owned_active_token(ctx.db, ctx.user.id, connection_id)
        except HTTPException as exc:
            raise ToolError(exc.detail) from exc
    ready = [token for token in await active_connections(ctx) if connection_ready(token)]
    if not ready:
        raise ToolError("This account has no WhatsApp connection yet. Connect WhatsApp in EchoSend under Connections first.")
    if len(ready) > 1:
        choices = ", ".join(f'{token.id} ("{token.name or token.id}")' for token in ready)
        raise ToolError(f"This account has more than one WhatsApp connection. Pass connection_id, one of: {choices}.")
    return ready[0]


async def fetch_groups(token: Token) -> list[dict]:
    try:
        groups = await job_routes.job_sender(token).get_groups()
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolError("Couldn't load WhatsApp groups. Check in EchoSend that this WhatsApp connection is online.") from exc
    return [{"id": group["id"], "name": group.get("name") or group["id"]} for group in groups if group.get("id")]


async def resolve_recipients(ctx: Context, token: Token, values: list[str], list_ids: list[int]) -> dict[str, str]:
    """Group names, group IDs, phone numbers and saved lists -> {recipient id: display name}."""
    resolved: dict[str, str] = {}
    for list_id in list_ids:
        row = (await ctx.db.execute(select(RecipientList).where(
            RecipientList.id == list_id, RecipientList.user_id == ctx.user.id, RecipientList.token_id == token.id,
        ))).scalar_one_or_none()
        if row is None:
            raise ToolError(f"Saved recipient list {list_id} doesn't exist for this WhatsApp connection.")
        for recipient in json.loads(row.recipients):
            resolved.setdefault(recipient["id"], recipient.get("name") or recipient["id"])

    groups: list[dict] | None = None
    for raw in values:
        value = raw.strip()
        if not value:
            continue
        if PHONE_LIKE.fullmatch(value):
            value = re.sub(r"[ ()\-]", "", value)
        if RECIPIENT_ID.fullmatch(value) or (token.provider != "native" and "@" in value):
            name = value
            if value.endswith("@g.us"):
                try:
                    groups = groups if groups is not None else await fetch_groups(token)
                    name = next((group["name"] for group in groups if group["id"] == value), value)
                except ToolError:
                    pass  # The name is only for display; the ID is enough to send.
            resolved.setdefault(value, name)
            continue
        groups = groups if groups is not None else await fetch_groups(token)
        matches = [group for group in groups if group["name"].strip().casefold() == value.casefold()]
        if len(matches) > 1:
            ids = ", ".join(group["id"] for group in matches)
            raise ToolError(f'More than one group is called "{value}" ({ids}). Use the group ID instead.')
        if not matches:
            raise ToolError(f'No WhatsApp group is called "{value}". Call list_recipients to see the exact names, '
                            "or use a phone number with its country code.")
        resolved.setdefault(matches[0]["id"], matches[0]["name"])
    if not resolved:
        raise ToolError("Add at least one recipient.")
    if len(resolved) > MAX_RECIPIENTS:
        raise ToolError(f"A message can go to at most {MAX_RECIPIENTS} recipients.")
    return resolved


def schedule_fields(args: CreateMessage, ctx: Context) -> tuple[str, dict]:
    """Translate the tool's schedule arguments into the website form's fields."""
    if args.when == "now":
        return "now", {}
    if args.when == "once":
        if not args.send_at:
            raise ToolError('Pass send_at as "YYYY-MM-DD HH:MM" for a one-time message.')
        try:
            local = datetime.strptime(args.send_at.strip().replace("T", " ")[:16], "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ToolError('send_at must look like "2026-11-03 09:00".') from exc
        if local < ctx.now_local().replace(tzinfo=None) - timedelta(minutes=1):
            raise ToolError(f"{args.send_at} has already passed in {ctx.tz} (it is {ctx.now_local():%Y-%m-%d %H:%M} there now).")
        return "date", {"trigger_value_date": local.strftime("%Y-%m-%d %H:%M")}
    if args.when == "repeating" and args.repeat == "cron":
        if not args.cron:
            raise ToolError("Pass a 5-field cron expression in cron.")
        return "cron", {"cron_freq": "raw", "cron_raw": args.cron}
    if not args.time:
        raise ToolError('Pass time as "HH:MM" (24-hour clock).')
    if args.when == "specific_dates":
        if not args.dates:
            raise ToolError('Pass the days to send on in dates, as "YYYY-MM-DD".')
        return "dates", {"schedule_dates": ",".join(args.dates), "dates_time": args.time}
    if args.repeat in (None, "daily", "weekdays"):
        return "cron", {"cron_freq": args.repeat or "daily", "cron_time": args.time}
    if args.repeat == "weekly":
        if not args.weekdays:
            raise ToolError('Pass weekdays, e.g. ["sunday", "wednesday"], for a weekly message.')
        return "cron", {"cron_freq": "custom", "cron_time": args.time, "cron_days": [WEEKDAY_NUMBERS[day] for day in args.weekdays]}
    if args.day_of_month is None:
        raise ToolError("Pass day_of_month (1-31) for a monthly message.")
    return "cron", {"cron_freq": "monthly", "cron_time": args.time, "cron_dom": args.day_of_month}


def describe_schedule(job: Job, ctx: Context) -> str:
    from app.main import cron_to_text, dates_text, tz

    if job.trigger_type == "now":
        return "Sent right away"
    if job.trigger_type == "date":
        try:
            return "Once on " + tz(datetime.strptime(job.trigger_value, "%Y-%m-%d %H:%M"), ctx.tz)
        except ValueError:
            return "Once"
    if job.trigger_type == "cron":
        return cron_to_text(job.trigger_value, job.schedule_timezone or "UTC", ctx.tz)
    if job.trigger_type == "dates":
        return "On specific dates. " + dates_text(job, ctx.tz, lambda text: text)
    if job.trigger_type == "trigger":
        return "When its external trigger is called"
    return job.trigger_type


def next_send(job: Job, ctx: Context) -> str | None:
    if job.status not in STATUS_GROUPS["upcoming"]:
        return None
    scheduled = apscheduler.get_job(str(job.id)) or apscheduler.get_job(f"retry:{job.id}")
    if scheduled is None:
        return None
    # Jobs added before the scheduler starts have no next_run_time yet.
    upcoming = getattr(scheduled, "next_run_time", None) or scheduled.trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    return ctx.local(upcoming)


def summary(job: Job, ctx: Context) -> dict:
    preview = job.message if len(job.message) <= 200 else job.message[:200] + "…"
    return {
        "id": job.id,
        "label": job.label,
        "status": job.status,
        "schedule": describe_schedule(job, ctx),
        "next_send": next_send(job, ctx),
        "recipients": [group.group_name or group.group_id for group in job.job_groups],
        "message": preview,
        "kind": "message series" if job.message_mode == "series" else "message",
        "link": f"{ctx.base_url}/jobs/{job.id}",
    }


async def owned_job(ctx: Context, job_id: int) -> Job:
    job = (await ctx.db.execute(select(Job).where(Job.id == job_id, Job.user_id == ctx.user.id)
                                .options(selectinload(Job.job_groups)))).scalar_one_or_none()
    if job is None:
        raise ToolError(f"There is no message {job_id} in this account.")
    return job


# ---------- tools ----------

async def get_account_overview(ctx: Context, args: NoArguments) -> dict:
    counts = dict((await ctx.db.execute(
        select(Job.status, func.count(Job.id)).where(Job.user_id == ctx.user.id).group_by(Job.status))).all())
    return {
        "username": ctx.user.username,
        "timezone": ctx.tz,
        "local_time_now": ctx.now_local().strftime("%Y-%m-%d %H:%M (%A)"),
        "whatsapp_connections": [connection_info(token) for token in await active_connections(ctx)],
        "messages": {name: sum(counts.get(status, 0) for status in statuses) for name, statuses in STATUS_GROUPS.items()},
        "note": "All dates and times you send or receive are in this timezone.",
    }


async def list_recipients(ctx: Context, args: ListRecipients) -> dict:
    token = await pick_connection(ctx, args.connection_id)
    needle = args.search.strip().casefold()
    saved = (await ctx.db.execute(select(RecipientList).where(
        RecipientList.user_id == ctx.user.id, RecipientList.token_id == token.id,
    ).order_by(RecipientList.name))).scalars().all()
    result = {
        "connection": connection_info(token),
        "saved_lists": [{"id": row.id, "name": row.name, "recipients": json.loads(row.recipients)}
                        for row in saved if needle in row.name.casefold()],
    }
    try:
        groups = [group for group in await fetch_groups(token) if needle in group["name"].casefold()]
        result["groups"] = groups[:200]
        if len(groups) > 200:
            result["note"] = "Only the first 200 groups are shown. Use search to narrow them down."
    except ToolError as exc:
        result["groups"] = []
        result["groups_error"] = str(exc)
    return result


async def list_messages(ctx: Context, args: ListMessages) -> dict:
    query = select(Job).where(Job.user_id == ctx.user.id).options(selectinload(Job.job_groups))
    if args.status != "all":
        query = query.where(Job.status.in_(STATUS_GROUPS[args.status]))
    if args.search.strip():
        like = f"%{args.search.strip()}%"
        query = query.where(Job.label.ilike(like) | Job.group_name.ilike(like) | Job.message.ilike(like))
    jobs = (await ctx.db.execute(query.order_by(Job.created_at.desc(), Job.id.desc()).limit(args.limit))).scalars().all()
    return {"timezone": ctx.tz, "messages": [summary(job, ctx) for job in jobs]}


async def get_message(ctx: Context, args: MessageId) -> dict:
    job = await owned_job(ctx, args.message_id)
    details = summary(job, ctx)
    details["message"] = job.message
    details["recipients"] = [{"id": group.group_id, "name": group.group_name or group.group_id} for group in job.job_groups]
    details["times_sent"] = job.send_count
    details["created_at"] = ctx.local(job.created_at)
    if job.trigger_type == "dates":
        now = datetime.now(timezone.utc)
        details["dates"] = [{"at": ctx.local(date), "passed": date <= now} for date in parse_dates(job)]
    if job.image_filename:
        details["attachment"] = job.image_filename
    run_id = await ctx.db.scalar(select(DeliveryAttempt.run_id).where(DeliveryAttempt.job_id == job.id)
                                 .order_by(DeliveryAttempt.id.desc()).limit(1))
    if run_id:
        attempts = (await ctx.db.execute(select(DeliveryAttempt).where(
            DeliveryAttempt.job_id == job.id, DeliveryAttempt.run_id == run_id))).scalars().all()
        counts: dict[str, int] = {}
        problems = []
        for attempt in attempts:
            counts[attempt.status] = counts.get(attempt.status, 0) + 1
            if attempt.status in {"failed", "unknown"}:
                problems.append({"recipient": attempt.group_name or attempt.group_id, "status": attempt.status,
                                 "details": (attempt.response or "")[:300]})
        details["last_delivery"] = {"counts": counts, "problems": problems[:20]}
    return details


async def create_message(ctx: Context, args: CreateMessage) -> dict:
    token = await pick_connection(ctx, args.connection_id)
    trigger_type, fields = schedule_fields(args, ctx)
    try:
        trigger_value = job_routes.build_trigger_value(trigger_type, ctx.tz, **fields)
    except (ValueError, zoneinfo.ZoneInfoNotFoundError) as exc:
        raise ToolError(str(exc)) from exc
    recipients = await resolve_recipients(ctx, token, args.recipients, args.recipient_list_ids)
    try:
        job_routes.validate_job_payload(token, list(recipients), args.message, None)
    except HTTPException as exc:
        raise ToolError(exc.detail) from exc

    first_id, first_name = next(iter(recipients.items()))
    job = Job(
        user_id=ctx.user.id,
        token_id=token.id,
        label=(args.label or "").strip() or None,
        group_id=first_id,
        group_name=first_name,
        message=args.message,
        trigger_type=trigger_type,
        **job_routes.split_schedule(trigger_type, trigger_value),
        schedule_timezone=ctx.tz,
        status="pending",
    )
    try:
        ctx.db.add(job)
        await ctx.db.flush()
        for recipient_id, name in recipients.items():
            ctx.db.add(JobGroup(job_id=job.id, group_id=recipient_id, group_name=name))
        await register_job(job)
        await ctx.db.commit()
    except ValueError as exc:
        await ctx.db.rollback()
        raise ToolError(str(exc)) from exc
    except Exception:
        await ctx.db.rollback()
        raise
    job = await owned_job(ctx, job.id)
    return {"created": True, **summary(job, ctx), "message": job.message}


async def pause_message(ctx: Context, args: MessageId) -> dict:
    job = await owned_job(ctx, args.message_id)
    if not await job_routes.pause(job):
        raise ToolError(f'Message {job.id} is "{job.status}", so it can\'t be paused. Only scheduled messages can.')
    await ctx.db.commit()
    return {"paused": True, **summary(job, ctx)}


async def resume_message(ctx: Context, args: MessageId) -> dict:
    job = await owned_job(ctx, args.message_id)
    try:
        resumed = await job_routes.resume(job)
    except ValueError as exc:
        raise ToolError(f"Message {job.id} can't be resumed: {exc}. Create a new message with a new time instead.") from exc
    if not resumed:
        raise ToolError(f'Message {job.id} is "{job.status}", not paused.')
    await ctx.db.commit()
    return {"resumed": True, **summary(job, ctx)}


async def cancel_message(ctx: Context, args: MessageId) -> dict:
    job = await owned_job(ctx, args.message_id)
    if not await job_routes.cancel(job):
        raise ToolError(f'Message {job.id} is "{job.status}", so there is nothing to cancel.')
    await ctx.db.commit()
    return {"cancelled": True, **summary(job, ctx)}


READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}

# name -> (function, arguments model, title, description, annotations)
TOOLS = {
    "get_account_overview": (get_account_overview, NoArguments, "Account overview",
        "The account's timezone, current local time, WhatsApp connections and how many messages are upcoming, paused, finished or failed. "
        "Call this first: every date and time in EchoSend uses the account timezone.", READ_ONLY),
    "list_recipients": (list_recipients, ListRecipients, "List recipients",
        "WhatsApp groups and saved recipient lists that messages can be sent to.", READ_ONLY),
    "list_messages": (list_messages, ListMessages, "List messages",
        "Scheduled and past messages with their recipients, schedule, status and next send time.", READ_ONLY),
    "get_message": (get_message, MessageId, "Message details",
        "One message in full: its text, every recipient, its schedule and how the last delivery went.", READ_ONLY),
    "create_message": (create_message, CreateMessage, "Create message",
        "Create a WhatsApp text message that EchoSend sends from the user's WhatsApp: right away, once, on a repeating schedule, "
        "or on specific dates. Confirm the text, recipients and time with the user before calling this. "
        "Attachments and message series can only be set up on the website.",
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True}),
    "pause_message": (pause_message, MessageId, "Pause message",
        "Stop a scheduled message from sending until it is resumed.",
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}),
    "resume_message": (resume_message, MessageId, "Resume message",
        "Schedule a paused message again.",
        {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}),
    "cancel_message": (cancel_message, MessageId, "Cancel message",
        "Stop a message for good. It can't be resumed afterwards, only copied on the website.",
        {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}),
}
