import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app.models.apikey import ApiKey
from app.models.token import Token
from app.models.user import User
from app.routers import api
from app.services.auth import hash_password
from app.services.crypto import encrypt_token


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDb:
    def __init__(self, *values):
        self.values = list(values)
        self.commits = 0

    async def execute(self, query):
        return FakeResult(self.values.pop(0))

    async def commit(self):
        self.commits += 1


class FakeRequest:
    def __init__(self, authorization: str | None):
        self.headers = {}
        if authorization:
            self.headers["Authorization"] = authorization


class FakeSender:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def send(self, group_id: str, message: str) -> dict:
        self.calls.append((group_id, message))
        return {"id": "message-1"}

    async def get_groups(self) -> list[dict]:
        return [
            {"id": "120363123456789@g.us", "name": "Main group"},
            {"id": "120363987654321@g.us", "name": None},
        ]


def test_api_key_auth_accepts_valid_bearer_key():
    raw_key = "wts_1_testkey"
    api_key = ApiKey(id=1, user_id=7, key_prefix="wts_1_testkey", key_hash=hash_password(raw_key))
    user = User(id=7, username="api-user", password_hash="hash", is_active=True)
    db = FakeDb(api_key, user)

    result = asyncio.run(api.get_api_key_user(FakeRequest(f"Bearer {raw_key}"), db))

    assert result.user is user
    assert result.api_key is api_key
    assert api_key.last_used_at is not None
    assert db.commits == 1


def test_api_key_auth_rejects_invalid_bearer_key():
    api_key = ApiKey(id=1, user_id=7, key_prefix="wts_1_testkey", key_hash=hash_password("wts_1_testkey"))
    db = FakeDb(api_key)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.get_api_key_user(FakeRequest("Bearer wts_1_wrong"), db))

    assert exc.value.status_code == 401


def test_api_send_uses_group_id_and_linked_whatsapp_token(monkeypatch):
    user = User(id=7, username="api-user", password_hash="hash")
    api_key = ApiKey(id=1, user_id=7, token_id=3, key_prefix="wts_1_testkey", key_hash="hash")
    token = Token(id=3, user_id=7, name="whatsapp", api_token=encrypt_token("whapi-token"))
    db = FakeDb(token)
    sender = FakeSender()
    monkeypatch.setattr(api, "get_sender", lambda provider, api_token: sender)

    result = asyncio.run(
        api.api_send(
            api.SendMessageRequest(group_id="120363123456789@g.us", message="Hello group"),
            context=api.ApiKeyContext(user=user, api_key=api_key),
            db=db,
        )
    )

    assert result == {
        "success": True,
        "group_id": "120363123456789@g.us",
        "data": {"id": "message-1"},
    }
    assert sender.calls == [("120363123456789@g.us", "Hello group")]
    assert token.last_used_at is not None
    assert db.commits == 1


def test_api_send_requires_group_id():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            api.api_send(
                api.SendMessageRequest(message="Hello group"),
                context=api.ApiKeyContext(
                    user=User(id=7, username="api-user", password_hash="hash"),
                    api_key=ApiKey(id=1, user_id=7, token_id=3, key_prefix="wts_1_testkey", key_hash="hash"),
                ),
                db=FakeDb(),
            )
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "group_id is required"


def test_api_groups_returns_linked_token_groups(monkeypatch):
    user = User(id=7, username="api-user", password_hash="hash")
    api_key = ApiKey(id=1, user_id=7, token_id=3, key_prefix="wts_1_testkey", key_hash="hash")
    token = Token(id=3, user_id=7, name="whatsapp", api_token=encrypt_token("whapi-token"))
    db = FakeDb(token)
    sender = FakeSender()
    monkeypatch.setattr(api, "get_sender", lambda provider, api_token: sender)

    result = asyncio.run(
        api.api_groups(
            context=api.ApiKeyContext(user=user, api_key=api_key),
            db=db,
        )
    )

    assert result == {
        "success": True,
        "groups": [
            {"id": "120363123456789@g.us", "name": "Main group"},
            {"id": "120363987654321@g.us", "name": "120363987654321@g.us"},
        ],
    }


def test_upstream_error_includes_whapi_response_detail():
    request = httpx.Request("POST", "https://gate.whapi.cloud/messages/text")
    response = httpx.Response(404, json={"message": "Chat not found"}, request=request)
    exc = httpx.HTTPStatusError("not found", request=request, response=response)

    assert api._upstream_error(exc) == "Whapi rejected the request (404): Chat not found"
