"""Connect Instagram: adds Creator and Business accounts, with no Facebook Page needed.

Two sign-ins are supported, depending on what the Meta app offers:
- Unified login (INSTAGRAM_LOGIN_CONFIG_ID, the only option for newer Meta apps): Facebook Login for
  Business with an Instagram configuration, on the main Meta app. The person picks which Instagram
  accounts to share; the business token covers all of them and does not expire.
- Instagram Login (INSTAGRAM_APP_ID/INSTAGRAM_APP_SECRET): logins last 60 days and
  app/services/instagram_tokens.py renews them daily before they expire.
"""
import time

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.providers._meta.graph import FACEBOOK_API, appsecret_proof, graph_url, raise_for_graph_error
from app.routers.auth import require_user
from app.services.social_connections import (
    back_to_connections, callback_url, save_accounts, start_login, state_matches,
)

PREFIX = "/tokens/instagram"
router = APIRouter(prefix=PREFIX, tags=["tokens"])

DIALOG_URL = "https://www.instagram.com/oauth/authorize"
UNIFIED_DIALOG_URL = "https://www.facebook.com/{version}/dialog/oauth"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
SCOPES = ("instagram_business_basic", "instagram_business_content_publish")


def unified_login() -> bool:
    return bool(settings.instagram_login_config_id and settings.meta_app_id and settings.meta_app_secret)


def instagram_connect_enabled() -> bool:
    return unified_login() or bool(settings.instagram_app_id and settings.instagram_app_secret)


@router.get("/connect")
async def start_instagram_connect(request: Request):
    require_user(request)
    if not instagram_connect_enabled():
        return back_to_connections(PREFIX, error="Instagram isn't set up on this server yet. Set INSTAGRAM_LOGIN_CONFIG_ID "
                                                 "(with META_APP_ID and META_APP_SECRET), or INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET.")
    if unified_login():
        return start_login(UNIFIED_DIALOG_URL.format(version=settings.meta_graph_api_version), {
            "client_id": settings.meta_app_id, "redirect_uri": callback_url(request, PREFIX),
            "response_type": "code", "config_id": settings.instagram_login_config_id,
            "override_default_response_type": "true",
        }, PREFIX)
    return start_login(DIALOG_URL, {
        "client_id": settings.instagram_app_id, "redirect_uri": callback_url(request, PREFIX),
        "response_type": "code", "scope": ",".join(SCOPES),
    }, PREFIX)


async def fetch_unified_accounts(code: str, redirect_uri: str) -> list[dict]:
    """Exchange the Unified login code for a business token and list the Instagram accounts shared."""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(graph_url("oauth/access_token"), params={
            "client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret,
            "redirect_uri": redirect_uri, "code": code,
        })
        raise_for_graph_error(response, "Instagram")
        token = response.json()["access_token"]
        params = {"fields": "id,username", "limit": 100}
        if proof := appsecret_proof(token):
            params["appsecret_proof"] = proof
        response = await client.get(graph_url("me/assigned_instagram_accounts"), params=params,
                                    headers={"Authorization": f"Bearer {token}"})
        raise_for_graph_error(response, "Instagram")
    return [{"id": str(account["id"]), "token": token, "api": FACEBOOK_API,
             "name": f"@{account['username']}" if account.get("username") else str(account["id"])}
            for account in response.json().get("data", [])]


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
        if unified_login():
            accounts = await fetch_unified_accounts(code, callback_url(request, PREFIX))
        else:
            accounts = [await fetch_account(code, callback_url(request, PREFIX))]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, httpx.HTTPError) and str(exc) else "Instagram did not respond as expected."
        return back_to_connections(PREFIX, error=f"Could not connect Instagram. {detail} "
                                                 "Personal accounts can't be connected; switch to a Creator or Business account first.")
    if not accounts:
        return back_to_connections(PREFIX, error="No Instagram account was shared. Connect Instagram again and "
                                                 "select your Instagram account when asked.")
    await save_accounts(db, int(user["sub"]), "instagram", accounts)
    names = ", ".join(account["name"] for account in accounts)
    noun = "account" if len(accounts) == 1 else "accounts"
    return back_to_connections(PREFIX, success=f"Connected Instagram {noun} {names}.")
