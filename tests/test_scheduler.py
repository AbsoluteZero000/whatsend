from app.services.scheduler import MISFIRE_GRACE_SECONDS, scheduler


def test_scheduler_allows_short_runtime_delays():
    assert scheduler._job_defaults["misfire_grace_time"] == MISFIRE_GRACE_SECONDS
    assert MISFIRE_GRACE_SECONDS >= 60


def test_scheduler_keeps_single_pending_run_per_job():
    assert scheduler._job_defaults["coalesce"] is True
    assert scheduler._job_defaults["max_instances"] == 1
