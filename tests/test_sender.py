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


def test_send_routes_pdf_to_document_endpoint(tmp_path):
    document = tmp_path / "report.pdf"
    document.write_bytes(b"%PDF-1.7")
    sender = WhatsAppSender("token")
    sender.send_document = AsyncMock(return_value={"sent": True})

    result = asyncio.run(sender.send("group@g.us", "Quarterly report", str(document)))

    assert result == {"sent": True}
    sender.send_document.assert_awaited_once_with("group@g.us", str(document), caption="Quarterly report")
