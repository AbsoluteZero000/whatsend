import pytest

from app.services.sender import WhatsAppSender, get_sender


def test_get_sender_returns_whatsapp_sender():
    sender = get_sender("whatsapp", "token")

    assert isinstance(sender, WhatsAppSender)


def test_get_sender_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unsupported provider"):
        get_sender("signal", "token")
