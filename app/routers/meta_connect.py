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
from app.providers._meta.graph import GraphError, appsecret_proof, graph_url, raise_for_graph_error
from app.routers.auth import require_user
from app.services.social_connections import (
    back_to_connections, callback_url, save_accounts, start_login, state_matches,
)

PREFIX = "/tokens/meta"
router = APIRouter(prefix=PREFIX, tags=["tokens"])

DIALOG_URL = "https://www.facebook.com/{version}/dialog/oauth"
SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts", "business_management")
PAGE_SCOPES = {"pages_show_list", "pages_manage_posts", "pages_read_engagement"}
PAGE_FIELDS = "id,name,access_token"
MAX_PAGE_LISTS = 10


def meta_connect_enabled() -> bool:
    return bool(settings.meta_app_id and settings.meta_app_secret)


@router.get("/connect")
async def start_meta_connect(request: Request):
    require_user(request)
    if not meta_connect_enabled():
        return back_to_connections(PREFIX, error="Facebook isn't set up on this server yet. Set META_APP_ID and META_APP_SECRET.")
    params = {"client_id": settings.meta_app_id, "redirect_uri": callback_url(request, PREFIX), "response_type": "code"}
    if settings.meta_login_config_id:
        # Facebook Login for Business: the configuration in the Meta app lists the permissions.
        params["config_id"] = settings.meta_login_config_id
    else:
        params["scope"] = ",".join(SCOPES)
    return start_login(DIALOG_URL.format(version=settings.meta_graph_api_version), params, PREFIX)


async def fetch_pages(code: str, redirect_uri: str) -> list[dict]:
    """Exchange the login code and list the Pages this person shared and can post to."""
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
        user = GraphRequests(client, user_token)

        pages = {}
        params = {"fields": PAGE_FIELDS, "limit": 100}
        for _ in range(MAX_PAGE_LISTS):
            body = await user.get("me/accounts", params)
            for page in body.get("data", []):
                pages[str(page["id"])] = page
            after = (body.get("paging") or {}).get("cursors", {}).get("after")
            if not after or not (body.get("paging") or {}).get("next"):
                break
            params = {**params, "after": after}

        # Facebook Login for Business can leave Pages out of me/accounts (for example Pages owned by a
        # business portfolio). The login itself records every Page the person chose, so read those too.
        for page_id in await granted_page_ids(client, user_token):
            if page_id not in pages:
                try:
                    pages[page_id] = await user.get(page_id, {"fields": PAGE_FIELDS})
                except GraphError:
                    continue
    return [{"id": page_id, "name": page.get("name") or page_id, "token": page["access_token"]}
            for page_id, page in pages.items() if page.get("access_token")]


async def granted_page_ids(client: httpx.AsyncClient, user_token: str) -> list[str]:
    """Page IDs this login granted, from the token's granular scopes."""
    response = await client.get(graph_url("debug_token"), params={"input_token": user_token},
                                headers={"Authorization": f"Bearer {settings.meta_app_id}|{settings.meta_app_secret}"})
    try:
        raise_for_graph_error(response)
        scopes = response.json().get("data", {}).get("granular_scopes") or []
    except (GraphError, ValueError, AttributeError):
        return []
    ids = []
    for scope in scopes:
        if scope.get("scope") in PAGE_SCOPES:
            ids += [str(target) for target in scope.get("target_ids") or [] if str(target) not in ids]
    return ids


class GraphRequests:
    """GET requests made with one user token, sent in the header with its appsecret_proof."""

    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client = client
        self.token = token
        self.proof = appsecret_proof(token)

    async def get(self, path: str, params: dict) -> dict:
        params = {**params, **({"appsecret_proof": self.proof} if self.proof else {})}
        response = await self.client.get(graph_url(path), params=params,
                                         headers={"Authorization": f"Bearer {self.token}"})
        raise_for_graph_error(response)
        return response.json()


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
        return back_to_connections(PREFIX, error="No Facebook Pages you can post to were shared. Connect again and choose your Pages. "
                                                 "Facebook only lets apps post to Pages where you have a role that can create content "
                                                 "(such as admin or editor), not Pages you only follow or like.")
    await save_accounts(db, int(user["sub"]), "facebook", pages)
    return back_to_connections(PREFIX, success=f"Connected {len(pages)} Facebook Page{'s' if len(pages) != 1 else ''}.")
