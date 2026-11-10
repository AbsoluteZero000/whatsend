"""Publishes posts to one Facebook Page with that Page's access token."""
import mimetypes
from pathlib import Path

from app.providers._meta import GraphClient, parse_credentials
from app.providers.base import BaseSender

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARACTERS = 63206


class FacebookPageSender(BaseSender):
    def __init__(self, credentials: str):
        token, self.page_id = parse_credentials(credentials)
        self.graph = GraphClient(token)

    def validate_payload(self, recipient, message, image_path=None):
        super().validate_payload(recipient, message, image_path)
        if recipient != self.page_id:
            raise ValueError("A Facebook connection can only post to its own Page")
        if not image_path and not message.strip():
            raise ValueError("A Facebook post needs text, a photo or a video")
        if len(message) > MAX_TEXT_CHARACTERS:
            raise ValueError(f"Facebook posts can have up to {MAX_TEXT_CHARACTERS} characters")
        if image_path:
            ext = Path(image_path).suffix.lower()
            if ext not in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
                raise ValueError("Facebook posts accept JPEG, PNG or GIF photos and MP4, MOV, AVI, MKV or WebM videos")
            if ext in PHOTO_EXTENSIONS and Path(image_path).stat().st_size > MAX_PHOTO_BYTES:
                raise ValueError("Facebook photos can be up to 10MB")

    async def get_groups(self):
        page = await self.graph.request("GET", self.page_id, params={"fields": "id,name"})
        return [{"id": str(page["id"]), "name": page.get("name") or str(page["id"])}]

    async def send(self, chat_id, message, image_path=None, filename=None):
        self.validate_payload(chat_id, message, image_path)
        if not image_path:
            return await self.graph.request("POST", f"{self.page_id}/feed", data={"message": message})
        path = Path(image_path)
        name = filename or path.name
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as fh:
            if path.suffix.lower() in VIDEO_EXTENSIONS:
                return await self.graph.request(
                    "POST", f"{self.page_id}/videos", video=True, timeout=600,
                    data={"description": message}, files={"source": (name, fh, content_type)},
                )
            return await self.graph.request(
                "POST", f"{self.page_id}/photos", timeout=120,
                data={"message": message}, files={"source": (name, fh, content_type)},
            )
