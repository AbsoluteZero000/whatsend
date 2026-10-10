"""Durable, at-most-one automatic submission to Relay for each caller key."""
import hashlib
import json
from uuid import uuid4
from datetime import datetime, timezone

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.relay_operation import RelayOperation
from app.config import settings
from app.services.relay_sender import normalize_base_url


def connection_base_url(token) -> str:
    return normalize_base_url(token.relay_base_url or settings.relay_base_url)


def key_fingerprint(api_key) -> str:
    # SQLite may reuse a deleted key's integer ID; credential identity must not.
    return hashlib.sha256(api_key.key_hash.encode()).hexdigest()


def operation_response(operation: RelayOperation, *, polling: bool = False) -> JSONResponse:
    data = json.loads(operation.response) if operation.response else None
    state = operation.status
    if state == "submitting" and operation.created_at:
        age = datetime.now(timezone.utc) - operation.created_at.replace(tzinfo=timezone.utc)
        if age.total_seconds() > 60:
            state = "unknown"
            data = {"error": {"message": "Submission was interrupted; do not resend automatically"}}
    status_code = 200 if polling or state == "sent" else 502 if state == "failed" else 202
    return JSONResponse(status_code=status_code, content={
        "success": state not in {"failed", "unknown"},
        "provider": "relay", "group_id": operation.recipient,
        "operation_id": operation.id, "status": state, "data": data,
        "status_url": f"/api/operations/{operation.id}",
    })


async def submit_relay(db, context, token, recipient, payload, caller_key, submit):
    if not caller_key or caller_key != caller_key.strip() or len(caller_key) > 128 or not caller_key.isascii() or not caller_key.isprintable():
        raise HTTPException(400, "Relay requires an Idempotency-Key header of 1–128 printable ASCII characters")
    payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    token_id, session_id = token.id, token.relay_session_id
    relay_url = connection_base_url(token)
    fingerprint = key_fingerprint(context.api_key)
    query = select(RelayOperation).where(
        RelayOperation.user_id == context.user.id,
        RelayOperation.api_key_id == context.api_key.id,
        RelayOperation.key_fingerprint == fingerprint,
        RelayOperation.caller_key == caller_key,
    )

    def replay(operation):
        if (operation.payload_hash != payload_hash or operation.token_id != token_id
                or operation.session_id != session_id or operation.relay_base_url != relay_url):
            raise HTTPException(409, "Idempotency-Key was already used for another payload or connection")
        return operation_response(operation)

    operation = (await db.execute(query)).scalar_one_or_none()
    if operation:
        return replay(operation)
    operation = RelayOperation(
        id=str(uuid4()), user_id=context.user.id, api_key_id=context.api_key.id,
        token_id=token_id, session_id=session_id, caller_key=caller_key, key_fingerprint=fingerprint,
        relay_base_url=relay_url,
        payload_hash=payload_hash, recipient=recipient, status="submitting",
    )
    db.add(operation)
    try:
        # Only the process that commits the unique operation may contact Relay.
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = (await db.execute(query)).scalar_one_or_none()
        if existing is None:
            raise
        return replay(existing)

    try:
        data = await submit(f"echosend:{operation.id}")
    except Exception as exc:
        # A timeout, malformed acknowledgement, or server failure may follow an
        # accepted send. Never generate a new key or enqueue again automatically.
        definitive = isinstance(exc, httpx.HTTPStatusError) and 400 <= exc.response.status_code < 500
        if definitive and exc.response.status_code == 409:
            try:
                definitive = exc.response.json().get("error", {}).get("code") != "idempotency_conflict"
            except (ValueError, AttributeError):
                definitive = False
        operation.status = "failed" if definitive else "unknown"
        error = {"message": "Relay rejected the submission" if definitive else "Submission outcome is unknown; do not resend automatically"}
        if isinstance(exc, httpx.HTTPStatusError):
            error["upstream_status"] = exc.response.status_code
            try:
                upstream = exc.response.json().get("error")
                if isinstance(upstream, dict):
                    error["code"] = upstream.get("code")
                    error["message"] = upstream.get("message") or error["message"]
            except (ValueError, AttributeError):
                pass
        operation.response = json.dumps({"error": error})
    else:
        operation.upstream_message_id = data["id"]
        operation.status = "queued"
        operation.response = json.dumps(data)
    await db.commit()
    return operation_response(operation)
