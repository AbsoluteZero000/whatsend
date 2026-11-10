"""Telegram bot messages; never expose token-bearing request URLs in exceptions."""
import mimetypes
import re
from pathlib import Path

import httpx

from app.providers import BaseSender
from app.providers.base import ProviderError


def validate_token(value: str) -> None:
    if len(value) > 256 or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", value):
        raise ValueError("Enter a valid Telegram bot token from @BotFather")


class TelegramSender(BaseSender):
    def __init__(self, token: str, timeout: int = 60):
        validate_token(token)
        self._url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def is_recipient_id(self, value: str) -> bool:
        return bool(re.fullmatch(r"-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{3,}", value))

    def validate_payload(self, recipient, message, image_path=None):
        if not self.is_recipient_id(recipient):
            raise ValueError("Telegram needs a numeric chat ID or @channel username")
        limit = 1024 if image_path else 4096
        if len(message.encode('utf-16-le')) // 2 > limit or (not image_path and not message):
            raise ValueError(f"Telegram {'captions' if image_path else 'messages'} must contain {'0' if image_path else '1'}–{limit} characters")
        if image_path:
            path = Path(image_path)
            limit_bytes = 10 * 1024 * 1024 if path.suffix.lower() in {'.jpg', '.jpeg', '.png'} else 50 * 1024 * 1024
            if not 0 < path.stat().st_size <= limit_bytes:
                raise ValueError("Telegram attachment is empty or exceeds its upload limit")

    async def send(self, chat_id, message, image_path=None, filename=None):
        self.validate_payload(chat_id, message, image_path)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                if not image_path:
                    response = await client.post(f"{self._url}/sendMessage", json={"chat_id": chat_id, "text": message})
                else:
                    path = Path(image_path)
                    method, field = ('sendPhoto', 'photo') if path.suffix.lower() in {'.jpg', '.jpeg', '.png'} else (
                        ('sendVideo', 'video') if path.suffix.lower() == '.mp4' else ('sendDocument', 'document'))
                    with path.open('rb') as stream:
                        response = await client.post(f"{self._url}/{method}",
                            data={"chat_id": chat_id, "caption": message},
                            files={field: (filename or path.name, stream, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')})
        except httpx.RequestError:
            raise ProviderError("Telegram acknowledgement is uncertain; check the chat before resending", unknown=True) from None
        try:
            payload = response.json()
        except ValueError:
            raise ProviderError("Telegram returned an invalid acknowledgement", unknown=True) from None
        if not isinstance(payload, dict) or type(payload.get('ok')) is not bool:
            raise ProviderError("Telegram returned an invalid acknowledgement", unknown=True)
        if response.is_error or payload.get('ok') is not True:
            # Do not echo upstream response bodies, which can include secrets or request data.
            raise ProviderError(f"Telegram rejected the request ({response.status_code}); check bot permissions, chat ID, token and rate limits",
                                unknown=response.status_code >= 500)
        result = payload.get('result')
        if not isinstance(result, dict) or not isinstance(result.get('message_id'), int):
            raise ProviderError("Telegram returned an invalid message acknowledgement", unknown=True)
        return result
