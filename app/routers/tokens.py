from urllib.parse import urlencode
from pathlib import Path
from uuid import UUID
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
from app.services.relay_sender import RelaySender, normalize_base_url
from app.services.relay_api import connection_base_url

router = APIRouter(prefix="/tokens", tags=["tokens"], dependencies=[Depends(csrf_protect)])
PAIRING_HEADERS = {"Cache-Control": "no-store, private", "Pragma": "no-cache"}


def pairing_error(exc: Exception) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 403:
            return HTTPException(502, "Relay key needs sessions:read and sessions:write permissions for pairing.")
        if exc.response.status_code == 401:
            return HTTPException(502, "Relay rejected the API key. Update the token and try again.")
        if exc.response.status_code == 409:
            try:
                error = exc.response.json().get("error", {})
                code = error.get("code") if isinstance(error, dict) else None
            except (ValueError, AttributeError):
                code = None
            if code == "session_unavailable":
                return HTTPException(502, "Relay has no healthy WhatsApp worker for this session. Wait for the Relay worker to recover, then try again.")
            if code == "active_session_exists":
                return HTTPException(502, "Your Relay account already has an active session. Enter its session ID when adding the token.")
            return HTTPException(502, "Relay could not complete this session operation. Check the Relay session and worker, then try again.")
    if isinstance(exc, httpx.TimeoutException):
        return HTTPException(502, "Relay timed out. Check connection status before trying again.")
    if isinstance(exc, httpx.RequestError):
        return HTTPException(502, "Could not reach the Relay backend. Check its URL and availability.")
    return HTTPException(502, "Relay could not complete pairing. Check the session and Relay worker, then try again.")


async def owned_relay_token(token_id: int, request: Request, db: AsyncSession) -> Token:
    user = require_user(request)
    token = (await db.execute(select(Token).where(
        Token.id == token_id, Token.user_id == int(user["sub"]),
    ))).scalar_one_or_none()
    if not token:
        raise HTTPException(404, "Token not found")
    if token.provider != "relay" or not token.is_active:
        raise HTTPException(400, "Select an active Relay token for pairing")
    if not token.relay_session_id:
        raise HTTPException(400, "Save a Relay session on the token edit page before pairing")
    return token


def relay_connection(token: Token) -> RelaySender:
    return RelaySender(decrypt_token(token.api_token), token.relay_session_id, connection_base_url(token))


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


@router.get("/relay/setup")
async def relay_setup_page(request: Request):
    require_user(request)
    response = request.app.state.render(request, "tokens/relay_setup.html", values={})
    response.headers.update(PAIRING_HEADERS)
    return response


@router.post("/relay/setup")
async def relay_setup(
    request: Request, relay_base_url: str = Form(...), email: str = Form(...),
    password: str = Form(...), mode: str = Form(default="register"),
    name: str = Form(default=""), relay_session_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    values = {"relay_base_url": relay_base_url, "email": email, "mode": mode,
              "name": name, "relay_session_id": relay_session_id}
    try:
        relay_url = validate_relay_url("relay", relay_base_url)
        if mode not in {"register", "login"}:
            raise HTTPException(400, "Choose Register or Log in.")
        if not email.strip() or not password:
            raise HTTPException(400, "Enter your Relay email and password.")
        if mode == "register" and not 12 <= len(password.encode("utf-8")) <= 1024:
            raise HTTPException(400, "Relay passwords must contain 12–1024 UTF-8 bytes.")
        session_id = validate_connection("relay", relay_session_id) if relay_session_id.strip() else None
        key = await RelaySender.provision_key(relay_url, email.strip(), password, mode)
    except (HTTPException, httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, HTTPException):
            message = exc.detail
        elif isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409:
            message = "This email is already registered in Relay. Choose Log in instead."
        elif isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
            message = "Relay rejected the email or password. Try again."
        else:
            message = "Could not set up your Relay account. Check the backend URL and credentials. If registration already succeeded, choose Log in to continue."
        response = request.app.state.render(request, "tokens/relay_setup.html", values=values, error=message)
        response.status_code = 400
        response.headers.update(PAIRING_HEADERS)
        return response
    # Persist the issued key before session creation so a worker failure cannot lose it.
    token = Token(user_id=int(user["sub"]), name=name.strip() or "Relay", provider="relay",
                  api_token=encrypt_token(key), relay_session_id=session_id, relay_base_url=relay_url)
    db.add(token)
    await db.commit()
    if not session_id:
        try:
            token.relay_session_id = await RelaySender.create_session(key, relay_url)
            await db.commit()
        except (httpx.HTTPError, ValueError):
            return _flash(f"/tokens/{token.id}/edit", success="Relay account connected. Enter an existing session ID, or leave it blank and save to retry session creation.")
    return RedirectResponse(f"/tokens/{token.id}/pair", status_code=303, headers=PAIRING_HEADERS)


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

    relay_url = validate_relay_url(provider, relay_base_url)
    if not api_token.strip():
        raise HTTPException(400, "API token is required")
    if provider == "relay" and not relay_session_id.strip():
        try:
            session_id = await RelaySender.create_session(api_token.strip(), relay_url)
        except (httpx.HTTPError, ValueError) as exc:
            raise pairing_error(exc) from exc
    else:
        session_id = validate_connection(provider, relay_session_id)
    token = Token(user_id=user_id, name=name or None, api_token=encrypt_token(api_token.strip()),
                  provider=provider, relay_session_id=session_id, relay_base_url=relay_url)
    db.add(token)
    await db.commit()
    if provider == "relay":
        return RedirectResponse(f"/tokens/{token.id}/pair", status_code=303)
    return _flash("/tokens", success="Token created")


@router.get("/{token_id}/pair")
async def pair_relay_page(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = await owned_relay_token(token_id, request, db)
    response = request.app.state.render(request, "tokens/pair.html", token=token)
    response.headers.update(PAIRING_HEADERS)
    return response


@router.post("/{token_id}/relay/connect")
async def connect_relay(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = await owned_relay_token(token_id, request, db)
    try:
        data = await relay_connection(token).connect()
    except (httpx.HTTPError, ValueError) as exc:
        raise pairing_error(exc) from exc
    return JSONResponse({"state": data["state"]}, headers=PAIRING_HEADERS)


@router.get("/{token_id}/relay/state")
async def relay_pairing_state(token_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    token = await owned_relay_token(token_id, request, db)
    try:
        client = relay_connection(token)
        state = (await client.get_status())["state"]
        qr = await client.get_qr() if state == "qr_required" else None
    except (httpx.HTTPError, ValueError) as exc:
        # Upstream QR responses are never stored in the database or execution logs.
        return JSONResponse({"detail": pairing_error(exc).detail}, status_code=502, headers=PAIRING_HEADERS)
    return JSONResponse({"state": state, "qr": qr}, headers=PAIRING_HEADERS)


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

    if token.provider == "relay" and token.relay_session_id is None and provider == "relay":
        relay_url = validate_relay_url(provider, relay_base_url)
        if relay_url != token.relay_base_url:
            raise HTTPException(400, "Create a new token to change its backend URL")
        try:
            token.relay_session_id = (validate_connection(provider, relay_session_id) if relay_session_id.strip()
                                      else await RelaySender.create_session(decrypt_token(token.api_token), relay_url))
        except (httpx.HTTPError, ValueError) as exc:
            raise pairing_error(exc) from exc
        token.name = name or None
        await db.commit()
        return RedirectResponse(f"/tokens/{token.id}/pair", status_code=303)
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
