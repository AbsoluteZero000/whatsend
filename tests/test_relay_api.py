import asyncio
import json
import base64
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.models.apikey import ApiKey
from app.models.relay_operation import RelayOperation
from app.models.token import Token
from app.models.user import User
from app.routers import api, jobs, tokens
from app.services.crypto import encrypt_token
from app.services.crypto import decrypt_token
from app.services.relay_sender import RelaySender
from app.services.relay_api import key_fingerprint
from app.services.relay_sender import normalize_base_url


SESSION = str(uuid4())
MESSAGE = str(uuid4())
RECIPIENT = "120363123456789@g.us"
CLIENT = httpx.AsyncClient


@pytest.mark.parametrize("code,phrase", [
    ("session_unavailable", "no healthy WhatsApp worker"),
    ("active_session_exists", "already has an active session"),
    ("other_conflict", "could not complete this session operation"),
])
def test_pairing_distinguishes_relay_conflicts(tmp_path, monkeypatch, code, phrase):
    def relay(request):
        return httpx.Response(409, json={"error": {"code": code, "message": "upstream-private-detail"}})

    async def scenario(client, sessions, context):
        headers = {"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"}
        for result in [await client.get("/tokens/3/relay/state"),
                       await client.post("/tokens/3/relay/connect", headers=headers)]:
            assert result.status_code == 502 and phrase in result.json()["detail"]
            assert "upstream-private-detail" not in result.text
            if code == "session_unavailable":
                assert "already has" not in result.text

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


@pytest.mark.parametrize("mode", ["register", "login"])
def test_relay_ui_account_setup(tmp_path, monkeypatch, mode):
    calls = []

    def relay(request):
        calls.append(request.url.path)
        if request.url.path == f"/api/v1/auth/{mode}":
            assert json.loads(request.content) == {"email": "user@example.com", "password": "long-test-password"}
            return httpx.Response(200, json={"data": {"access_token": "temporary-jwt"}})
        if request.url.path == "/api/v1/api-keys":
            assert request.headers["authorization"] == "Bearer temporary-jwt"
            assert set(json.loads(request.content)["scopes"]) == {
                "sessions:read", "sessions:write", "chats:read", "messages:read", "messages:write"}
            return httpx.Response(201, json={"data": {"key": "issued-relay-key"}})
        assert request.url.path == "/api/v1/sessions"
        assert request.headers["authorization"] == "Bearer issued-relay-key"
        return httpx.Response(201, json={"data": {"id": MESSAGE}})

    async def scenario(client, sessions, context):
        page = await client.get("/tokens/relay/setup")
        assert page.status_code == 200 and 'type="password"' in page.text
        assert page.headers["cache-control"] == "no-store, private"
        data = {"relay_base_url": "https://relay.test", "mode": mode, "email": "user@example.com",
                "password": "long-test-password", "name": "UI account"}
        assert (await client.post("/tokens/relay/setup", data=data)).status_code == 403
        assert not calls
        result = await client.post("/tokens/relay/setup", data=data,
                                   headers={"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"})
        assert result.status_code == 303 and result.headers["location"].endswith("/pair")
        async with sessions() as db:
            token = (await db.execute(select(Token).where(Token.name == "UI account"))).scalar_one()
            assert token.user_id == 7 and token.relay_session_id == MESSAGE
            assert token.api_token != "issued-relay-key" and decrypt_token(token.api_token) == "issued-relay-key"
        assert "long-test-password" not in result.text and "temporary-jwt" not in result.text
        assert len(calls) == 3

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


