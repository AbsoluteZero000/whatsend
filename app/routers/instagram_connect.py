"""Connect Instagram: Instagram Login for Creator and Business accounts, with no Facebook Page needed.

Instagram logins last 60 days; app/services/instagram_tokens.py renews them daily before they expire.
"""
import time

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.providers._meta.graph import graph_url, raise_for_graph_error
from app.routers.auth import require_user
from app.services.social_connections import (
    back_to_connections, callback_url, save_accounts, start_login, state_matches,
)

PREFIX = "/tokens/instagram"
router = APIRouter(prefix=PREFIX, tags=["tokens"])

DIALOG_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
SCOPES = ("instagram_business_basic", "instagram_business_content_publish")


def instagram_connect_enabled() -> bool:
    return bool(settings.instagram_app_id and settings.instagram_app_secret)


@router.get("/connect")
async def start_instagram_connect(request: Request):
    require_user(request)
    if not instagram_connect_enabled():
        return back_to_connections(PREFIX, error="Instagram isn't set up on this server yet. Set INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET.")
    return start_login(DIALOG_URL, {
        "client_id": settings.instagram_app_id, "redirect_uri": callback_url(request, PREFIX),
        "response_type": "code", "scope": ",".join(SCOPES),
    }, PREFIX)


async def fetch_account(code: str, redirect_uri: str) -> dict:
    """Exchange the login code for a 60-day token and read the account it belongs to."""
    base = settings.instagram_graph_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(TOKEN_URL, data={
            "client_id": settings.instagram_app_id, "client_secret": settings.instagram_app_secret,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code,
        })
        raise_for_graph_error(response, "Instagram")
        body = response.json()
        short_lived = (body.get("data") or [body])[0]["access_token"]
        response = await client.get(f"{base}/access_token", params={
            "grant_type": "ig_exchange_token", "client_secret": settings.instagram_app_secret,
            "access_token": short_lived,
        })
        raise_for_graph_error(response, "Instagram")
        long_lived = response.json()
        token = long_lived["access_token"]
        response = await client.get(graph_url("me", base_url=base), params={"fields": "user_id,username"},
                                    headers={"Authorization": f"Bearer {token}"})
        raise_for_graph_error(response, "Instagram")
    account = response.json()
    account_id = str(account["user_id"])
    username = account.get("username")
    return {"id": account_id, "name": f"@{username}" if username else account_id, "token": token,
            "expires_at": int(time.time()) + int(long_lived.get("expires_in") or 60 * 86400)}


@router.get("/callback")
async def finish_instagram_connect(request: Request, code: str = "", state: str = "", error: str = "",
                                   db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    if not state_matches(request, state):
        return back_to_connections(PREFIX, error="The Instagram sign-in expired. Try connecting again.")
    if error or not code:
        return back_to_connections(PREFIX, error="Instagram connection was cancelled.")
    try:
        account = await fetch_account(code, callback_url(request, PREFIX))
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, httpx.HTTPError) and str(exc) else "Instagram did not respond as expected."
        return back_to_connections(PREFIX, error=f"Could not connect Instagram. {detail} "
                                                 "Personal accounts can't be connected; switch to a Creator or Business account first.")
    await save_accounts(db, int(user["sub"]), "instagram", [account])
    return back_to_connections(PREFIX, success=f"Connected Instagram account {account['name']}.")
