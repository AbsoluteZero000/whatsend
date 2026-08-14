import pytest

from app.models.job import Job
from app.routers.jobs import build_trigger_value, local_to_utc
from app.services.scheduler import build_trigger


def test_recurring_schedule_stays_in_local_timezone():
    expression = build_trigger_value("cron", "America/New_York", cron_time="09:00", cron_freq="daily")
    assert expression == "00 09 * * *"
    job = Job(
        id=1, user_id=1, token_id=1, group_id="g", message="hello",
        trigger_type="cron", trigger_value=expression, schedule_timezone="America/New_York",
    )
    trigger = build_trigger(job)
    assert str(trigger.timezone) == "America/New_York"


def test_nonexistent_dst_time_is_rejected():
    with pytest.raises(ValueError, match="does not exist"):
        local_to_utc("2026-03-08 02:30", "America/New_York")


def test_invalid_raw_cron_is_rejected():
    with pytest.raises(ValueError):
        build_trigger_value("cron", "UTC", cron_freq="raw", cron_raw="not a cron")


def test_one_time_date_converts_to_utc():
    assert local_to_utc("2026-08-14 12:00", "Africa/Cairo") == "2026-08-14 09:00"
