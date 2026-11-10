import asyncio
import json
import re
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.token import Token
from app.providers.base import ProviderError
from app.providers.tiktok import jobs as publishing
from app.providers.tiktok.client import TikTokPublisher
from app.providers.tiktok.media import video_duration
from app.providers.tiktok.models import TikTokPost
from app.providers.tiktok.routes import photo_sources
from app.routers import jobs
from app.services.crypto import encrypt_token
from test_native_api import exercise
from test_telegram import transport

HEADERS = {'Cookie': 'csrf_token=safe', 'X-CSRF-Token': 'safe'}
CREATOR = {'creator_username': 'test_creator', 'creator_nickname': 'Test Creator',
    'privacy_level_options': ['SELF_ONLY', 'PUBLIC_TO_EVERYONE'], 'max_video_post_duration_sec': 60,
    'comment_disabled': False, 'duet_disabled': True, 'stitch_disabled': False}


def box(kind, data):
    return struct.pack('>I4s', len(data) + 8, kind) + data


def mp4(seconds=15):
    return box(b'ftyp', b'isom\x00\x00\x00\x00isommp42') + box(b'moov', box(b'mvhd', b'\x00' * 12 + struct.pack('>II', 1000, seconds * 1000)))


def ok(data):
    return httpx.Response(200, json={'data': data, 'error': {'code': 'ok'}})


async def select_tiktok(sessions):
    async with sessions() as db:
        token = await db.get(Token, 3)
        token.provider, token.api_token = 'tiktok', encrypt_token('act.test-credential')
        await db.commit()


async def fields(client, **overrides):
    page = await client.get('/providers/tiktok/3')
    assert page.status_code == 200
    assert 'Test Creator' in page.text and 'value="" selected disabled' in page.text
    assert 'name="allow_duet" disabled' in page.text
    key = re.search(r'name="submission_key" value="([^"]+)"', page.text).group(1)
    return {'submission_key': key, 'creator_username': 'test_creator', 'content_kind': 'video',
            'privacy_level': 'SELF_ONLY', 'caption': 'A new post', 'consent': 'true', **overrides}


@pytest.mark.parametrize('kind', ['video', 'photo'])
def test_publishing_is_durable_and_only_complete_status_means_sent(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path)
    monkeypatch.setattr(settings, 'tiktok_photo_url_prefixes', 'https://media.example/verified/')
    calls, state = [], ['PROCESSING_UPLOAD']
    def handler(request):
        path = request.url.path
        if path.endswith('creator_info/query/'): return ok(CREATOR)
        calls.append(request)
        if path.endswith('/init/'):
            body = json.loads(request.content)
            assert body['post_info']['privacy_level'] == 'SELF_ONLY'
            assert body['post_info']['disable_comment'] is True
            if kind == 'video':
                assert body['source_info'] == {'source': 'FILE_UPLOAD', 'video_size': len(mp4()), 'chunk_size': len(mp4()), 'total_chunk_count': 1}
                return ok({'publish_id': 'v_pub_test', 'upload_url': 'https://open-upload.tiktokapis.com/video/?upload_token=secret'})
            assert body['post_mode'] == 'DIRECT_POST' and body['media_type'] == 'PHOTO'
            assert body['source_info']['photo_images'] == ['https://media.example/verified/photo.jpg']
            assert 'disable_duet' not in body['post_info']
            return ok({'publish_id': 'v_pub_test'})
        if request.method == 'PUT':
            assert 'authorization' not in request.headers
            assert request.headers['content-range'] == f'bytes 0-{len(mp4()) - 1}/{len(mp4())}'
            assert request.content == mp4()
            return httpx.Response(201)
        assert path.endswith('/status/fetch/')
        assert json.loads(request.content) == {'publish_id': 'v_pub_test'}
        return ok({'status': state[0]})

    async def scenario(client, sessions, context):
        await select_tiktok(sessions)
        data = await fields(client)
        files = {'video': ('My Video.mp4', mp4(), 'video/mp4')} if kind == 'video' else None
        if kind == 'photo': data.update(content_kind='photo', photos='https://media.example/verified/photo.jpg')
        assert (await client.post('/providers/tiktok/3', data=data, files=files)).status_code == 403
        response = await client.post('/providers/tiktok/3', headers=HEADERS, data=data, files=files)
        assert response.status_code == 303, response.text
        # Reloading/replaying the form cannot create another publishing record.
        assert (await client.post('/providers/tiktok/3', headers=HEADERS, data=data, files=files)).status_code == 303
        assert not calls
        async with sessions() as db:
            post = (await db.execute(select(TikTokPost))).scalar_one()
            post_id = post.id
            assert post.status == 'pending' and post.publish_id is None
            assert 'act.test-credential' not in post.post_info
            if kind == 'video':
                assert (await client.get(f'/providers/tiktok/posts/{post.id}/media')).status_code == 200
        await publishing.tick()
        async with sessions() as db:
            post = await db.get(TikTokPost, post_id)
            assert post.status == 'processing' and post.publish_id == 'v_pub_test' and post.media_path is None
        await publishing.tick()
        assert (await client.get(f'/providers/tiktok/posts/{post_id}')).json()['status'] == 'processing'
        state[0] = 'PUBLISH_COMPLETE'
        await publishing.tick()
        await publishing.tick()
        result = (await client.get(f'/providers/tiktok/posts/{post_id}')).json()
        assert result['status'] == 'sent'
        assert len([request for request in calls if request.url.path.endswith('/init/')]) == 1
        assert (await client.get('/providers/tiktok')).status_code == 200
        assert (await client.post('/api/send', json={'number': 'someone', 'message': 'not a post'})).status_code == 400
        assert (await client.post('/jobs/create', headers=HEADERS, data={'token_id': '3', 'group_ids': 'someone', 'message': 'not a post', 'trigger_type': 'now'})).status_code == 400

    asyncio.run(exercise(tmp_path, monkeypatch, handler, scenario))


