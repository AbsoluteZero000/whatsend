import asyncio
import hashlib
import hmac

import pytest
from fastapi import HTTPException

from app.routers import webhooks
from app.services.meta_sender import MetaCloudSender


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"messages": [{"id": "wamid.1"}]}


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return FakeResponse()


class FakeWebhookRequest:
    def __init__(self, body: bytes, signature: str):
        self._body = body
        self.headers = {"X-Hub-Signature-256": signature}

    async def body(self):
        return self._body


def test_meta_sender_uses_supported_cloud_api_shape(monkeypatch):
    monkeypatch.setattr("app.services.meta_sender.httpx.AsyncClient", FakeClient)
    sender = MetaCloudSender("access-token", "phone-id")
    result = asyncio.run(sender.send_text("201000000000", "hello"))
    assert result["messages"][0]["id"] == "wamid.1"


def test_meta_webhook_rejects_invalid_signature(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "meta_app_secret", "app-secret")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks.receive_meta_webhook(FakeWebhookRequest(b"{}", "sha256=wrong")))
    assert exc.value.status_code == 403


def test_meta_webhook_accepts_valid_signature(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "meta_app_secret", "app-secret")
    body = b'{"object":"whatsapp_business_account"}'
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    result = asyncio.run(webhooks.receive_meta_webhook(FakeWebhookRequest(body, signature)))
    assert result == {"received": True}
