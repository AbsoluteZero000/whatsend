"""Compatibility imports and the shared provider sender factory."""
from app.providers.base import ProviderContext
from app.providers.registry import get_registry
from app.providers.whatsapp.sender import WhatsAppSender

__all__ = ["WhatsAppSender", "get_sender"]


def get_sender(provider: str, api_token: str, *, session_id: str | None = None, base_url: str | None = None):
    # base_url is retained for old callers; provider-controlled endpoints only.
    return get_registry().sender(provider, ProviderContext(api_token, session_id))
