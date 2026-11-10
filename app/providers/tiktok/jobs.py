"""Claim once, persist TikTok IDs before uploading, and reconcile without resubmitting."""
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, update, or_, and_

from app.database import async_session
from app.models.token import Token
from app.providers.base import ProviderError
from app.services.crypto import decrypt_token
from .client import TikTokPublisher
from .media import video_duration
from .models import TikTokPost

logger = logging.getLogger(__name__)
ACTIVE = set()


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def remove_media(post):
    if post.media_path:
        Path(post.media_path).unlink(missing_ok=True)
        post.media_path = None


async def connection(db, post):
    token = await db.get(Token, post.token_id) if post.token_id else None
    if not token or token.provider != 'tiktok' or not token.is_active or token.user_id != post.user_id:
        raise ValueError('The original TikTok connection is unavailable')
    credential = decrypt_token(token.api_token)
    if fingerprint(credential) != post.credential_fingerprint:
        raise ValueError('The original TikTok credential changed; check this post in TikTok before resubmitting')
    return TikTokPublisher(credential), token


async def submit(db, post):
    claimed = await db.execute(update(TikTokPost).where(TikTokPost.id == post.id, TikTokPost.status == 'pending')
                               .values(status='submitting', updated_at=now()))
    if not claimed.rowcount:
        await db.rollback()
        return
    await db.commit()
    ACTIVE.add(post.id)
    try:
        try:
            publisher, token = await connection(db, post)
            creator = await publisher.creator_info()
            if creator['creator_username'] != post.creator_username:
                raise ValueError('The TikTok account changed; create a new post after reviewing its account and settings')
            info = json.loads(post.post_info)
            if info['privacy_level'] not in creator['privacy_level_options']:
                raise ValueError('The selected visibility is no longer available for this TikTok account')
            for interaction in ('comment', 'duet', 'stitch'):
                if creator[f'{interaction}_disabled'] and not info.get(f'disable_{interaction}', True):
                    raise ValueError('TikTok interaction permissions changed; review the post settings again')
            if post.media_path and video_duration(post.media_path) > creator['max_video_post_duration_sec']:
                raise ValueError('The video exceeds this TikTok account\'s duration limit')
        except Exception:
            post.status, post.detail = 'failed', 'The connection, media or account permissions changed. Review them before creating another post.'
            remove_media(post)
            await db.commit()
            return
        try:
            data = await publisher.initialize(info, video_path=post.media_path,
                                              photo_urls=json.loads(post.photo_urls) if post.photo_urls else None)
        except ProviderError as exc:
            post.status, post.detail = ('unknown' if exc.unknown else 'failed'), str(exc)
            remove_media(post)
            await db.commit()
            return
        # This commit precedes upload. A crash can never generate a second init call.
        post.publish_id, post.status = data['publish_id'], 'uploading' if post.media_path else 'processing'
        post.detail = 'TikTok accepted the publishing request; awaiting completion'
        token.last_used_at = datetime.now(timezone.utc)
        await db.commit()
        if post.media_path:
            try:
                await publisher.upload(data.get('upload_url'), post.media_path)
            except ProviderError as exc:
                post.detail = str(exc)
            finally:
                remove_media(post)
                post.status = 'processing'
                await db.commit()
    finally:
        ACTIVE.discard(post.id)


async def poll(db, post):
    if post.id in ACTIVE:
        return
    if not post.publish_id:
        if post.status == 'submitting' and now() - post.updated_at > timedelta(minutes=10):
            post.status = 'unknown'
            post.detail = 'Submission was interrupted before its publishing ID was saved. Check TikTok before creating another post.'
            remove_media(post)
            await db.commit()
        return
    try:
        publisher, _ = await connection(db, post)
        data = await publisher.status(post.publish_id)
    except Exception:
        post.detail = 'Unable to check TikTok yet. The existing publishing ID is retained; this post will not be submitted again.'
        post.updated_at = now()
        await db.commit()
        return
    state = data['status']
    if state == 'PUBLISH_COMPLETE':
        post.status, post.detail = 'sent', 'TikTok confirmed publication'
    elif state == 'FAILED':
        reason = data.get('fail_reason', '')
        reason = reason if isinstance(reason, str) and reason.replace('_', '').isalnum() and len(reason) <= 100 else 'publishing_failed'
        post.status, post.detail = 'failed', f'TikTok could not publish this post ({reason})'
    elif state == 'SEND_TO_USER_INBOX':
        post.status, post.detail = 'unknown', 'TikTok returned an inbox draft instead of confirmed publication. Check it in TikTok.'
    else:
        post.status, post.detail = 'processing', 'TikTok is processing this post; publication is not confirmed yet'
    # If a process died between saving the ID and upload, retain that ID and poll;
    # never repeat an initialization or guess whether the upload was accepted.
    if post.media_path:
        remove_media(post)
    post.updated_at = now()
    await db.commit()


async def tick():
    async with async_session() as db:
        posts = (await db.execute(select(TikTokPost).where(
            or_(
                and_(TikTokPost.publish_id.is_not(None), TikTokPost.status.in_(['uploading', 'processing', 'unknown'])),
                and_(TikTokPost.status == 'submitting', TikTokPost.updated_at < now() - timedelta(minutes=10)),
            )
        ).order_by(TikTokPost.updated_at, TikTokPost.id).limit(5))).scalars().all()
        for post in posts:
            await poll(db, post)
        due = (await db.execute(select(TikTokPost).where(
            TikTokPost.status == 'pending', TikTokPost.scheduled_at <= now()
        ).order_by(TikTokPost.scheduled_at, TikTokPost.id).limit(1))).scalars().all()
        for post in due:
            try:
                await submit(db, post)
            except Exception:
                # Do not include HTTP exceptions or signed URLs in logs.
                logger.error('TikTok post submission interrupted (post %s); it will be reconciled without resubmitting', post.id)
                await db.rollback()


def register_jobs(scheduler):
    scheduler.add_job(tick, 'interval', seconds=15, id='tiktok:publish-status', replace_existing=True, max_instances=1)


async def delete_connection(db, token):
    posts = (await db.execute(select(TikTokPost).where(TikTokPost.token_id == token.id))).scalars().all()
    for post in posts:
        if post.status == 'pending':
            post.status, post.detail = 'cancelled', 'Connection deleted before publication'
        elif post.status in {'submitting', 'uploading', 'processing'}:
            post.status, post.detail = 'unknown', 'Connection deleted; check TikTok for the final publication outcome'
        post.token_id = None
        if post.id not in ACTIVE:
            remove_media(post)
