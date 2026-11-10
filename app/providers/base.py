"""Provider contracts. This module must not import routes, models or the scheduler."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Literal


class ProviderError(ValueError):
    """Sanitized provider failure; uncertain sends must never be retried blindly."""
    def __init__(self, message: str, *, unknown: bool = False):
        super().__init__(message)
        self.unknown = unknown


class UnsupportedCapability(ValueError):
    """The provider cannot handle the requested message type."""


class BaseSender(ABC):
    """Direct delivery adapter; a successful send means delivery was accepted.

    Ordinary exceptions use the scheduler's existing retry policy. Raise a
    sanitized ProviderError for definitive or uncertain outcomes that must not
    be retried automatically.
    """

    @abstractmethod
    async def send(self, chat_id: str, message: str, image_path: str | None = None,
                   filename: str | None = None) -> dict:
        """Send to an opaque provider recipient ID. Do not log credentials."""

    async def get_groups(self) -> list[dict]:
        """Optional recipient discovery: dictionaries with id and name keys."""
        return []

    def is_recipient_id(self, value: str) -> bool:
        """Optional recognition for assistant tools; names still use discovery."""
        return False

    def validate_payload(self, recipient: str, message: str, image_path: str | None = None) -> None:
        """Override for provider limits; validation must not send messages."""
        if not recipient.strip():
            raise ValueError("A recipient is required")


@dataclass(frozen=True)
class ProviderContext:
    api_token: str = ""
    session_id: str | None = None


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    create_sender: Callable[[ProviderContext], object]
    supports_media: bool = False
    supports_groups: bool = False
    auth: Literal["token", "managed"] = "token"
    # The worker path has persistence semantics specific to built-in WhatsApp.
    delivery: Literal["direct", "whatsapp_worker"] = "direct"
    kind: Literal["messaging", "publishing"] = "messaging"
    credential_label: str = "Provider API Token"
    credential_help: str = ""
    validate_credentials: Callable[[str], None] | None = None

    def validate_payload(self, sender, recipient: str, message: str, image_path: str | None = None) -> None:
        if image_path and not self.supports_media:
            raise UnsupportedCapability(f"{self.label} does not support attachments")
        sender.validate_payload(recipient, message, image_path)