@pytest.mark.parametrize("code", [401, 409, 500])
def test_relay_registration_error_safe_render(tmp_path, monkeypatch, code):
    def relay(request):
        return httpx.Response(code, json={"error": {"message": "long-test-password temporary-jwt"}})

    async def scenario(client, sessions, context):
        result = await client.post("/tokens/relay/setup", data={
            "relay_base_url": "https://relay.test", "email": "user@example.com", "password": "long-test-password",
        }, headers={"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"})
        assert result.status_code == 400
        assert "long-test-password" not in result.text and "temporary-jwt" not in result.text
        assert 'value="user@example.com"' in result.text
        assert result.headers["cache-control"] == "no-store, private"
        async with sessions() as db:
            assert len((await db.execute(select(Token))).scalars().all()) == 1

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_relay_setup_retains_key_when_session_creation_fails(tmp_path, monkeypatch):
    def relay(request):
        if "/auth/" in request.url.path:
            return httpx.Response(200, json={"data": {"access_token": "jwt"}})
        if request.url.path.endswith("api-keys"):
            return httpx.Response(201, json={"data": {"key": "retained-key"}})
        return httpx.Response(409, json={"error": {"code": "active_session_exists"}})

    async def scenario(client, sessions, context):
        headers = {"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"}
        result = await client.post("/tokens/relay/setup", data={
            "relay_base_url": "https://relay.test", "email": "user@example.com", "password": "long-test-password",
            "name": "Recoverable",
        }, headers=headers)
        assert result.status_code == 303 and "/edit" in result.headers["location"]
        async with sessions() as db:
            token = (await db.execute(select(Token).where(Token.name == "Recoverable"))).scalar_one()
            assert token.relay_session_id is None and decrypt_token(token.api_token) == "retained-key"
            token_id = token.id
        page = await client.get(f"/tokens/{token_id}/edit")
        assert 'id="relay_session_id" name="relay_session_id" value="" readonly' not in page.text
        result = await client.post(f"/tokens/{token_id}/edit", data={
            "provider": "relay", "relay_base_url": "https://relay.test", "relay_session_id": SESSION,
        }, headers=headers)
        assert result.status_code == 303 and result.headers["location"].endswith("/pair")
        async with sessions() as db:
            assert (await db.get(Token, token_id)).relay_session_id == SESSION

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


async def exercise(tmp_path, monkeypatch, handler, scenario):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    context = api.ApiKeyContext(
        User(id=7, username="relay-test", password_hash="hash", is_active=True),
        ApiKey(id=1, user_id=7, token_id=3, key_prefix="test", key_hash="hash", is_active=True),
    )
    async with sessions() as db:
        db.add(context.user)
        await db.flush()
        db.add(Token(id=3, user_id=7, name="Relay", provider="relay", relay_session_id=SESSION,
                     relay_base_url="https://relay.test",
                     api_token=encrypt_token("relay-secret"), is_active=True))
        await db.flush()
        db.add(context.api_key)
        await db.commit()

    async def database():
        async with sessions() as db:
            yield db

    app = FastAPI()
    app.include_router(api.router)
    app.include_router(tokens.router)
    from app.main import render
    app.state.render = render
    monkeypatch.setattr(tokens, "require_user", lambda request: {"sub": str(context.user.id)})
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[api.get_api_key_user] = lambda: context
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: CLIENT(transport=httpx.MockTransport(handler), **kwargs))
    try:
        async with CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await scenario(client, sessions, context)
    finally:
        await engine.dispose()


def text_request(**overrides):
    return {"group_id": RECIPIENT, "message": "Hello", **overrides}


