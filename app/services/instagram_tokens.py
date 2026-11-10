"""Renews Instagram logins, which expire 60 days after they are issued or last renewed."""
import logging
import time

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models.token import Token
from app.providers._meta.graph import dump_credentials, load_credentials
from app.services.crypto import decrypt_token, encrypt_token

logger = logging.getLogger("app.instagram_tokens")
# Instagram only renews tokens that are at least a day old, so wait until a third of the lifetime is left.
RENEW_WHEN_DAYS_LEFT = 20


async def renew_instagram_tokens(now: float | None = None) -> int:
    now = now if now is not None else time.time()
    renewed = 0
    async with async_session() as db:
        tokens = (await db.execute(select(Token).where(Token.provider == "instagram"))).scalars().all()
        for token in tokens:
            try:
                credentials = load_credentials(decrypt_token(token.api_token), "Instagram")
            except Exception:
                continue
            expires_at = credentials.get("exp")
            if not expires_at or expires_at < now or expires_at - now > RENEW_WHEN_DAYS_LEFT * 86400:
                continue
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.get(
                        f"{settings.instagram_graph_base_url.rstrip('/')}/refresh_access_token",
                        params={"grant_type": "ig_refresh_token", "access_token": credentials["t"]},
                    )
                response.raise_for_status()
                body = response.json()
                token.api_token = encrypt_token(dump_credentials(
                    body["access_token"], credentials["id"], int(now) + int(body.get("expires_in") or 60 * 86400)))
                renewed += 1
            except (httpx.HTTPError, KeyError, ValueError):
                # The error text can contain the request URL, which holds the token; log only the connection.
                logger.warning("instagram login renewal failed", extra={"token_id": token.id})
        await db.commit()
    return renewed
