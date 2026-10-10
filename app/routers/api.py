from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID
import json
import hashlib
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.apikey import ApiKey
from app.models.token import Token
from app.models.user import User
from app.models.relay_operation import RelayOperation
from app.services.auth import verify_password
from app.services.crypto import decrypt_token
from app.services.sender import get_sender
from app.services.relay_api import connection_base_url, key_fingerprint, operation_response, submit_relay
from app.routers.jobs import save_upload

router = APIRouter(prefix="/api", tags=["api"])


class SendMessageRequest(BaseModel):
    group_id: str | None = Field(default=None, min_length=1)
    number: str | None = Field(default=None, min_length=1)
    message: str = Field(min_length=1)
    provider: str | None = None


@dataclass
class ApiKeyContext:
    user: User
    api_key: ApiKey


def _upstream_error(exc: Exception, provider: str = "whatsapp") -> str:
    label = "Relay" if provider == "relay" else "Whapi"
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        try:
            data = response.json()
        except ValueError:
            data = None
        detail = data.get("message") or data.get("error") or data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("code")
        if not detail:
            detail = response.text[:500] or response.reason_phrase
        return f"{label} rejected the request ({response.status_code}): {detail}"
    if isinstance(exc, httpx.TimeoutException):
        return f"{label} request timed out"
    if isinstance(exc, httpx.RequestError):
        return f"Could not reach {label}: {exc}"
    return str(exc)


