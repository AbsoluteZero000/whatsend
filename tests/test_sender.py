import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services.sender import WhatsAppSender, get_sender


def test_get_sender_returns_whatsapp_sender():
    sender = get_sender("whatsapp", "token")

    assert isinstance(sender, WhatsAppSender)


def test_get_sender_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unsupported provider"):
        get_sender("signal", "token")


def test_native_sender_only_uses_the_private_worker(monkeypatch):
    from app.config import settings
    from app.services.native_sender import NativeSender
    from uuid import uuid4
    monkeypatch.setattr(settings, "whatsapp_worker_url", "http://127.0.0.1:3001")
    monkeypatch.setattr(settings, "whatsapp_worker_secret", "private-secret")
    session = str(uuid4())
    sender = NativeSender("ignored-provider-token", session, "https://untrusted.example.com")
    assert sender.base_url == f"http://127.0.0.1:3001/api/v1/sessions/{session}"
    assert sender.headers["authorization"] == "Bearer private-secret"
    with pytest.raises(ValueError, match="Unsupported provider"):
        get_sender("relay", "old-token")


def test_send_routes_pdf_to_document_endpoint(tmp_path):
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.7")
    sender = WhatsAppSender("token")
    sender.send_document = AsyncMock(return_value={"sent": True})

    result = asyncio.run(sender.send("group@g.us", "Quarterly report", str(document)))

    assert result == {"sent": True}
    sender.send_document.assert_awaited_once_with("group@g.us", str(document), caption="Quarterly report")
