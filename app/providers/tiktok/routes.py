"""Explicit TikTok publishing consent and ownership-checked post history."""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.token import Token
from app.routers.auth import require_user
from app.services.crypto import decrypt_token
from app.services.csrf import csrf_protect
from app.providers.base import ProviderError
from .client import TikTokPublisher
from .jobs import fingerprint, remove_media
from .media import video_duration
from .models import TikTokPost

router = APIRouter(prefix='/providers/tiktok', tags=['tiktok'], dependencies=[Depends(csrf_protect)])


async def owned_connection(request, db, token_id, *, active=True):
    user = require_user(request)
    token = await db.scalar(select(Token).where(Token.id == token_id, Token.user_id == int(user['sub']), Token.provider == 'tiktok'))
    if not token:
        raise HTTPException(404, 'TikTok connection not found')
    if active and not token.is_active:
        raise HTTPException(400, 'Select an active TikTok connection')
    return user, token, TikTokPublisher(decrypt_token(token.api_token))


def photo_sources(raw):
    urls = [value.strip() for value in raw.splitlines() if value.strip()]
    prefixes = [prefix.strip() for prefix in settings.tiktok_photo_url_prefixes.split(',') if prefix.strip()]
    if not 1 <= len(urls) <= 35:
        raise ValueError('Photo posts need 1–35 image URLs, one per line')
    for value in urls:
        parts = urlsplit(value)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.fragment
                or len(value) > 2048 or any(ord(char) <= 32 for char in value)):
            raise ValueError('Photo sources must be HTTPS URLs without embedded credentials or fragments')
        matched = False
        for prefix in prefixes:
            allowed = urlsplit(prefix)
            if (allowed.scheme == 'https' and not allowed.username and not allowed.password and not allowed.query and not allowed.fragment
                    and parts.netloc == allowed.netloc
                    and parts.path.startswith((allowed.path or '/').rstrip('/') + '/')):
                matched = True
                break
        if not matched:
            raise ValueError('Photo URLs must use a source prefix configured by your administrator and verified with TikTok')
    return urls


