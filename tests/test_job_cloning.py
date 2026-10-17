import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.job import Job, JobGroup
from app.models.job_series import SeriesEntry
from app.models.token import Token
from app.routers import jobs
from app.services import scheduler, native_jobs
from test_message_series import HEADERS, create_series, pdf_bytes
from test_native_api import exercise, RECIPIENT


@pytest.mark.parametrize('provider', ['native', 'whatsapp'])
@pytest.mark.parametrize('trigger', ['cron', 'trigger'])
@pytest.mark.parametrize('layout', ['sequential', 'parallel', 'lines'])
def test_clone_mixed_plan_restarts_all_sources(tmp_path, monkeypatch, provider, trigger, layout):
    registered, sent = [], []

    async def register(job):
        registered.append(job.id)

    monkeypatch.setattr(jobs, 'register_job', register)
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path)

    def worker(request):
        if request.method == 'POST':
            if 'application/json' in request.headers.get('content-type', ''):
                sent.append(json.loads(request.content)['text' if provider == 'native' else 'body'])
            return httpx.Response(202, json={'data': {'id': str(uuid4()), 'status': 'pending'}})
        return httpx.Response(200, json={'data': {'id': request.url.path.rsplit('/', 1)[-1], 'status': 'sent'}})

    async def scenario(client, sessions, context):
        async with sessions() as db:
            token = await db.get(Token, 3)
            token.provider = provider
            await db.commit()
        asset = (await client.post('/jobs/series-files', files={'file': ('Book.pdf', pdf_bytes(1), 'application/pdf')}, headers=HEADERS)).json()
        definition = {'strategy': 'parallel' if layout == 'parallel' else 'sequential', 'footer': 'Footer {send_ordinal_ar}', 'sources': [
            {'kind': 'messages', 'name': 'Notes', 'message_template': '{source_name}: {content}', 'entries': [{'message': 'Note'}]},
            {'kind': 'pdf', 'name': 'Book', 'message_template': '{book_name} {start_page}-{end_page}', 'range_mode': 'custom',
             'ranges': [{'start': 1, 'end': 1}], 'entries': [{'message': 'Pages 1-1', 'start_page': 1, 'end_page': 1, 'attachments': [asset]}]},
            {'kind': 'playlist', 'name': 'Video', 'source_url': 'https://youtu.be/abcdefghijk',
             'entries': [{'message': 'Video\nhttps://youtu.be/abcdefghijk'}]},
        ]}
        if layout == 'lines':
            definition['lanes'] = [{'id': 'one', 'name': 'Reading'}, {'id': 'two', 'name': 'Media'}]
            for index, source in enumerate(definition['sources']):
                source['lane_id'] = 'one' if index < 2 else 'two'
        assert (await create_series(client, definition, trigger_type=trigger, group_ids=RECIPIENT, message='Header {send_number}')).status_code == 303
        async with sessions() as db:
            original = (await db.execute(select(Job))).scalar_one()
            original_id = original.id
            original.series_cursor, original.send_count = 1, 5
            original.series_active_run, original.status = 'old-run', 'queued'
            original.skip_count, original.retry_count = 2, 2
            original.retry_at, original.retry_group_ids = 'old', '["old"]'
            # Older saved plans did not always include empty attachment arrays.
            saved = json.loads(original.series_plan)
            for source in saved['sources']:
                for entry in source['entries']:
                    if not entry['attachments']:
                        del entry['attachments']
            original.series_plan = json.dumps(saved)
            entries = (await db.execute(select(SeriesEntry).where(SeriesEntry.job_id == original_id))).scalars().all()
            entries[0].status, entries[0].run_id = 'sent', 'old-run'
            await db.commit()
        assert (await client.post(f'/jobs/{original_id}/clone', headers=HEADERS)).status_code == 303
        async with sessions() as db:
            clone = (await db.execute(select(Job).where(Job.id != original_id).options(selectinload(Job.series_entries).selectinload(SeriesEntry.attachments), selectinload(Job.job_groups)))).scalar_one()
            clone_id = clone.id
            assert clone.message_mode == 'series' and clone.series_total == len(clone.series_entries)
            assert (clone.series_cursor, clone.send_count, clone.skip_count, clone.retry_count) == (0, 0, 0, 0)
            assert clone.series_active_run is clone.retry_at is clone.retry_group_ids is None
            assert clone.status == ('trigger' if trigger == 'trigger' else 'pending')
            assert [group.group_id for group in clone.job_groups] == [RECIPIENT]
            assert all(entry.status == 'pending' and entry.run_id is None for entry in clone.series_entries)
            copied = [attachment for entry in clone.series_entries for attachment in entry.attachments]
            assert len(copied) == 1 and copied[0].id != asset['id']
            saved['sources'][1]['entries'][0]['attachments'][0]['id'] = copied[0].id
            assert json.loads(clone.series_plan) == saved
        assert registered == [original_id, clone_id]
        edit_page = await client.get(f'/jobs/{clone_id}/edit')
        assert edit_page.status_code == 200
        assert 'id="series_footer"' in edit_page.text
        await client.post(f'/jobs/{original_id}/delete', headers=HEADERS)
        for _ in range(clone.series_total):
            await scheduler.send_job(clone_id)
            if provider == 'native':
                await native_jobs.poll_deliveries()
        assert sent and sent[0].startswith('Header 1\n\nNotes: Note')
        assert sent[0].endswith('Footer الأول')
        assert any('Book 1-1' in text for text in sent)
        assert any('https://youtu.be/abcdefghijk' in text for text in sent)
        async with sessions() as db:
            assert (await db.get(Job, clone_id)).status == 'completed'
    asyncio.run(exercise(tmp_path, monkeypatch, worker, scenario))


