"""Compatibility exports for the built-in WhatsApp provider client."""
from app.providers.native.sender import (
    CONNECTION_LOST,
    NativeSender,
    WhatsAppProtocolError,
    normalize_base_url,
    worker_error_code,
)

__all__ = ["CONNECTION_LOST", "NativeSender", "WhatsAppProtocolError", "normalize_base_url", "worker_error_code"]
