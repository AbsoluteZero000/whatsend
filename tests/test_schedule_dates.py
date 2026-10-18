import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.models.job import Job
from app.services import native_jobs, scheduler
from app.services.schedule_dates import (
    DatesTrigger, build_dates_value, local_form_values, parse_dates, remaining_dates,
)
from test_native_api import exercise, RECIPIENT

HEADERS = {'Cookie': 'csrf_token=safe', 'X-CSRF-Token': 'safe'}
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
FMT = '%Y-%m-%d %H:%M'


def utc(value):
    return datetime.strptime(value, FMT).replace(tzinfo=timezone.utc)


def stamp(dt):
    return dt.strftime(FMT)


def test_each_picked_day_converts_to_utc_on_its_own_across_daylight_saving():
    # New York moves its clocks forward on 2026-03-08: 09:00 is 14:00 UTC before and 13:00 UTC after.
    value = build_dates_value('2026-03-09, 2026-03-07', '09:00', 'America/New_York', now=NOW)
    assert json.loads(value) == ['2026-03-07 14:00', '2026-03-09 13:00']


def test_duplicate_days_are_merged():
    assert json.loads(build_dates_value('2026-02-01,2026-02-01', '08:30', 'Africa/Cairo', now=NOW)) == ['2026-02-01 06:30']


@pytest.mark.parametrize('days,time,message', [
    ('', '09:00', 'Pick at least one date'),
    ('2026-02-01', '25:00', 'valid time'),
    ('2026-02-30', '09:00', 'not valid'),
    ('2025-12-31', '09:00', 'already passed'),
    ('2026-01-01', '06:00', 'already passed'),           # today, but 06:00 New York (11:00 UTC) has gone
    ('2026-03-08', '02:30', 'daylight-saving'),          # skipped hour in New York
    (','.join(f'2026-{m:02d}-{d:02d}' for m in range(2, 8) for d in range(1, 20)), '09:00', 'up to 100'),
])
def test_invalid_picks_are_rejected_with_a_clear_message(days, time, message):
    with pytest.raises(ValueError, match=message):
        build_dates_value(days, time, 'America/New_York', now=NOW)


def test_trigger_fires_once_per_date_then_stops():
    dates = [utc('2026-02-01 09:00'), utc('2026-03-01 09:00'), utc('2026-04-01 09:00')]
    trigger = DatesTrigger(list(reversed(dates)))
    fired, previous = [], None
    while (next_time := trigger.get_next_fire_time(previous, NOW)) is not None:
        fired.append(next_time)
        previous = next_time
    assert fired == dates


def test_schedule_skips_dates_that_have_passed():
    now = datetime.now(timezone.utc)
    job = SimpleNamespace(trigger_type='dates', schedule_timezone='UTC', schedule_dates=json.dumps(
        [stamp(now - timedelta(days=2)), stamp(now + timedelta(days=1)), stamp(now + timedelta(days=3))]))
    trigger = scheduler.build_trigger(job)
    assert [stamp(date) for date in trigger.run_dates] == json.loads(job.schedule_dates)[1:]
    assert not scheduler.is_last_run(job)
    job.schedule_dates = json.dumps([stamp(now - timedelta(days=2))])
    assert scheduler.is_last_run(job)
    with pytest.raises(ValueError, match='passed'):
        scheduler.build_trigger(job)


def test_edit_form_reopens_with_local_days_and_time():
    job = SimpleNamespace(schedule_dates=json.dumps(['2026-03-07 14:00', '2026-03-09 13:00']))
    assert local_form_values(job, 'America/New_York') == {'schedule_dates': '2026-03-07,2026-03-09', 'dates_time': '09:00'}


def future_day(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime('%Y-%m-%d')


def test_create_send_and_finish_a_specific_dates_message(tmp_path, monkeypatch):
    calls = []

    def worker(request):
        if request.method == 'POST':
            calls.append(json.loads(request.content)['text'])
            return httpx.Response(202, json={'data': {'id': str(uuid4()), 'status': 'pending'}})
        return httpx.Response(200, json={'data': {'id': request.url.path.rsplit('/', 1)[-1], 'status': 'sent'}})

    async def scenario(client, sessions, context):
        picked = f'{future_day(3)},{future_day(10)}'
        created = await client.post('/jobs/create', headers=HEADERS, data={
            'token_id': '3', 'group_ids': RECIPIENT, 'message': 'Hello #{send_number}',
            'trigger_type': 'dates', 'schedule_dates': picked, 'dates_time': '09:00',
        })
        assert created.status_code == 303, created.text
        async with sessions() as db:
            job = (await db.execute(select(Job))).scalar_one()
            job_id = job.id
            assert job.trigger_type == 'dates' and job.trigger_value == ''
            assert [d.strftime('%Y-%m-%d') for d in parse_dates(job)] == picked.split(',')
            assert job.status == 'active'

        edit_page = await client.get(f'/jobs/{job_id}/edit')
        assert f'value="{picked}"' in edit_page.text and 'value="dates" selected' in edit_page.text

        # First date sends; another one is still to come, so the message stays active.
        await scheduler.send_job(job_id)
        await native_jobs.poll_deliveries()
        async with sessions() as db:
            assert (await db.get(Job, job_id)).status == 'active'

        # Once the last date has gone out, the message is completed.
        async with sessions() as db:
            job = await db.get(Job, job_id)
            job.schedule_dates = json.dumps([stamp(datetime.now(timezone.utc) - timedelta(minutes=1))])
            await db.commit()
        await scheduler.send_job(job_id)
        await native_jobs.poll_deliveries()
        async with sessions() as db:
            assert (await db.get(Job, job_id)).status == 'completed'
        assert calls == ['Hello #1', 'Hello #2']

        # A copy of a finished dates message waits for new dates instead of failing.
        cloned = await client.post(f'/jobs/{job_id}/clone', headers=HEADERS)
        assert cloned.status_code == 303 and '/edit?' in cloned.headers['location'] and 'Pick+new+dates' in cloned.headers['location']
        async with sessions() as db:
            clone = (await db.execute(select(Job).where(Job.id != job_id))).scalar_one()
            assert clone.status == 'paused' and clone.schedule_dates == job.schedule_dates

    asyncio.run(exercise(tmp_path, monkeypatch, worker, scenario))


def test_past_dates_are_rejected_when_creating(tmp_path, monkeypatch):
    async def scenario(client, sessions, context):
        response = await client.post('/jobs/create', headers=HEADERS, data={
            'token_id': '3', 'group_ids': RECIPIENT, 'message': 'Hi',
            'trigger_type': 'dates', 'schedule_dates': '2020-01-01', 'dates_time': '09:00',
        })
        assert response.status_code == 400 and 'has already passed' in response.text
        assert 'value="2020-01-01"' in response.text  # the form keeps what was picked
        async with sessions() as db:
            assert (await db.execute(select(Job))).first() is None

    asyncio.run(exercise(tmp_path, monkeypatch, lambda request: httpx.Response(500), scenario))