async def creator(publisher):
    try:
        return await publisher.creator_info()
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get('')
async def history(request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    posts = (await db.execute(select(TikTokPost).where(TikTokPost.user_id == int(user['sub']))
                              .order_by(TikTokPost.id.desc()).limit(100))).scalars().all()
    return request.app.state.render(request, 'providers/tiktok/history.html', posts=posts)


@router.get('/posts/{post_id}')
async def post_status(post_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    post = await db.scalar(select(TikTokPost).where(TikTokPost.id == post_id, TikTokPost.user_id == int(user['sub'])))
    if not post:
        raise HTTPException(404, 'Post not found')
    return JSONResponse({'id': post.id, 'status': post.status, 'detail': post.detail,
                         'publish_id': post.publish_id}, headers={'Cache-Control': 'no-store'})


@router.get('/posts/{post_id}/media')
async def post_media(post_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    post = await db.scalar(select(TikTokPost).where(TikTokPost.id == post_id, TikTokPost.user_id == int(user['sub'])))
    if not post or not post.media_path or not Path(post.media_path).is_file():
        raise HTTPException(404, 'Preview is no longer available')
    return FileResponse(post.media_path, media_type='video/mp4', headers={'Cache-Control': 'no-store'})


@router.post('/posts/{post_id}/cancel')
async def cancel(post_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    post = await db.scalar(select(TikTokPost).where(TikTokPost.id == post_id, TikTokPost.user_id == int(user['sub'])))
    if not post:
        raise HTTPException(404, 'Post not found')
    claimed = await db.execute(update(TikTokPost).where(TikTokPost.id == post_id, TikTokPost.status == 'pending')
                               .values(status='cancelled', detail='Cancelled before publishing'))
    if not claimed.rowcount:
        raise HTTPException(409, 'This post has already started; check its status in TikTok')
    remove_media(post)
    await db.commit()
    return RedirectResponse('/providers/tiktok', status_code=303)


@router.get('/{token_id}')
async def publish_page(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    _, token, publisher = await owned_connection(request, db, token_id)
    info = await creator(publisher)
    return request.app.state.render(request, 'providers/tiktok/form.html', token=token, creator=info,
                                    submission_key=str(uuid4()), photos_enabled=bool(settings.tiktok_photo_url_prefixes))


@router.post('/{token_id}')
async def publish(
    token_id: int, request: Request, db: AsyncSession = Depends(get_db),
    submission_key: str = Form(...), creator_username: str = Form(...),
    content_kind: str = Form(...), privacy_level: str = Form(...),
    caption: str = Form(default=''), photo_title: str = Form(default=''),
    photos: str = Form(default=''), video: UploadFile | None = File(default=None),
    scheduled_at: str = Form(default=''), consent: bool = Form(default=False),
    allow_comment: bool = Form(default=False), allow_duet: bool = Form(default=False), allow_stitch: bool = Form(default=False),
    commercial: bool = Form(default=False), own_brand: bool = Form(default=False), branded: bool = Form(default=False),
    is_aigc: bool = Form(default=False),
):
    user, token, publisher = await owned_connection(request, db, token_id)
    try:
        key = str(UUID(submission_key))
    except ValueError:
        raise HTTPException(400, 'Reload the publishing page before submitting') from None
    existing = await db.scalar(select(TikTokPost).where(TikTokPost.submission_key == key))
    if existing:
        if existing.user_id != int(user['sub']) or existing.token_id != token.id:
            raise HTTPException(409, 'This publishing submission key was already used')
        return RedirectResponse('/providers/tiktok', status_code=303)
    info = await creator(publisher)
    if creator_username != info['creator_username']:
        raise HTTPException(409, 'The connected TikTok account changed. Reload the page and review it before publishing.')
    if not consent:
        raise HTTPException(400, 'Review the content and agree to TikTok publishing before submitting')
    if privacy_level not in info['privacy_level_options']:
        raise HTTPException(400, 'Choose a visibility option available for this TikTok account')
    if (commercial and not (own_brand or branded)) or (not commercial and (own_brand or branded)):
        raise HTTPException(400, 'Choose the commercial content disclosure that applies to this post')
    if branded and privacy_level == 'SELF_ONLY':
        raise HTTPException(400, 'Branded content cannot use private visibility')
    if content_kind not in {'video', 'photo'}:
        raise HTTPException(400, 'Choose a video or photo post')
    for interaction, enabled in [('comment', allow_comment), ('duet', allow_duet), ('stitch', allow_stitch)]:
        if enabled and info[f'{interaction}_disabled']:
            raise HTTPException(400, 'This interaction is disabled by the creator\'s TikTok settings')
    if content_kind == 'photo' and (allow_duet or allow_stitch):
        raise HTTPException(400, 'Duet and Stitch apply to videos only')
    if len(caption.encode('utf-16-le')) // 2 > (2200 if content_kind == 'video' else 4000) or len(photo_title.encode('utf-16-le')) // 2 > 90:
        raise HTTPException(400, 'TikTok caption or photo title exceeds its character limit')
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    when = utc_now
    if scheduled_at:
        try:
            # The form's local time is interpreted in the signed-in user's IANA timezone.
            local = datetime.fromisoformat(scheduled_at)
            if local.tzinfo is None:
                local = local.replace(tzinfo=ZoneInfo(user.get('tz', 'UTC')))
            when = local.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, ZoneInfoNotFoundError):
            raise HTTPException(400, 'Enter a valid publication date and time') from None
        if when < utc_now:
            raise HTTPException(400, 'Choose a publication time in the future')
    path, photo_urls = None, None
    try:
        if content_kind == 'video':
            if not video or Path(video.filename or '').suffix.lower() != '.mp4' or photos.strip():
                raise HTTPException(400, 'Choose one MP4 video for this post')
            from app.routers.jobs import save_upload
            path = await save_upload(video)
            if not path or video_duration(path) > info['max_video_post_duration_sec']:
                raise ValueError('Video exceeds the duration allowed for this TikTok account')
        else:
            if video and video.filename:
                raise HTTPException(400, 'Photo posts cannot also include a video')
            photo_urls = photo_sources(photos)
        post_info = {'privacy_level': privacy_level, 'disable_comment': not allow_comment,
                     'disable_duet': not allow_duet, 'disable_stitch': not allow_stitch,
                     'brand_content_toggle': branded, 'brand_organic_toggle': own_brand, 'is_aigc': is_aigc}
        if content_kind == 'video':
            post_info['title'] = caption
        else:
            post_info.update(title=photo_title, description=caption)
        post = TikTokPost(user_id=int(user['sub']), token_id=token.id, submission_key=key,
            creator_username=creator_username, credential_fingerprint=fingerprint(decrypt_token(token.api_token)),
            media_path=path, original_filename=Path(video.filename).name if path else None,
            photo_urls=json.dumps(photo_urls) if photo_urls else None, post_info=json.dumps(post_info),
            scheduled_at=when, consent_at=utc_now, status='pending', detail='Waiting for the scheduled publishing time')
        db.add(post)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if path: Path(path).unlink(missing_ok=True)
        raise HTTPException(409, 'This post has already been submitted') from None
    except (ValueError, OSError) as exc:
        if path: Path(path).unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        if path: Path(path).unlink(missing_ok=True)
        raise
    return RedirectResponse('/providers/tiktok', status_code=303)
