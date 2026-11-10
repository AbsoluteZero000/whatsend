import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.job import Job
from app.models.delivery_attempt import DeliveryAttempt
from app.models.log import Log
from app.models.token import Token
from app.providers.signal.sender import connection_token
from app.routers import jobs
from app.services import scheduler
from app.services.crypto import encrypt_token
from test_native_api import exercise
from test_native_jobs import no_schedule
from test_message_series import create_series, plan
from test_telegram import TOKEN

HEADERS = {'Cookie': 'csrf_token=safe', 'X-CSRF-Token': 'safe'}


@pytest.mark.parametrize('provider', ['telegram', 'signal'])
@pytest.mark.parametrize('mode', ['repeat', 'series'])
def test_provider_credentials_api_media_scheduling_and_series(tmp_path, monkeypatch, provider, mode):
    monkeypatch.setattr(settings, 'signal_api_url', 'https://signal.internal')
    monkeypatch.setattr(jobs, 'register_job', no_schedule)
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path)
    secret = TOKEN if provider == 'telegram' else connection_token('+201234567890')
    recipient = '-1001234567' if provider == 'telegram' else '+201111111111'
    calls = []
    def handler(request):
        if request.method == 'GET':
            assert provider == 'signal'
            return httpx.Response(200, json=[{'id': 'group.YWJj', 'name': 'Team'}])
        calls.append(request)
        if provider == 'telegram':
            assert request.url.host == 'api.telegram.org'
            if request.url.path.endswith('sendDocument'):
                assert b'filename="Original.pdf"' in request.content
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': len(calls)}})
        assert request.url.host == 'signal.internal'
        payload = json.loads(request.content)
        assert payload['number'] == '+201234567890' and payload['recipients'] == [recipient]
        return httpx.Response(201, json={'timestamp': '1730000000000'})

    async def scenario(client, sessions, context):
        invalid = await client.post('/tokens/create', headers=HEADERS,
            data={'provider': provider, 'api_token': 'invalid'})
        assert invalid.status_code == 400
        saved = await client.post('/tokens/create', headers=HEADERS,
            data={'provider': provider, 'name': provider, 'api_token': secret})
        assert saved.status_code == 303
        async with sessions() as db:
            token = (await db.execute(select(Token).where(Token.provider == provider))).scalar_one()
            assert token.api_token != secret
            original = await db.get(Token, 3)
            original.provider, original.api_token = provider, token.api_token
            await db.commit()
        from app.services import assistant_tools
        async with sessions() as db:
            token = await db.get(Token, 3)
            ctx = assistant_tools.Context(db, context.user, 'http://test')
            assert await assistant_tools.resolve_recipients(ctx, token, [recipient], []) == {recipient: recipient}
            assert assistant_tools.connection_info(token)['type'] == provider.capitalize()
        groups = (await client.get('/api/groups')).json()['groups']
        assert groups == ([] if provider == 'telegram' else [{'id': 'group.YWJj', 'name': 'Team'}])
        response = await client.post('/api/send', json={'number': recipient, 'message': 'API text', 'provider': provider})
        assert response.status_code == 200, response.text
        media = await client.post('/api/send/media', data={'group_id': recipient, 'caption': 'API file'},
            files={'file': ('Original.pdf', b'%PDF-1.7 file', 'application/pdf')})
        assert media.status_code == 200, media.text
        assert not list(tmp_path.glob('*.pdf'))
        if mode == 'series':
            response = await create_series(client, plan(['Scheduled text']), group_ids=recipient)
        else:
            response = await client.post('/jobs/create', headers=HEADERS, data={
                'token_id': '3', 'group_ids': recipient, 'message': 'Scheduled text', 'trigger_type': 'trigger'})
        assert response.status_code == 303, response.text
        async with sessions() as db:
            job_id = (await db.execute(select(Job.id))).scalar_one()
        await scheduler.send_job(job_id)
        async with sessions() as db:
            attempt = (await db.execute(select(DeliveryAttempt))).scalar_one()
            assert attempt.status == 'sent'
        assert len(calls) == 3
    asyncio.run(exercise(tmp_path, monkeypatch, handler, scenario))


@pytest.mark.parametrize('provider', ['telegram', 'signal'])
def test_network_failure_records_unknown_without_a_blind_retry(tmp_path, monkeypatch, provider):
    monkeypatch.setattr(settings, 'signal_api_url', 'https://signal.internal')
    monkeypatch.setattr(jobs, 'register_job', no_schedule)
    credential = TOKEN if provider == 'telegram' else connection_token('+201234567890')
    recipient = '12345' if provider == 'telegram' else '+201111111111'
    def handler(request):
        raise httpx.ReadTimeout(f'Cannot confirm {credential}', request=request)
    async def scenario(client, sessions, context):
        async with sessions() as db:
            token = await db.get(Token, 3)
            token.provider, token.api_token = provider, encrypt_token(credential)
            await db.commit()
        response = await client.post('/jobs/create', headers=HEADERS, data={
            'token_id': '3', 'group_ids': recipient, 'message': 'hello', 'trigger_type': 'now'})
        assert response.status_code == 303
        async with sessions() as db: job_id = (await db.execute(select(Job.id))).scalar_one()
        await scheduler.send_job(job_id)
        async with sessions() as db:
            job = await db.get(Job, job_id)
            assert job.status == 'unknown' and job.retry_at is None
            attempt = (await db.execute(select(DeliveryAttempt))).scalar_one()
            log = (await db.execute(select(Log))).scalar_one()
            assert attempt.status == log.status == 'unknown'
            assert credential not in attempt.response and credential not in log.response
        assert scheduler.scheduler.get_job(f'retry:{job_id}') is None
    asyncio.run(exercise(tmp_path, monkeypatch, handler, scenario))