def test_relay_enqueue_replay_and_delivery_status(tmp_path, monkeypatch):
    calls = []

    def relay(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer relay-secret"
        assert request.url.host == "relay.test"
        if request.method == "POST":
            assert json.loads(request.content) == {"recipient": RECIPIENT, "text": "Hello"}
            assert request.headers["Idempotency-Key"].startswith("echosend:")
            return httpx.Response(202, json={"data": {"id": MESSAGE, "status": "pending"}})
        assert request.url.path == f"/api/v1/sessions/{SESSION}/messages/{MESSAGE}"
        return httpx.Response(200, json={"data": {"id": MESSAGE, "status": "sent"}})

    async def scenario(client, sessions, context):
        headers = {"Idempotency-Key": "event-1"}
        first = await client.post("/api/send", json=text_request(), headers=headers)
        assert first.status_code == 202
        assert first.json()["status"] == "queued"
        replay = await client.post("/api/send", json=text_request(), headers=headers)
        assert replay.json() == first.json()
        conflict = await client.post("/api/send", json=text_request(message="Changed"), headers=headers)
        assert conflict.status_code == 409
        status = await client.get(first.json()["status_url"])
        assert status.status_code == 200 and status.json()["status"] == "sent"
        # New sessions read the committed result; terminal status requires no further upstream call.
        assert (await client.get(first.json()["status_url"])).json() == status.json()
        async with sessions() as db:
            operation = await db.get(RelayOperation, first.json()["operation_id"])
            assert operation.status == "sent" and operation.upstream_message_id == MESSAGE
        assert len(calls) == 2
        context.api_key.key_hash = "another-credential-at-same-integer-id"
        assert (await client.get(first.json()["status_url"])).status_code == 404
        context.api_key.key_hash = "hash"
        context.api_key.id = 99
        assert (await client.get(first.json()["status_url"])).status_code == 404

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


@pytest.mark.parametrize("failure", ["timeout", "server", "conflict", "malformed"])
def test_uncertain_submission_never_automatically_resends(tmp_path, monkeypatch, failure):
    calls = []

    def relay(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("lost response", request=request)
        if failure == "malformed":
            return httpx.Response(202, json={"data": {"id": "invalid", "status": "pending"}})
        code = "idempotency_conflict" if failure == "conflict" else "internal_error"
        return httpx.Response(409 if failure == "conflict" else 500, json={"error": {"code": code, "message": "Upstream error"}})

    async def scenario(client, sessions, context):
        headers = {"Idempotency-Key": "uncertain"}
        first = await client.post("/api/send", json=text_request(), headers=headers)
        assert first.status_code == 202 and first.json()["status"] == "unknown"
        assert first.json()["success"] is False
        assert (await client.post("/api/send", json=text_request(), headers=headers)).json() == first.json()
        assert (await client.get(first.json()["status_url"])).json()["status"] == "unknown"
        assert len(calls) == 1

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_poll_failure_preserves_accepted_send(tmp_path, monkeypatch):
    calls = []

    def relay(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"data": {"id": MESSAGE, "status": "pending"}})
        raise httpx.ConnectError("offline", request=request)

    async def scenario(client, sessions, context):
        headers = {"Idempotency-Key": "poll"}
        first = await client.post("/api/send", json=text_request(), headers=headers)
        assert (await client.get(first.json()["status_url"])).status_code == 502
        replay = await client.post("/api/send", json=text_request(), headers=headers)
        assert replay.json()["status"] == "queued"
        assert len(calls) == 2

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


@pytest.mark.parametrize("upstream_state", ["pending", "processing", "failed"])
def test_delivery_states_are_reported_without_reenqueue(tmp_path, monkeypatch, upstream_state):
    def relay(request):
        return httpx.Response(202 if request.method == "POST" else 200, json={"data": {
            "id": MESSAGE, "status": "pending" if request.method == "POST" else upstream_state,
        }})

    async def scenario(client, sessions, context):
        first = await client.post("/api/send", json=text_request(), headers={"Idempotency-Key": "states"})
        status = await client.get(first.json()["status_url"])
        assert status.status_code == 200
        assert status.json()["status"] == ("queued" if upstream_state == "pending" else upstream_state)
        assert status.json()["success"] is (upstream_state != "failed")

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_bad_inputs_and_provider_mismatch_do_not_contact_relay(tmp_path, monkeypatch):
    def relay(request):
        pytest.fail("Invalid input contacted Relay")

    async def scenario(client, sessions, context):
        assert (await client.post("/api/send", json=text_request())).status_code == 400
        for payload in [text_request(provider="whatsapp"), text_request(message="ع" * 2049), text_request(group_id=" " * 2)]:
            result = await client.post("/api/send", json=payload, headers={"Idempotency-Key": "invalid"})
            assert result.status_code == 400
        async with sessions() as db:
            assert (await db.execute(select(RelayOperation))).scalars().all() == []

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_relay_groups_fetch_every_page_and_use_external_ids(tmp_path, monkeypatch):
    cursors = []

    def relay(request):
        assert request.url.path.endswith("/groups")
        cursor = request.url.params["cursor"]
        cursors.append(cursor)
        return httpx.Response(200, json={"data": [{
            "id": str(uuid4()), "external_chat_id": "two@g.us" if cursor else "one@g.us", "name": None if cursor else "One",
        }], "next_cursor": "page-two" if not cursor else ""})

    async def scenario(client, sessions, context):
        result = await client.get("/api/groups")
        assert result.json()["groups"] == [{"id": "one@g.us", "name": "One"}, {"id": "two@g.us", "name": "two@g.us"}]
        assert cursors == ["", "page-two"]

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_concurrent_replay_contacts_relay_once(tmp_path, monkeypatch):
    calls = []

    async def relay(request):
        calls.append(request)
        await asyncio.sleep(0.05)
        return httpx.Response(202, json={"data": {"id": MESSAGE, "status": "pending"}})

    async def scenario(client, sessions, context):
        results = await asyncio.gather(*[
            client.post("/api/send", json=text_request(), headers={"Idempotency-Key": "concurrent"}) for _ in range(2)
        ])
        assert [result.status_code for result in results] == [202, 202]
        assert results[0].json()["operation_id"] == results[1].json()["operation_id"]
        assert len(calls) == 1

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_media_upload_mapping_validation_and_cleanup(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "UPLOAD_DIR", tmp_path)

    def relay(request):
        calls.append(request)
        assert b'name="file"' in request.content
        assert b'filename="photo.png"' in request.content
        assert b'name="recipient"' in request.content
        assert b'name="caption"' in request.content
        assert b"A caption" in request.content
        return httpx.Response(202, json={"data": {"id": MESSAGE, "status": "pending"}})

    async def scenario(client, sessions, context):
        fields = {"group_id": RECIPIENT, "caption": "A caption"}
        headers = {"Idempotency-Key": "media"}
        content = b"\x89PNG\r\n\x1a\n" + b"image"
        for _ in range(2):
            result = await client.post("/api/send/media", data=fields, files={"file": ("photo.png", content, "image/png")}, headers=headers)
            assert result.status_code == 202 and result.json()["status"] == "queued"
        assert len(calls) == 1
        assert not list(tmp_path.glob("*.png"))
        bad = await client.post("/api/send/media", data=fields, files={"file": ("photo.png", b"fake", "image/png")}, headers=headers)
        assert bad.status_code == 400
        pdf = await client.post("/api/send/media", data=fields, files={"file": ("report.pdf", b"%PDF", "application/pdf")}, headers=headers)
        assert pdf.status_code == 400

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_relay_connection_configuration():
    assert tokens.validate_connection("relay", SESSION.upper()) == SESSION
    assert tokens.validate_connection("whatsapp", "") is None
    with pytest.raises(Exception, match="valid Relay"):
        tokens.validate_connection("relay", "invalid")


def test_relay_limits_are_utf8_bytes():
    RelaySender.validate_text(RECIPIENT, "ع" * 2048)
    with pytest.raises(ValueError, match="4096"):
        RelaySender.validate_text(RECIPIENT, "ع" * 2049)


def test_interrupted_submission_survives_restart_without_resend(tmp_path, monkeypatch):
    def relay(request):
        pytest.fail("Interrupted operation was automatically resubmitted")

    async def scenario(client, sessions, context):
        import hashlib
        payload = {"recipient": RECIPIENT, "text": "Hello"}
        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        identifier = str(uuid4())
        async with sessions() as db:
            db.add(RelayOperation(
                id=identifier, user_id=7, api_key_id=1, key_fingerprint=key_fingerprint(context.api_key),
                token_id=3, session_id=SESSION, caller_key="interrupted", payload_hash=payload_hash,
                relay_base_url="https://relay.test",
                recipient=RECIPIENT, status="submitting", created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
            ))
            await db.commit()
        response = await client.post("/api/send", json=text_request(), headers={"Idempotency-Key": "interrupted"})
        assert response.status_code == 202 and response.json()["status"] == "unknown"
        assert (await client.get(f"/api/operations/{identifier}")).json()["status"] == "unknown"

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


@pytest.mark.parametrize("value", ["", "file:///etc/passwd", "https://relay.test/api/v1", "https://user:password@relay.test", "https://relay.test?token=x", "https://relay.test#fragment", "https://relay.test\n/api"])
def test_relay_backend_url_rejects_invalid_origins(value):
    with pytest.raises(ValueError):
        normalize_base_url(value)


def test_relay_backend_url_normalizes_origin():
    assert tokens.validate_relay_url("relay", " https://Relay.Test:8080/ ") == "https://relay.test:8080"
    assert tokens.validate_relay_url("whatsapp", "") is None


def test_relay_connection_status_uses_site_url(tmp_path, monkeypatch):
    def relay(request):
        assert request.url.host == "relay.test"
        assert request.url.path == f"/api/v1/sessions/{SESSION}/status"
        return httpx.Response(200, json={"data": {"id": SESSION, "state": "connected"}})

    async def scenario(client, sessions, context):
        result = await client.get("/api/connection")
        assert result.status_code == 200 and result.json()["data"]["state"] == "connected"

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_pairing_page_connect_qr_and_connected_state(tmp_path, monkeypatch):
    state = {"value": "disconnected"}
    calls = []
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"test-qr").decode()

    def relay(request):
        calls.append(request)
        assert request.url.host == "relay.test"
        assert request.headers["authorization"] == "Bearer relay-secret"
        if request.url.path.endswith("/connect"):
            assert request.method == "POST"
            state["value"] = "qr_required"
            return httpx.Response(200, json={"data": {"id": SESSION, "state": "qr_required"}})
        if request.url.path.endswith("/qr"):
            return httpx.Response(200, json={"data": {
                "png_base64": encoded, "value": "private-pairing-string",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            }})
        return httpx.Response(200, json={"data": {"id": SESSION, "state": state["value"]}})

    async def scenario(client, sessions, context):
        page = await client.get("/tokens/3/pair")
        assert page.status_code == 200
        assert "relay-pairing.js" in page.text and "Get QR code" in page.text
        assert page.headers["cache-control"] == "no-store, private"
        assert "relay-secret" not in page.text and calls == []
        assert (await client.post("/tokens/3/relay/connect")).status_code == 403
        result = await client.post("/tokens/3/relay/connect", headers={"X-CSRF-Token": "safe", "Cookie": "csrf_token=safe"})
        assert result.status_code == 200 and result.json() == {"state": "qr_required"}
        qr = await client.get("/tokens/3/relay/state")
        assert qr.json()["qr"]["png_base64"] == encoded
        assert "private-pairing-string" not in qr.text
        assert qr.headers["cache-control"] == "no-store, private"
        state["value"] = "connected"
        result = await client.get("/tokens/3/relay/state")
        assert result.json() == {"state": "connected", "qr": None}
        assert sum(request.url.path.endswith("/qr") for request in calls) == 1
        assert sum(request.method == "POST" for request in calls) == 1

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_pairing_is_owned_and_requires_active_relay_token(tmp_path, monkeypatch):
    def relay(request):
        pytest.fail("Invalid pairing connection contacted Relay")

    async def scenario(client, sessions, context):
        context.user.id = 8
        assert (await client.get("/tokens/3/pair")).status_code == 404
        assert (await client.get("/tokens/3/relay/state")).status_code == 404
        context.user.id = 7
        async with sessions() as db:
            token = await db.get(Token, 3)
            token.is_active = False
            await db.commit()
        assert (await client.get("/tokens/3/relay/state")).status_code == 400
        async with sessions() as db:
            token = await db.get(Token, 3)
            token.is_active = True
            token.provider = "whatsapp"
            await db.commit()
        assert (await client.get("/tokens/3/pair")).status_code == 400

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_new_relay_token_creates_session_and_redirects_to_pairing(tmp_path, monkeypatch):
    def relay(request):
        assert request.method == "POST" and request.url.path == "/api/v1/sessions"
        assert request.headers["authorization"] == "Bearer new-relay-key"
        return httpx.Response(201, json={"data": {"id": MESSAGE, "state": "disconnected"}})

    async def scenario(client, sessions, context):
        result = await client.post("/tokens/create", data={
            "provider": "relay", "name": "New connection", "api_token": "new-relay-key",
            "relay_base_url": "https://relay.test", "csrf_token": "safe",
        }, headers={"Cookie": "csrf_token=safe"})
        assert result.status_code == 303 and result.headers["location"].endswith("/pair")
        async with sessions() as db:
            token = (await db.execute(select(Token).where(Token.name == "New connection"))).scalar_one()
            assert token.relay_session_id == MESSAGE and token.relay_base_url == "https://relay.test"
            assert token.api_token != "new-relay-key"

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_existing_session_skips_creation(tmp_path, monkeypatch):
    def relay(request):
        pytest.fail("Existing session was recreated")

    async def scenario(client, sessions, context):
        result = await client.post("/tokens/create", data={
            "provider": "relay", "api_token": "relay-key", "relay_base_url": "https://relay.test",
            "relay_session_id": SESSION, "csrf_token": "safe",
        }, headers={"Cookie": "csrf_token=safe"})
        assert result.status_code == 303 and result.headers["location"].endswith("/pair")

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


