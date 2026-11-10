"""Publishes feed photos and Reels to an Instagram professional (Creator or Business) account.

Uses the Instagram API with Instagram Login (graph.instagram.com), so no Facebook Page
or Facebook account is involved. Instagram downloads the media itself, so each post
hands it a signed, short-lived link to the uploaded file (see app/services/public_media.py).
"""
import asyncio
import time
from pathlib import Path

from app.config import settings
from app.providers._meta import GraphClient, GraphError, parse_credentials
from app.providers.base import BaseSender
from app.services.public_media import public_media_url

PHOTO_EXTENSIONS = {".jpg", ".jpeg"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_CAPTION_CHARACTERS = 2200
MAX_HASHTAGS = 30


class InstagramSender(BaseSender):
    def __init__(self, credentials: str, *, poll_interval: float = 3, photo_timeout: float = 120,
                 video_timeout: float = 600):
        token, self.account_id = parse_credentials(credentials, "Instagram")
        self.graph = GraphClient(token, base_url=settings.instagram_graph_base_url, app_secret="",
                                 service="Instagram")
        self.poll_interval = poll_interval
        self.photo_timeout = photo_timeout
        self.video_timeout = video_timeout

    def validate_payload(self, recipient, message, image_path=None):
        super().validate_payload(recipient, message, image_path)
        if recipient != self.account_id:
            raise ValueError("An Instagram connection can only post to its own account")
        if not image_path:
            raise ValueError("Instagram posts need a photo or video. Attach a JPEG photo or an MP4 or MOV video")
        ext = Path(image_path).suffix.lower()
        if ext not in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
            raise ValueError("Instagram accepts JPEG photos and MP4 or MOV videos. Save the photo as .jpg and attach it again")
        if ext in PHOTO_EXTENSIONS and Path(image_path).stat().st_size > MAX_PHOTO_BYTES:
            raise ValueError("Instagram photos can be up to 8MB")
        if len(message) > MAX_CAPTION_CHARACTERS:
            raise ValueError(f"Instagram captions can have up to {MAX_CAPTION_CHARACTERS} characters")
        if message.count("#") > MAX_HASHTAGS:
            raise ValueError(f"Instagram captions can have up to {MAX_HASHTAGS} hashtags")
        if not settings.public_url:
            raise ValueError("Instagram downloads the photo or video from EchoSend, so PUBLIC_URL must be set "
                             "to this site's address, for example https://echosend.fly.dev")

    async def get_groups(self):
        account = await self.graph.request("GET", "me", params={"fields": "user_id,username"})
        account_id = str(account.get("user_id") or self.account_id)
        username = account.get("username")
        return [{"id": account_id, "name": f"@{username}" if username else account_id}]

    async def send(self, chat_id, message, image_path=None, filename=None):
        self.validate_payload(chat_id, message, image_path)
        url = public_media_url(image_path)
        video = Path(image_path).suffix.lower() in VIDEO_EXTENSIONS
        if video:
            data = {"media_type": "REELS", "video_url": url, "caption": message, "share_to_feed": "true"}
        else:
            data = {"image_url": url, "caption": message}
        container = await self.graph.request("POST", f"{self.account_id}/media", data=data)
        container_id = str(container["id"])
        await self.wait_until_ready(container_id, self.video_timeout if video else self.photo_timeout)
        published = await self.graph.request("POST", f"{self.account_id}/media_publish",
                                             data={"creation_id": container_id})
        return {"id": published.get("id"), "container_id": container_id}

    async def wait_until_ready(self, container_id: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            state = await self.graph.request("GET", container_id, params={"fields": "status_code,status"})
            code = state.get("status_code")
            if code in {"FINISHED", "PUBLISHED"}:
                return
            if code in {"ERROR", "EXPIRED"}:
                detail = state.get("status") or code
                raise GraphError(f"Instagram could not process the media: {detail}")
            if time.monotonic() >= deadline:
                raise GraphError("Instagram is still processing the media. Try sending again in a few minutes")
            await asyncio.sleep(self.poll_interval)