@pytest.mark.parametrize('override', [{'consent': 'false'}, {'privacy_level': 'FOLLOWER_OF_CREATOR'},
    {'allow_duet': 'true'}, {'commercial': 'true'}, {'commercial': 'true', 'branded': 'true'},
    {'creator_username': 'different_creator'}, {'caption': '😀' * 1101}])
def test_post_permissions_and_consent_are_checked_server_side(tmp_path, monkeypatch, override):
    def handler(request):
        assert request.url.path.endswith('creator_info/query/')
        return ok(CREATOR)
    async def scenario(client, sessions, context):
        await select_tiktok(sessions)
        data = await fields(client, **override)
        result = await client.post('/providers/tiktok/3', headers=HEADERS, data=data,
                                   files={'video': ('video.mp4', mp4(), 'video/mp4')})
        assert result.status_code in {400, 409}
        async with sessions() as db: assert not (await db.execute(select(TikTokPost))).scalars().all()
    asyncio.run(exercise(tmp_path, monkeypatch, handler, scenario))


@pytest.mark.parametrize('failure', ['init_timeout', 'upload_timeout', 'upload_redirect', 'bad_upload_url'])
def test_uncertain_publishing_is_never_initialized_twice(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path)
    inits = []
    def handler(request):
        if request.url.path.endswith('creator_info/query/'): return ok(CREATOR)
        if request.url.path.endswith('/init/'):
            inits.append(request)
            if failure == 'init_timeout': raise httpx.ReadTimeout('act.test-credential', request=request)
            return ok({'publish_id': 'v_pub_uncertain', 'upload_url': 'http://127.0.0.1/private' if failure == 'bad_upload_url'
                       else 'https://open-upload.tiktokapis.com/video/?upload_token=secret'})
        if request.method == 'PUT':
            if failure == 'upload_redirect': return httpx.Response(302, headers={'location': 'https://evil.example/'})
            raise httpx.ReadTimeout('signed upload URL', request=request)
        assert request.url.path.endswith('/status/fetch/')
        return ok({'status': 'PUBLISH_COMPLETE'})
    async def scenario(client, sessions, context):
        await select_tiktok(sessions)
        result = await client.post('/providers/tiktok/3', headers=HEADERS, data=await fields(client),
                                   files={'video': ('video.mp4', mp4(), 'video/mp4')})
        assert result.status_code == 303
        await publishing.tick()
        await publishing.tick()
        await publishing.tick()
        assert len(inits) == 1
        async with sessions() as db:
            post = (await db.execute(select(TikTokPost))).scalar_one()
            assert post.status == ('unknown' if failure == 'init_timeout' else 'sent')
            assert 'act.test-credential' not in post.detail and 'upload_token' not in post.detail
    asyncio.run(exercise(tmp_path, monkeypatch, handler, scenario))


