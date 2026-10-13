import asyncio
import json
from uuid import uuid4

import httpx
from sqlalchemy import select

from app.models.delivery_attempt import DeliveryAttempt
from app.models.job import Job
from app.models.recipient_list import RecipientList
from app.routers import jobs
from test_native_api import exercise, RECIPIENT


def test_saved_recipient_lists_validate_deduplicate_and_isolate_accounts(tmp_path, monkeypatch):
    async def scenario(client, sessions, context):
        headers = {"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"}
        payload = {"token_id": 3, "name": "All branches", "recipients": [
            {"id": RECIPIENT, "name": "One"}, {"id": RECIPIENT, "name": "One updated"},
            {"id": "+201234567890", "name": "Two"},
        ]}
        assert (await client.post('/jobs/recipient-lists', json=payload)).status_code == 403
        assert (await client.post('/jobs/recipient-lists', json={**payload, "name": " "}, headers=headers)).status_code == 400
        for invalid in ['bad', 'ع' * 128]:
            assert (await client.post('/jobs/recipient-lists', json={**payload, "recipients": [{"id": invalid, "name": "Bad"}]}, headers=headers)).status_code == 400
        result = await client.post('/jobs/recipient-lists', json=payload, headers=headers)
        assert result.status_code == 201
        row = result.json()
        assert len(row['recipients']) == 2 and row['recipients'][0]['name'] == 'One updated'
        assert (await client.get('/jobs/recipient-lists?token_id=3')).json()['lists'] == [row]
        assert (await client.get('/jobs/recipient-lists?token_id=999')).status_code == 400
        assert (await client.delete(f"/jobs/recipient-lists/{row['id']}")).status_code == 403
        with monkeypatch.context() as patch:
            patch.setattr(jobs, 'require_user', lambda request: {"sub": "8"})
            assert (await client.get('/jobs/recipient-lists?token_id=3')).status_code == 400
            assert (await client.delete(f"/jobs/recipient-lists/{row['id']}", headers=headers)).status_code == 404
        assert (await client.delete(f"/jobs/recipient-lists/{row['id']}", headers=headers)).status_code == 200
        async with sessions() as db:
            assert not (await db.execute(select(RecipientList))).scalars().all()

    asyncio.run(exercise(tmp_path, monkeypatch, lambda r: httpx.Response(500), scenario))


def test_progress_tracks_latest_run_and_is_private(tmp_path, monkeypatch):
    async def scenario(client, sessions, context):
        async with sessions() as db:
            job = Job(user_id=7, token_id=3, group_id=RECIPIENT, message='Hello', trigger_type='now', trigger_value='', status='queued')
            db.add(job); await db.flush()
            job_id = job.id
            old_run, current_run = str(uuid4()), str(uuid4())
            db.add(DeliveryAttempt(job_id=job_id, run_id=old_run, group_id=RECIPIENT, status='sent'))
            await db.flush()
            for state in ['sent','queued','processing','failed','unknown']:
                db.add(DeliveryAttempt(job_id=job_id, run_id=current_run, group_id=RECIPIENT, group_name=state,
                                       status=state, response=json.dumps({'error': 'No acknowledgement'}) if state == 'unknown' else None))
            await db.commit()
        response = await client.get(f'/jobs/{job_id}/progress')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store, private'
        data = response.json()
        assert data['run_id'] == current_run and data['total'] == 5
        assert data['counts'] == {'sent': 1, 'pending': 2, 'failed': 1, 'unknown': 1}
        assert data['recipients'][-1]['error'] == 'No acknowledgement'
        async with sessions() as db:
            attempts = (await db.execute(select(DeliveryAttempt).where(DeliveryAttempt.run_id == current_run))).scalars().all()
            for attempt in attempts: attempt.status = 'sent'
            await db.commit()
        assert (await client.get(f'/jobs/{job_id}/progress')).json()['counts']['sent'] == 5
        assert (await client.get('/jobs/999/progress')).status_code == 404
        with monkeypatch.context() as patch:
            patch.setattr(jobs, 'require_user', lambda request: {'sub': '8'})
            assert (await client.get(f'/jobs/{job_id}/progress')).status_code == 404
        page = await client.get(f'/jobs/{job_id}')
        assert page.status_code == 200 and 'delivery-progress.js?v=' in page.text

    asyncio.run(exercise(tmp_path, monkeypatch, lambda r: httpx.Response(500), scenario))
