"""Shared pieces of the "Connect Facebook" and "Connect Instagram" sign-in flows."""
import secrets
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.token import Token
from app.providers._meta.graph import dump_credentials, parse_credentials
from app.services.crypto import decrypt_token, encrypt_token
from app.services.oauth import public_base_url

STATE_COOKIE = "social_oauth_state"


def callback_url(request: Request, prefix: str) -> str:
    return public_base_url(request) + f"{prefix}/callback"


def start_login(dialog_url: str, query: dict, prefix: str) -> RedirectResponse:
    """Redirect to the sign-in dialog with a one-time state value that the callback must echo."""
    state = secrets.token_urlsafe(32)
    response = RedirectResponse(f"{dialog_url}?{urlencode({**query, 'state': state})}", status_code=303)
    response.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, secure=settings.cookie_secure,
                        samesite="lax", path=prefix)
    return response


def state_matches(request: Request, state: str) -> bool:
    expected = request.cookies.get(STATE_COOKIE, "")
    return bool(expected and state and secrets.compare_digest(expected, state))


def back_to_connections(prefix: str, **message: str) -> RedirectResponse:
    response = RedirectResponse("/tokens?" + urlencode(message), status_code=303)
    response.delete_cookie(STATE_COOKIE, path=prefix)
    return response


async def save_accounts(db: AsyncSession, user_id: int, provider: str, accounts: list[dict]) -> None:
    """Add new connections and refresh the login of ones this person already connected.

    Each account is {"id", "name", "token"} plus an optional "expires_at" (Unix time).
    """
    existing = {}
    rows = (await db.execute(select(Token).where(Token.user_id == user_id, Token.provider == provider))).scalars().all()
    for token in rows:
        try:
            existing[parse_credentials(decrypt_token(token.api_token))[1]] = token
        except Exception:
            continue
    for account in accounts:
        credentials = encrypt_token(dump_credentials(account["token"], account["id"], account.get("expires_at")))
        token = existing.get(account["id"])
        if token:
            token.api_token = credentials
        else:
            db.add(Token(user_id=user_id, name=account["name"][:120], provider=provider, api_token=credentials))
    await db.commit()
