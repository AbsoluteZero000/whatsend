import mimetypes
from pathlib import Path

import httpx

from app.config import settings


class MetaCloudSender:
    """Supported Meta Cloud API adapter used by the direct-integration spike.

    Group-specific endpoints and account eligibility must be verified against the
    target Meta business account before this adapter is enabled in production.
    """

    def __init__(self, access_token: str, phone_number_id: str, timeout: int = 30):
        self.access_token = access_token
        self.phone_number_id = phone_number_id
        self.timeout = timeout
        self.base_url = f"{settings.meta_graph_base_url}/{settings.meta_graph_api_version}"
        self.headers = {"Authorization": f"Bearer {access_token}"}

    async def send_text(self, recipient_id: str, message: str) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": "text",
            "text": {"body": message},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/{self.phone_number_id}/messages",
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
            )
        response.raise_for_status()
        return response.json()

    async def upload_media(self, media_path: str) -> str:
        path = Path(media_path)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as media:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/{self.phone_number_id}/media",
                    headers=self.headers,
                    data={"messaging_product": "whatsapp", "type": media_type},
                    files={"file": (path.name, media, media_type)},
                )
        response.raise_for_status()
        return response.json()["id"]

    async def send(self, recipient_id: str, message: str, image_path: str | None = None) -> dict:
        if not image_path:
            return await self.send_text(recipient_id, message)
        media_id = await self.upload_media(image_path)
        media_kind = "video" if (mimetypes.guess_type(image_path)[0] or "").startswith("video/") else "image"
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_id,
            "type": media_kind,
            media_kind: {"id": media_id, "caption": message},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/{self.phone_number_id}/messages",
                headers={**self.headers, "Content-Type": "application/json"},
                json=payload,
            )
        response.raise_for_status()
        return response.json()
