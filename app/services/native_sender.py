"""Private client for WhatSend's built-in WhatsApp Web service."""
from pathlib import Path
from uuid import UUID
from urllib.parse import urlsplit
import base64
import binascii
from datetime import datetime

import httpx
from pydantic import HttpUrl


def normalize_base_url(value: str) -> str:
    value = value.strip()
    if len(value) > 2048 or any(ord(char) <= 32 for char in value):
        raise ValueError("Enter a valid HTTP or HTTPS WhatsApp backend URL")
    normalized = str(HttpUrl(value))
    parts = urlsplit(normalized)
    if parts.username is not None or parts.password is not None or parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ValueError("Enter the WhatsApp backend origin without credentials, path, query or fragment")
    return normalized.rstrip("/")


class WhatsAppProtocolError(ValueError):
    pass


class NativeSender:
    def __init__(self, api_token: str = "", session_id: str = "", base_url: str = "", timeout: int = 30):
        from app.config import settings
        self.session_id = str(UUID(session_id))
        self.base_url = f"{normalize_base_url(settings.whatsapp_worker_url)}/api/v1/sessions/{self.session_id}"
        self.timeout = timeout
        self.headers = {"accept": "application/json", "authorization": f"Bearer {settings.whatsapp_worker_secret}"}

    async def connect(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/connect", headers=self.headers)
        response.raise_for_status()
        return self._session_data(response)

    async def disconnect(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(f"{self.base_url}/disconnect", headers=self.headers)
        response.raise_for_status()
        return self._session_data(response)

    async def delete(self):
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.delete(self.base_url, headers=self.headers)
        response.raise_for_status()

    async def get_qr(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/qr", headers=self.headers)
        response.raise_for_status()
        data = self._data(response)
        try:
            encoded = data["png_base64"]
            if not isinstance(encoded, str) or len(encoded) > 1024 * 1024:
                raise ValueError("invalid image size")
            png = base64.b64decode(encoded, validate=True)
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("invalid PNG")
            expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                raise ValueError("missing timezone")
        except (KeyError, ValueError, TypeError, AttributeError, binascii.Error) as exc:
            raise WhatsAppProtocolError("WhatsApp returned an invalid QR code") from exc
        # The raw pairing value is unnecessary for display and stays server-side.
        return {"png_base64": encoded, "expires_at": expires_at.isoformat()}

    @staticmethod
    def validate_text(recipient: str, text: str):
        if not recipient.strip() or len(recipient.encode("utf-8")) > 128:
            raise ValueError("WhatsApp recipient must contain 1–128 UTF-8 bytes")
        if not text or len(text.encode("utf-8")) > 4096:
            raise ValueError("WhatsApp text must contain 1–4096 UTF-8 bytes")

    async def enqueue_text(self, recipient: str, text: str, idempotency_key: str) -> dict:
        self.validate_text(recipient, text)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/messages",
                headers={**self.headers, "Idempotency-Key": idempotency_key},
                json={"recipient": recipient, "text": text},
            )
        response.raise_for_status()
        data = self._data(response)
        if response.status_code != 202 or data.get("status") != "pending":
            raise WhatsAppProtocolError("WhatsApp did not return a valid enqueue acknowledgement")
        self._message_id(data)
        return data

    @staticmethod
    def validate_media(recipient: str, path: str, caption: str) -> str:
        if not recipient.strip() or len(recipient.encode("utf-8")) > 128:
            raise ValueError("WhatsApp recipient must contain 1–128 UTF-8 bytes")
        if len(caption.encode("utf-8")) > 1024:
            raise ValueError("WhatsApp caption exceeds 1024 UTF-8 bytes")
        file = Path(path)
        types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".mp4": "video/mp4"}
        mime = types.get(file.suffix.lower())
        if not mime:
            raise ValueError("WhatsApp supports JPEG, PNG, WebP and MP4 attachments")
        limit = 50 * 1024 * 1024 if mime == "video/mp4" else 16 * 1024 * 1024
        if not 0 < file.stat().st_size <= limit:
            raise ValueError("Attachment exceeds the WhatsApp/WhatSend upload limit or is empty")
        return mime

    async def enqueue_media(self, recipient: str, path: str, caption: str, idempotency_key: str, *, filename: str | None = None) -> dict:
        mime = self.validate_media(recipient, path, caption)
        file = Path(path)
        with file.open("rb") as stream:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/messages",
                    headers={**self.headers, "Idempotency-Key": idempotency_key},
                    data={"recipient": recipient, "caption": caption},
                    files={"file": (filename or file.name, stream, mime)},
                )
        response.raise_for_status()
        data = self._data(response)
        if response.status_code != 202 or data.get("status") != "pending":
            raise WhatsAppProtocolError("WhatsApp did not return a valid enqueue acknowledgement")
        self._message_id(data)
        return data

    async def get_groups(self) -> list[dict]:
        groups = {}
        cursor = ""
        seen = set()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for _ in range(1000):
                response = await client.get(
                    f"{self.base_url}/groups", headers=self.headers,
                    params={"page_size": 200, "cursor": cursor},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise WhatsAppProtocolError("WhatsApp returned an invalid group page")
                for group in payload["data"]:
                    if not isinstance(group, dict) or not isinstance(group.get("external_chat_id"), str):
                        raise WhatsAppProtocolError("WhatsApp returned an invalid group")
                    recipient = group["external_chat_id"]
                    groups[recipient] = {"id": recipient, "name": group.get("name") or recipient}
                cursor = payload.get("next_cursor")
                if cursor == "":
                    return list(groups.values())
                if not isinstance(cursor, str) or cursor in seen:
                    raise WhatsAppProtocolError("WhatsApp returned an invalid or repeated group cursor")
                seen.add(cursor)
        raise WhatsAppProtocolError("WhatsApp group pagination exceeded its limit")

    async def get_message(self, message_id: str) -> dict:
        message_id = str(UUID(message_id))
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/messages/{message_id}", headers=self.headers)
        response.raise_for_status()
        data = self._data(response)
        if self._message_id(data) != message_id or data.get("status") not in {"pending", "processing", "sent", "failed", "unknown"}:
            raise WhatsAppProtocolError("WhatsApp returned an invalid message status")
        return data

    async def get_status(self) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(f"{self.base_url}/status", headers=self.headers)
        response.raise_for_status()
        return self._session_data(response)

    def _session_data(self, response: httpx.Response) -> dict:
        data = self._data(response)
        if self._message_id(data) != self.session_id or data.get("state") not in {
            "disconnected", "connecting", "qr_required", "connected", "reconnecting", "failed",
        }:
            raise WhatsAppProtocolError("WhatsApp returned an invalid session state")
        return data

    @staticmethod
    def _data(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as exc:
            raise WhatsAppProtocolError("WhatsApp returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise WhatsAppProtocolError("WhatsApp returned an invalid response envelope")
        return payload["data"]

    @staticmethod
    def _message_id(data: dict) -> str:
        try:
            return str(UUID(data["id"]))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise WhatsAppProtocolError("WhatsApp returned an invalid message ID") from exc
