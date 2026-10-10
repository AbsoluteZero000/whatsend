from urllib.parse import urlencode
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.token import Token
from app.models.job import Job
from app.models.apikey import ApiKey
from app.routers.auth import require_user
from app.services.crypto import encrypt_token, decrypt_token
from app.services.csrf import csrf_protect
from app.services.scheduler import remove_job
from app.services.relay_sender import normalize_base_url

router = APIRouter(prefix="/tokens", tags=["tokens"], dependencies=[Depends(csrf_protect)])


def validate_connection(provider: str, session_id: str) -> str | None:
    if provider not in {"whatsapp", "relay"}:
        raise HTTPException(400, "Unsupported provider")
    if provider == "whatsapp":
        return None
    try:
        return str(UUID(session_id.strip()))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(400, "Enter a valid Relay session UUID") from exc


def validate_relay_url(provider: str, url: str) -> str | None:
    if provider != "relay":
        return None
    try:
        return normalize_base_url(url)
    except ValueError as exc:
        raise HTTPException(400, "Enter the Relay HTTP/HTTPS backend URL without /api/v1, credentials, query or fragment") from exc

def _flash(url: str, success: str = "") -> RedirectResponse:
    if success:
        url += ("&" if "?" in url else "?") + urlencode({"success": success})
    return RedirectResponse(url=url, status_code=303)


@router.get("")
async def list_tokens(request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Token).where(Token.user_id == user_id).order_by(Token.created_at.desc()))
    tokens = result.scalars().all()
    return request.app.state.render(request, "tokens/list.html", tokens=tokens)


@router.get("/create")
async def create_token_page(request: Request):
    require_user(request)
    return request.app.state.render(request, "tokens/form.html")


@router.post("/create")
async def create_token(
    request: Request,
    name: str = Form(default=""),
    api_token: str = Form(...),
    provider: str = Form(default="whatsapp"),
    relay_session_id: str = Form(default=""),
    relay_base_url: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])

    session_id = validate_connection(provider, relay_session_id)
    relay_url = validate_relay_url(provider, relay_base_url)
    if not api_token.strip():
        raise HTTPException(400, "API token is required")
    token = Token(user_id=user_id, name=name or None, api_token=encrypt_token(api_token.strip()),
                  provider=provider, relay_session_id=session_id, relay_base_url=relay_url)
    db.add(token)
    await db.commit()
    return _flash("/tokens", success="Token created")


@router.post("/{token_id}/toggle")
async def toggle_token(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Token).where(Token.id == token_id, Token.user_id == user_id))
    token = result.scalar_one_or_none()
    if token:
        token.is_active = not token.is_active
        await db.commit()
    return _flash("/tokens", success="Token toggled")


@router.get("/{token_id}/edit")
async def edit_token_page(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Token).where(Token.id == token_id, Token.user_id == user_id))
    token = result.scalar_one_or_none()
    if not token:
        return RedirectResponse(url="/tokens", status_code=303)

    return request.app.state.render(request, "tokens/form.html", token=token, edit_mode=True)


@router.post("/{token_id}/edit")
async def edit_token(
    token_id: int,
    request: Request,
    name: str = Form(default=""),
    api_token: str = Form(default=""),
    provider: str = Form(default="whatsapp"),
    relay_session_id: str = Form(default=""),
    relay_base_url: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Token).where(Token.id == token_id, Token.user_id == user_id))
    token = result.scalar_one_or_none()
    if not token:
        return RedirectResponse(url="/tokens", status_code=303)

    session_id = validate_connection(provider, relay_session_id)
    relay_url = validate_relay_url(provider, relay_base_url)
    if provider != token.provider or session_id != token.relay_session_id or relay_url != token.relay_base_url:
        raise HTTPException(400, "Create a new token to change its provider, Relay session or backend URL")
    token.name = name or None
    if api_token:
        if not api_token.strip():
            raise HTTPException(400, "API token cannot be whitespace")
        token.api_token = encrypt_token(api_token.strip())
    await db.commit()
    return _flash("/tokens", success="Token updated")


@router.post("/{token_id}/delete")
async def delete_token(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Token).where(Token.id == token_id, Token.user_id == user_id))
    token = result.scalar_one_or_none()
    if token:
        jobs_result = await db.execute(select(Job).where(Job.token_id == token.id, Job.user_id == user_id))
        linked_jobs = jobs_result.scalars().all()
        media_paths = {job.image_path for job in linked_jobs if job.image_path}
        for job in linked_jobs:
            await remove_job(job.id)
        await db.execute(update(ApiKey).where(ApiKey.token_id == token.id).values(token_id=None))
        await db.delete(token)
        await db.flush()
        for media_path in media_paths:
            references = await db.scalar(select(func.count(Job.id)).where(Job.image_path == media_path))
            if not references:
                Path(media_path).unlink(missing_ok=True)
        await db.commit()
    return _flash("/tokens", success="Token deleted")
