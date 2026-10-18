"""Hand-picked send dates.

A "dates" job stores its send times in ``Job.schedule_dates`` as a JSON list of UTC
"YYYY-MM-DD HH:MM" strings. The user picks calendar days in their timezone plus one time;
each day is converted to UTC on its own, so daylight-saving changes between dates are respected.
"""
import json
import zoneinfo
from datetime import datetime, timedelta, timezone

from apscheduler.triggers.base import BaseTrigger

MAX_DATES = 100
FORMAT = "%Y-%m-%d %H:%M"


def parse_dates(job) -> list[datetime]:
    """All of the job's send times as sorted, timezone-aware UTC datetimes."""
    try:
        values = json.loads(job.schedule_dates or "[]")
    except (TypeError, ValueError):
        return []
    dates = []
    for value in values if isinstance(values, list) else []:
        try:
            dates.append(datetime.strptime(str(value), FORMAT).replace(tzinfo=timezone.utc))
        except ValueError:
            continue
    return sorted(set(dates))


def remaining_dates(job, now: datetime | None = None, grace_seconds: int = 0) -> list[datetime]:
    """Send times that have not passed yet (optionally allowing a short misfire grace)."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=grace_seconds)
    return [date for date in parse_dates(job) if date > cutoff]


def build_dates_value(local_dates: str, local_time: str, tz_name: str, now: datetime | None = None) -> str:
    """Validate picked days ("YYYY-MM-DD,YYYY-MM-DD") and a time ("HH:MM") in the user's timezone."""
    try:
        hour, minute = (int(part) for part in (local_time or "").split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise ValueError("Choose a valid time for the picked dates")
    tz = zoneinfo.ZoneInfo(tz_name)
    days = sorted({day.strip() for day in (local_dates or "").split(",") if day.strip()})
    if not days:
        raise ValueError("Pick at least one date on the calendar")
    if len(days) > MAX_DATES:
        raise ValueError(f"You can pick up to {MAX_DATES} dates")
    now = now or datetime.now(timezone.utc)
    result = []
    for day in days:
        try:
            local = datetime.strptime(f"{day} {hour:02d}:{minute:02d}", FORMAT)
        except ValueError:
            raise ValueError("One of the picked dates is not valid")
        utc = local.replace(tzinfo=tz).astimezone(timezone.utc)
        if utc.astimezone(tz).replace(tzinfo=None) != local:
            raise ValueError(f"{day} at {local_time} does not exist because of a daylight-saving change. Choose another time.")
        if utc <= now:
            raise ValueError(f"{day} at {local_time} has already passed. Remove it or choose a later time.")
        result.append(utc.strftime(FORMAT))
    return json.dumps(result)


def local_form_values(job, tz_name: str) -> dict:
    """The picked days and time in the user's timezone, for re-opening the edit form."""
    dates = parse_dates(job)
    if not dates:
        return {"schedule_dates": "", "dates_time": "09:00"}
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except zoneinfo.ZoneInfoNotFoundError:
        tz = timezone.utc
    local = [date.astimezone(tz) for date in dates]
    return {"schedule_dates": ",".join(date.strftime("%Y-%m-%d") for date in local),
            "dates_time": local[-1].strftime("%H:%M")}


class DatesTrigger(BaseTrigger):
    """Fires once at each listed time, in order. (APScheduler's OrTrigger of DateTriggers stops after one.)"""

    def __init__(self, run_dates: list[datetime]):
        self.run_dates = sorted(run_dates)

    def get_next_fire_time(self, previous_fire_time, now):
        for run_date in self.run_dates:
            if previous_fire_time is None or run_date > previous_fire_time:
                return run_date
        return None

    def __str__(self):
        return f"dates[{len(self.run_dates)}]"