@pytest.mark.parametrize('trigger', ['now', 'date', 'expired', 'cron', 'trigger'])
@pytest.mark.parametrize('media', [False, True])
def test_clone_repeat_modes_and_expired_dates(tmp_path, monkeypatch, trigger, media):
    registrations = []

    async def register(job):
        if job.status != 'paused':
            registrations.append(scheduler.build_trigger(job))

    monkeypatch.setattr(jobs, 'register_job', register)

    async def scenario(client, sessions, context):
        path = tmp_path / 'attachment.pdf'
        path.write_bytes(pdf_bytes(1))
        date = datetime.now(timezone.utc) + timedelta(days=-1 if trigger == 'expired' else 1)
        value = date.strftime('%Y-%m-%d %H:%M') if trigger in {'date', 'expired'} else '0 9 * * *' if trigger == 'cron' else ''
        async with sessions() as db:
            original = Job(user_id=7, token_id=3, label='x' * 120, group_id=RECIPIENT, group_name='Group',
                           message='Lesson {send_number} {send_ordinal_ar}', trigger_type='date' if trigger == 'expired' else trigger,
                           trigger_value=value, schedule_timezone='Africa/Cairo', status='completed', send_count=9,
                           image_path=str(path) if media else None, image_filename='Book.pdf' if media else None)
            db.add(original)
            await db.flush()
            db.add(JobGroup(job_id=original.id, group_id=RECIPIENT, group_name='Group'))
            await db.commit()
            original_id = original.id
        response = await client.post(f'/jobs/{original_id}/clone', headers=HEADERS)
        assert response.status_code == 303
        async with sessions() as db:
            clone = (await db.execute(select(Job).where(Job.id != original_id))).scalar_one()
            assert clone.message_mode == 'repeat' and clone.message == original.message
            assert clone.trigger_type == original.trigger_type and clone.trigger_value == value
            assert clone.schedule_timezone == 'Africa/Cairo' and clone.send_count == 0
            assert clone.image_path == original.image_path and clone.image_filename == original.image_filename
            assert len(clone.label) == 120 and clone.label.endswith(' (copy)')
            if trigger == 'expired':
                assert clone.status == 'paused' and not registrations
                assert response.headers['location'].startswith(f'/jobs/{clone.id}/edit?')
            else:
                assert len(registrations) == 1
                assert clone.status == ('trigger' if trigger == 'trigger' else 'pending')
        if media:
            await client.post(f'/jobs/{original_id}/delete', headers=HEADERS)
            assert path.is_file()
    asyncio.run(exercise(tmp_path, monkeypatch, lambda request: httpx.Response(500), scenario))


def test_clone_failure_does_not_leave_partial_copy(tmp_path, monkeypatch):
    async def fail_registration(job):
        raise ValueError('Invalid schedule')
    monkeypatch.setattr(jobs, 'register_job', fail_registration)

    async def scenario(client, sessions, context):
        async with sessions() as db:
            original = Job(user_id=7, token_id=3, group_id=RECIPIENT, message='Text', trigger_type='cron', trigger_value='invalid')
            db.add(original)
            await db.commit()
        with pytest.raises(ValueError, match='Invalid schedule'):
            await client.post(f'/jobs/{original.id}/clone', headers=HEADERS)
        async with sessions() as db:
            assert len((await db.execute(select(Job))).scalars().all()) == 1
        assert (await client.post('/jobs/999/clone', headers=HEADERS)).status_code == 404
    asyncio.run(exercise(tmp_path, monkeypatch, lambda request: httpx.Response(500), scenario))
