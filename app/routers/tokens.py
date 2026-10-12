from urllib.parse import urlencode
from pathlib import Path
from uuid import uuid4
import httpx

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
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
from app.services.native_sender import NativeSender

router = APIRouter(prefix="/tokens", tags=["tokens"], dependencies=[Depends(csrf_protect)])
PAIRING_HEADERS = {"Cache-Control": "no-store, private", "Pragma": "no-cache"}


def pairing_error(exc: Exception) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            detail = exc.response.json().get("error", {}).get("message")
        except (ValueError, AttributeError):
            detail = None
        if detail and exc.response.status_code in {400, 409}:
            return HTTPException(502, detail)
    return HTTPException(502, "The built-in WhatsApp service is unavailable. Try reconnecting shortly.")


async def owned_native_token(token_id: int, request: Request, db: AsyncSession) -> Token:
    user = require_user(request)
    token = (await db.execute(select(Token).where(
        Token.id == token_id, Token.user_id == int(user["sub"]),
    ))).scalar_one_or_none()
    if not token:
        raise HTTPException(404, "Connection not found")
    if token.provider != "native" or not token.is_active:
        raise HTTPException(400, "Select an active built-in WhatsApp connection")
    return token


def native_connection(token: Token) -> NativeSender:
    return NativeSender(session_id=token.relay_session_id)


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


@router.get("/connect")
async def connect_page(request: Request):
    require_user(request)
    return request.app.state.render(request, "tokens/connect.html")


@router.get("/relay/setup", include_in_schema=False)
async def retired_relay_setup(request: Request):
    require_user(request)
    return RedirectResponse("/tokens/connect", status_code=303)


@router.post("/connect")
async def create_native_connection(request: Request, name: str = Form(default=""), db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    token = Token(user_id=int(user["sub"]), name=name.strip()[:120] or "WhatsApp",
                  provider="native", api_token="", relay_session_id=str(uuid4()))
    db.add(token)
    await db.commit()
    return RedirectResponse(f"/tokens/{token.id}/pair", status_code=303)


@router.post("/create")
async def create_token(request: Request, name: str = Form(default=""), api_token: str = Form(...),
                       provider: str = Form(default="whatsapp"), db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    if provider != "whatsapp":
        raise HTTPException(400, "Use Connect WhatsApp for QR pairing")
    if not api_token.strip():
        raise HTTPException(400, "API token is required")
    db.add(Token(user_id=int(user["sub"]), name=name or None, provider="whatsapp", api_token=encrypt_token(api_token.strip())))
    await db.commit()
    return _flash("/tokens", success="Token created")


@router.get("/{token_id}/pair")
async def pair_native_page(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = await owned_native_token(token_id, request, db)
    response = request.app.state.render(request, "tokens/pair.html", token=token)
    response.headers.update(PAIRING_HEADERS)
    return response


@router.post("/{token_id}/whatsapp/connect")
async def connect_native(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = await owned_native_token(token_id, request, db)
    try:
        data = await native_connection(token).connect()
    except (httpx.HTTPError, ValueError) as exc:
        raise pairing_error(exc) from exc
    return JSONResponse({"state": data["state"]}, headers=PAIRING_HEADERS)


@router.get("/{token_id}/whatsapp/state")
async def native_pairing_state(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = await owned_native_token(token_id, request, db)
    try:
        client = native_connection(token)
        state = (await client.get_status())["state"]
        qr = await client.get_qr() if state == "qr_required" else None
    except (httpx.HTTPError, ValueError) as exc:
        # Upstream QR responses are never stored in the database or execution logs.
        return JSONResponse({"detail": pairing_error(exc).detail}, status_code=502, headers=PAIRING_HEADERS)
    return JSONResponse({"state": state, "qr": qr}, headers=PAIRING_HEADERS)


@router.get("/{token_id}/whatsapp/status")
async def native_connection_status(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = await owned_native_token(token_id, request, db)
    try:
        state = (await native_connection(token).get_status())["state"]
    except (httpx.HTTPError, ValueError):
        return JSONResponse({"state": "unknown"}, status_code=502, headers=PAIRING_HEADERS)
    return JSONResponse({"state": state}, headers=PAIRING_HEADERS)


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
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Token).where(Token.id == token_id, Token.user_id == user_id))
    token = result.scalar_one_or_none()
    if not token:
        return RedirectResponse(url="/tokens", status_code=303)

    if provider != token.provider:
        raise HTTPException(400, "Create a new connection to change its provider")
    token.name = name or None
    if api_token and token.provider == "whatsapp":
        if not api_token.strip():
            raise HTTPException(400, "API token cannot be whitespace")
        token.api_token = encrypt_token(api_token.strip())
    await db.commit()
    return _flash("/tokens", success="Token updated")


@router.post("/{token_id}/whatsapp/logout")
async def logout_native(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = await owned_native_token(token_id, request, db)
    try:
        data = await native_connection(token).disconnect()
    except (httpx.HTTPError, ValueError) as exc:
        raise pairing_error(exc) from exc
    return JSONResponse({"state": data["state"]}, headers=PAIRING_HEADERS)


@router.post("/{token_id}/delete")
async def delete_token(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(select(Token).where(Token.id == token_id, Token.user_id == user_id))
    token = result.scalar_one_or_none()
    if token:
        if token.provider == "native":
            try:
                await native_connection(token).delete()
            except (httpx.HTTPError, ValueError) as exc:
                raise pairing_error(exc) from exc
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