@pytest.mark.parametrize("bad_qr", [
    {"png_base64": "not-base64!", "expires_at": "2026-01-01T00:00:00Z"},
    {"png_base64": base64.b64encode(b"not a PNG").decode(), "expires_at": "2026-01-01T00:00:00Z"},
    {"png_base64": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode(), "expires_at": "not a date"},
])
def test_invalid_qr_is_not_exposed(tmp_path, monkeypatch, bad_qr):
    def relay(request):
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"data": {"id": SESSION, "state": "qr_required"}})
        return httpx.Response(200, json={"data": bad_qr})

    async def scenario(client, sessions, context):
        result = await client.get("/tokens/3/relay/state")
        assert result.status_code == 502
        assert "png_base64" not in result.text
        assert result.headers["cache-control"] == "no-store, private"

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_pairing_permission_error_does_not_save_token(tmp_path, monkeypatch):
    def relay(request):
        return httpx.Response(403, json={"error": {"code": "insufficient_scope", "message": "Forbidden"}})

    async def scenario(client, sessions, context):
        headers = {"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"}
        result = await client.post("/tokens/3/relay/connect", headers=headers)
        assert result.status_code == 502 and "sessions:write" in result.json()["detail"]
        result = await client.post("/tokens/create", data={
            "provider": "relay", "api_token": "relay-key", "relay_base_url": "https://relay.test",
        }, headers=headers)
        assert result.status_code == 502
        async with sessions() as db:
            assert len((await db.execute(select(Token))).scalars().all()) == 1

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))