async def get_linked_token(context: ApiKeyContext, db: AsyncSession, provider: str | None = None) -> Token:
    if provider is not None and provider not in {"whatsapp", "relay"}:
        raise HTTPException(status_code=400, detail="provider must be 'whatsapp' or 'relay'")

    user = context.user
    api_key = context.api_key

    if not api_key.token_id:
        raise HTTPException(status_code=400, detail="API key is not linked to a token. Generate a new API key and select a token.")

    result = await db.execute(
        select(Token).where(
            Token.id == api_key.token_id,
            Token.user_id == user.id,
            Token.is_active == True,
        )
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(
            status_code=400,
            detail=f"No active {provider} token found for this API key. Select an active token and generate a new API key.",
        )
    if provider is not None and provider != (token.provider or "whatsapp"):
        raise HTTPException(400, "Requested provider does not match the API key's linked token")
    return token


def connection_sender(token: Token):
    provider = token.provider or "whatsapp"
    try:
        if provider == "relay":
            return get_sender(provider, decrypt_token(token.api_token), session_id=token.relay_session_id,
                              base_url=connection_base_url(token))
        return get_sender(provider, decrypt_token(token.api_token))
    except ValueError as exc:
        raise HTTPException(400, "Invalid provider or Relay session configuration") from exc


async def get_api_key_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ApiKeyContext:
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    raw_key = auth.removeprefix("Bearer ")

    parts = raw_key.split("_", 2)
    if len(parts) != 3 or parts[0] != "wts":
        raise HTTPException(status_code=401, detail="Invalid API key format")

    try:
        key_id = int(parts[1])
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid API key format")

    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.is_active == True)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    if not verify_password(raw_key, api_key.key_hash):
        raise HTTPException(status_code=401, detail="Invalid API key")

    result = await db.execute(select(User).where(User.id == api_key.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    api_key.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    return ApiKeyContext(user=user, api_key=api_key)


@router.post("/send")
async def api_send(
    body: SendMessageRequest,
    context: ApiKeyContext = Depends(get_api_key_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    group_id = body.group_id or body.number
    message = body.message
    provider = body.provider

    if not group_id:
        raise HTTPException(status_code=400, detail="group_id is required")

    token = await get_linked_token(context, db, provider)

    sender = connection_sender(token)
    provider = token.provider or "whatsapp"
    if provider == "relay":
        try:
            sender.validate_text(group_id, message)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        token.last_used_at = datetime.now(timezone.utc)
        return await submit_relay(
            db, context, token, group_id, {"recipient": group_id, "text": message}, idempotency_key,
            lambda key: sender.enqueue_text(group_id, message, key),
        )

    try:
        resp = await sender.send(group_id, message)
    except Exception as e:
        raise HTTPException(status_code=502, detail=_upstream_error(e, provider))

    token.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    return {"success": True, "group_id": group_id, "data": resp}


@router.get("/groups")
async def api_groups(
    context: ApiKeyContext = Depends(get_api_key_user),
    db: AsyncSession = Depends(get_db),
):
    token = await get_linked_token(context, db)
    sender = connection_sender(token)

    try:
        groups = await sender.get_groups()
    except Exception as e:
        raise HTTPException(status_code=502, detail=_upstream_error(e, token.provider or "whatsapp"))

    return {
        "success": True,
        "groups": [
            {"id": group.get("id"), "name": group.get("name") or group.get("id")}
            for group in groups
            if group.get("id")
        ],
    }


@router.post("/send/media")
async def api_send_media(
    group_id: Annotated[str, Form(min_length=1)],
    file: Annotated[UploadFile, File()],
    caption: Annotated[str, Form()] = "",
    context: ApiKeyContext = Depends(get_api_key_user),
    db: AsyncSession = Depends(get_db),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    token = await get_linked_token(context, db, "relay")
    sender = connection_sender(token)
    filename = Path(file.filename or "").name
    if Path(filename).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".mp4"}:
        raise HTTPException(400, "Relay supports JPEG, PNG, WebP and MP4 attachments")
    if len(filename.encode()) > 255 or len(caption.encode()) > 1024:
        raise HTTPException(400, "Relay filename exceeds 255 bytes or caption exceeds 1024 bytes")
    path = await save_upload(file)
    if not path:
        raise HTTPException(400, "A media file is required")
    try:
        try:
            sender.validate_media(group_id, path, caption)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        token.last_used_at = datetime.now(timezone.utc)
        return await submit_relay(
            db, context, token, group_id,
            {"recipient": group_id, "caption": caption, "filename": filename, "sha256": digest.hexdigest()},
            idempotency_key,
            lambda key: sender.enqueue_media(group_id, path, caption, key, filename=filename),
        )
    finally:
        Path(path).unlink(missing_ok=True)


@router.get("/connection")
async def api_connection(
    context: ApiKeyContext = Depends(get_api_key_user),
    db: AsyncSession = Depends(get_db),
):
    token = await get_linked_token(context, db)
    provider = token.provider or "whatsapp"
    result = {"provider": provider, "token_id": token.id, "name": token.name}
    if provider == "relay":
        try:
            result["data"] = await connection_sender(token).get_status()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, _upstream_error(exc, provider)) from exc
    return result


@router.get("/operations/{operation_id}")
async def api_operation(
    operation_id: UUID,
    context: ApiKeyContext = Depends(get_api_key_user),
    db: AsyncSession = Depends(get_db),
):
    operation = (await db.execute(select(RelayOperation).where(
        RelayOperation.id == str(operation_id),
        RelayOperation.user_id == context.user.id,
        RelayOperation.api_key_id == context.api_key.id,
        RelayOperation.key_fingerprint == key_fingerprint(context.api_key),
    ))).scalar_one_or_none()
    if not operation:
        raise HTTPException(404, "Operation not found")
    if not operation.upstream_message_id or operation.status in {"sent", "failed"}:
        return operation_response(operation, polling=True)
    token = (await db.execute(select(Token).where(
        Token.id == operation.token_id, Token.user_id == context.user.id, Token.is_active.is_(True),
    ))).scalar_one_or_none()
    if not token:
        raise HTTPException(409, "The operation's connection is missing or inactive")
    if (token.provider != "relay" or token.relay_session_id != operation.session_id
            or connection_base_url(token) != operation.relay_base_url):
        raise HTTPException(409, "The operation's connection was changed")
    try:
        data = await connection_sender(token).get_message(operation.upstream_message_id)
    except (httpx.HTTPError, ValueError) as exc:
        # Leave accepted sends untouched when polling fails.
        raise HTTPException(502, _upstream_error(exc, "relay")) from exc
    await db.execute(update(RelayOperation).where(
        RelayOperation.id == operation.id,
        RelayOperation.status.not_in(["sent", "failed"]),
    ).values(status="queued" if data["status"] == "pending" else data["status"], response=json.dumps(data))
        .execution_options(synchronize_session=False))
    await db.commit()
    await db.refresh(operation)
    return operation_response(operation, polling=True)
