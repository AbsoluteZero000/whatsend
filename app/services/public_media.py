"""Signed, expiring links to uploaded media, for services that fetch files themselves (Instagram)."""
import hashlib
import hmac
import time
from pathlib import Path
from urllib.parse import quote

from app.config import settings

LINK_LIFETIME_SECONDS = 3600
_KEY = hashlib.sha256(f"public-media:{settings.secret_key}".encode()).digest()


def _signature(filename: str, expires: int) -> str:
    return hmac.new(_KEY, f"{filename}:{expires}".encode(), hashlib.sha256).hexdigest()


def public_media_url(path: str, lifetime: int = LINK_LIFETIME_SECONDS, now: float | None = None) -> str:
    if not settings.public_url:
        raise ValueError("PUBLIC_URL must be set to share media with other services")
    filename = Path(path).name
    expires = int(now if now is not None else time.time()) + lifetime
    return f"{settings.public_url.rstrip('/')}/media/public/{expires}/{_signature(filename, expires)}/{quote(filename)}"


def verify_public_media(filename: str, expires: int, signature: str, now: float | None = None) -> bool:
    if expires < (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(_signature(filename, expires), signature)
