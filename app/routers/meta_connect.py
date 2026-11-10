"""Connect Facebook: Facebook Login that adds one connection per Facebook Page the person shares.

Facebook's dialog lets the person choose which Pages to share. Page access tokens made
from a long-lived user token do not expire, so no refresh job is needed. They stop working
if the person removes the app, loses the Page role or changes their password; posting then
fails with a "connect Facebook again" message.
"""
import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.providers._meta.graph import appsecret_proof, graph_url, raise_for_graph_error
from app.routers.auth import require_user
from app.services.social_connections import (
    back_to_connections, callback_url, save_accounts, start_login, state_matches,
)

PREFIX = "/tokens/meta"
router = APIRouter(prefix=PREFIX, tags=["tokens"])

DIALOG_URL = "https://www.facebook.com/{version}/dialog/oauth"
SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts", "business_management")


def meta_connect_enabled() -> bool:
    return bool(settings.meta_app_id and settings.meta_app_secret)


@router.get("/connect")
async def start_meta_connect(request: Request):
    require_user(request)
    if not meta_connect_enabled():
        return back_to_connections(PREFIX, error="Facebook isn't set up on this server yet. Set META_APP_ID and META_APP_SECRET.")
    return start_login(DIALOG_URL.format(version=settings.meta_graph_api_version), {
        "client_id": settings.meta_app_id, "redirect_uri": callback_url(request, PREFIX),
        "response_type": "code", "scope": ",".join(SCOPES),
    }, PREFIX)


async def fetch_pages(code: str, redirect_uri: str) -> list[dict]:
    """Exchange the login code and list the Pages this person shared."""
    app = {"client_id": settings.meta_app_id, "client_secret": settings.meta_app_secret}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(graph_url("oauth/access_token"), params={**app, "redirect_uri": redirect_uri, "code": code})
        raise_for_graph_error(response)
        short_lived = response.json()["access_token"]
        response = await client.get(graph_url("oauth/access_token"), params={
            **app, "grant_type": "fb_exchange_token", "fb_exchange_token": short_lived,
        })
        raise_for_graph_error(response)
        user_token = response.json()["access_token"]
        params = {"fields": "id,name,access_token", "limit": 100}
        if proof := appsecret_proof(user_token):
            params["appsecret_proof"] = proof
        response = await client.get(graph_url("me/accounts"), params=params,
                                    headers={"Authorization": f"Bearer {user_token}"})
        raise_for_graph_error(response)
    return [{"id": str(page["id"]), "name": page.get("name") or str(page["id"]), "token": page["access_token"]}
            for page in response.json().get("data", []) if page.get("access_token")]


@router.get("/callback")
async def finish_meta_connect(request: Request, code: str = "", state: str = "", error: str = "",
                              db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    if not state_matches(request, state):
        return back_to_connections(PREFIX, error="The Facebook sign-in expired. Try connecting again.")
    if error or not code:
        return back_to_connections(PREFIX, error="Facebook connection was cancelled.")
    try:
        pages = await fetch_pages(code, callback_url(request, PREFIX))
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, httpx.HTTPError) and str(exc) else "Facebook did not respond as expected."
        return back_to_connections(PREFIX, error=f"Could not connect Facebook. {detail}")
    if not pages:
        return back_to_connections(PREFIX, error="No Facebook Pages were shared. Connect again and choose at least one Page you manage.")
    await save_accounts(db, int(user["sub"]), "facebook", pages)
    return back_to_connections(PREFIX, success=f"Connected {len(pages)} Facebook Page{'s' if len(pages) != 1 else ''}.")