def test_future_post_can_be_cancelled_and_other_users_cannot_access_it(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path)
    def handler(request):
        assert request.url.path.endswith('creator_info/query/')
        return ok(CREATOR)
    async def scenario(client, sessions, context):
        await select_tiktok(sessions)
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        result = await client.post('/providers/tiktok/3', headers=HEADERS, data=await fields(client, scheduled_at=future),
                                   files={'video': ('video.mp4', mp4(), 'video/mp4')})
        assert result.status_code == 303
        await publishing.tick()
        async with sessions() as db:
            post = (await db.execute(select(TikTokPost))).scalar_one()
            post_id, path = post.id, post.media_path
            assert post.status == 'pending'
        from app.providers.tiktok import routes
        monkeypatch.setattr(routes, 'require_user', lambda request: {'sub': '999'})
        assert (await client.get(f'/providers/tiktok/posts/{post_id}')).status_code == 404
        assert (await client.get(f'/providers/tiktok/posts/{post_id}/media')).status_code == 404
        assert (await client.post(f'/providers/tiktok/posts/{post_id}/cancel', headers=HEADERS)).status_code == 404
        monkeypatch.setattr(routes, 'require_user', lambda request: {'sub': '7'})
        assert (await client.post(f'/providers/tiktok/posts/{post_id}/cancel', headers=HEADERS)).status_code == 303
        assert not Path(path).exists()
        await publishing.tick()
    asyncio.run(exercise(tmp_path, monkeypatch, handler, scenario))


def test_mp4_duration_and_invalid_container(tmp_path):
    file = tmp_path / 'video.mp4'
    file.write_bytes(mp4(32))
    assert video_duration(str(file)) == 32
    for content in [b'not mp4', box(b'moov', b'bad'), box(b'moov', box(b'mvhd', b'\x00' * 20))]:
        file.write_bytes(content)
        with pytest.raises(ValueError): video_duration(str(file))


def test_photo_sources_require_a_verified_prefix(monkeypatch):
    monkeypatch.setattr(settings, 'tiktok_photo_url_prefixes', 'https://media.example/photos/')
    assert photo_sources('https://media.example/photos/photo.jpg') == ['https://media.example/photos/photo.jpg']
    for url in ['http://media.example/photos/1.jpg', 'https://media.example.evil/photos/1.jpg',
                'https://media.example/photos-evil/1.jpg', 'https://user:pass@media.example/photos/1.jpg']:
        with pytest.raises(ValueError): photo_sources(url)


def test_tiktok_rejects_expired_or_incomplete_creator_info(monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json={'error': {'code': 'access_token_invalid', 'message': 'act.secret'}}))
    with pytest.raises(ProviderError) as exc: asyncio.run(TikTokPublisher('act.secret').creator_info())
    assert 'act.secret' not in str(exc.value)
    transport(monkeypatch, lambda request: ok({}))
    with pytest.raises(ProviderError): asyncio.run(TikTokPublisher('act.secret').creator_info())


@pytest.mark.parametrize('outcome', ['crash_before_ack', 'crash_after_ack', 'failed', 'deleted_connection', 'creator_changed', 'too_long'])
def test_recovery_and_lifecycle_do_not_create_duplicate_posts(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path)
    inits = []
    creator_state = [dict(CREATOR)]
    def handler(request):
        if request.url.path.endswith('creator_info/query/'): return ok(creator_state[0])
        if request.url.path.endswith('/init/'):
            inits.append(request)
            return ok({'publish_id': 'v_pub_recovery', 'upload_url': 'https://open-upload.tiktokapis.com/video/'})
        if request.method == 'PUT': return httpx.Response(201)
        return ok({'status': 'FAILED', 'fail_reason': 'file_format_check_failed'})

    async def scenario(client, sessions, context):
        await select_tiktok(sessions)
        created = await client.post('/providers/tiktok/3', headers=HEADERS, data=await fields(client),
                                    files={'video': ('video.mp4', mp4(), 'video/mp4')})
        assert created.status_code == 303
        async with sessions() as db:
            post = (await db.execute(select(TikTokPost))).scalar_one()
            post_id, path = post.id, post.media_path
            if outcome in {'crash_before_ack', 'crash_after_ack'}:
                post.status = 'submitting' if outcome == 'crash_before_ack' else 'uploading'
                post.updated_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=11)
                if outcome == 'crash_after_ack': post.publish_id = 'v_pub_saved_before_crash'
                await db.commit()
        if outcome == 'deleted_connection':
            assert (await client.post('/tokens/3/delete', headers=HEADERS)).status_code == 303
        elif outcome == 'creator_changed':
            creator_state[0]['creator_username'] = 'changed_account'
        elif outcome == 'too_long':
            creator_state[0]['max_video_post_duration_sec'] = 10
        await publishing.tick()
        await publishing.tick()
        async with sessions() as db:
            post = await db.get(TikTokPost, post_id)
            assert post.status == ('unknown' if outcome == 'crash_before_ack' else 'cancelled' if outcome == 'deleted_connection' else 'failed')
            assert not Path(path).exists()
        assert len(inits) == (1 if outcome == 'failed' else 0)
    asyncio.run(exercise(tmp_path, monkeypatch, handler, scenario))
