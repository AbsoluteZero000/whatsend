"""OAuth endpoints for the Claude connector: discovery, client registration, consent, tokens and disconnecting."""
import base64
import re
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.oauth import OAuthGrant
from app.routers.auth import get_current_user, require_user
from app.services import oauth
from app.services.csrf import csrf_protect
from app.services.rate_limit import check_rate_limit


def connector_enabled() -> None:
    if not settings.claude_connector_enabled:
        raise HTTPException(404, "Not Found")


router = APIRouter(tags=["oauth"], dependencies=[Depends(connector_enabled)])

AUTHORIZE_FIELDS = ("response_type", "client_id", "redirect_uri", "state", "code_challenge",
                    "code_challenge_method", "scope", "resource")
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def oauth_error_response(exc: oauth.OAuthError) -> JSONResponse:
    headers = dict(NO_STORE)
    if exc.status_code == 401:
        headers["WWW-Authenticate"] = 'Basic realm="EchoSend"'
    return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=exc.status_code, headers=headers)


@router.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def authorization_server_metadata(request: Request):
    return JSONResponse(oauth.authorization_server_metadata(request))


@router.get("/.well-known/oauth-protected-resource", include_in_schema=False)
@router.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
async def protected_resource_metadata(request: Request):
    return JSONResponse(oauth.protected_resource_metadata(request))


@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request, db: AsyncSession = Depends(get_db)):
    check_rate_limit(request, "oauth-register", "", limit=20, window_seconds=3600)
    try:
        metadata = await request.json()
    except ValueError:
        metadata = None
    try:
        return JSONResponse(await oauth.register_client(db, metadata), status_code=201, headers=NO_STORE)
    except oauth.OAuthError as exc:
        return oauth_error_response(exc)


def redirect_with(redirect_uri: str, request: Request, **params) -> RedirectResponse:
    params = {key: value for key, value in params.items() if value}
    params["iss"] = oauth.public_base_url(request)
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(redirect_uri + separator + urlencode(params), status_code=303)


def consent_page(request: Request, status_code: int = 200, **context):
    response = request.app.state.render(request, "oauth/authorize.html", **context)
    response.status_code = status_code
    redirect_uri = context.get("params", {}).get("redirect_uri")
    if redirect_uri:
        # Allowing the app redirects the form post to it, so the page has to permit that destination.
        response.headers["Content-Security-Policy"] = response.headers["Content-Security-Policy"].replace(
            "form-action 'self'", f"form-action 'self' {oauth.redirect_origin(redirect_uri)}")
    return response


async def check_authorization_request(request: Request, db: AsyncSession, params: dict):
    """Returns (client, params, None) when the request is valid, otherwise (None, None, response)."""
    client = await oauth.get_client(db, params.get("client_id"))
    redirect_uri = oauth.client_redirect_uri(client, params.get("redirect_uri")) if client else None
    if client is None or redirect_uri is None:
        # Never redirect to an address that wasn't registered.
        return None, None, consent_page(request, 400, error="This connection link is not valid. Start connecting again from Claude.")
    params = {**params, "redirect_uri": redirect_uri}
    state = params.get("state")
    if params.get("response_type") != "code":
        return None, None, redirect_with(redirect_uri, request, error="unsupported_response_type", state=state)
    if params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9\-_]{43}", params.get("code_challenge") or ""):
        return None, None, redirect_with(redirect_uri, request, error="invalid_request",
                                         error_description="PKCE with S256 is required", state=state)
    try:
        params["resource"] = oauth.check_resource(request, params.get("resource"))
    except oauth.OAuthError as exc:
        return None, None, redirect_with(redirect_uri, request, error=exc.error, error_description=exc.description, state=state)
    return client, params, None


@router.get("/oauth/authorize", include_in_schema=False)
async def authorize_page(request: Request, db: AsyncSession = Depends(get_db)):
    params = {field: request.query_params.get(field) for field in AUTHORIZE_FIELDS}
    client, params, error = await check_authorization_request(request, db, params)
    if error:
        return error
    payload = get_current_user(request)
    if payload is None:
        next_url = request.url.path + "?" + request.url.query
        return RedirectResponse("/auth/signin?next=" + quote(next_url, safe=""), status_code=303)
    if payload.get("is_admin"):
        return consent_page(request, 403, error="Administrator accounts can't connect apps. Sign in with your own account.")
    require_user(request)  # e.g. sends accounts that must change their password to Settings first
    return consent_page(request, client_name=client.name, redirect_host=oauth.redirect_host(params["redirect_uri"]),
                        params=params, username=payload.get("username", ""))


@router.post("/oauth/authorize", include_in_schema=False, dependencies=[Depends(csrf_protect)])
async def authorize_decision(request: Request, db: AsyncSession = Depends(get_db)):
    payload = get_current_user(request)
    if payload is None:
        return RedirectResponse("/auth/signin", status_code=303)
    if payload.get("is_admin"):
        return consent_page(request, 403, error="Administrator accounts can't connect apps. Sign in with your own account.")
    user = require_user(request)
    form = await request.form()
    client, params, error = await check_authorization_request(request, db, {field: form.get(field) for field in AUTHORIZE_FIELDS})
    if error:
        return error
    if form.get("decision") != "allow":
        return redirect_with(params["redirect_uri"], request, error="access_denied",
                             error_description="The user did not allow access", state=params.get("state"))
    code = await oauth.create_authorization_code(db, int(user["sub"]), client, params["redirect_uri"],
                                                 params["code_challenge"], params["resource"])
    return redirect_with(params["redirect_uri"], request, code=code, state=params.get("state"))


@router.post("/oauth/token", include_in_schema=False)
async def token(request: Request, db: AsyncSession = Depends(get_db)):
    check_rate_limit(request, "oauth-token", "", limit=60, window_seconds=60)
    form = {key: str(value) for key, value in (await request.form()).items()}
    try:
        client = await oauth.get_client(db, form.get("client_id") or basic_client_id(request))
        client = oauth.authenticate_client(client, form, request.headers.get("authorization"))
        grant_type = form.get("grant_type")
        if grant_type == "authorization_code":
            tokens = await oauth.exchange_code(db, client, form)
        elif grant_type == "refresh_token":
            tokens = await oauth.refresh(db, client, form)
        else:
            raise oauth.OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token")
    except oauth.OAuthError as exc:
        return oauth_error_response(exc)
    return JSONResponse(tokens, headers=NO_STORE)


def basic_client_id(request: Request) -> str | None:
    authorization = request.headers.get("authorization") or ""
    if not authorization.lower().startswith("basic "):
        return None
    try:
        return base64.b64decode(authorization[6:]).decode().partition(":")[0]
    except (ValueError, UnicodeDecodeError):
        return None


@router.post("/oauth/revoke", include_in_schema=False)
async def revoke(request: Request, db: AsyncSession = Depends(get_db)):
    form = {key: str(value) for key, value in (await request.form()).items()}
    try:
        client = await oauth.get_client(db, form.get("client_id") or basic_client_id(request))
        client = oauth.authenticate_client(client, form, request.headers.get("authorization"))
    except oauth.OAuthError as exc:
        return oauth_error_response(exc)
    await oauth.revoke_token(db, client, form.get("token", ""))
    return JSONResponse({}, headers=NO_STORE)


@router.post("/oauth/connections/{grant_id}/disconnect", include_in_schema=False, dependencies=[Depends(csrf_protect)])
async def disconnect(grant_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    await db.execute(delete(OAuthGrant).where(OAuthGrant.id == grant_id, OAuthGrant.user_id == int(user["sub"])))
    await db.commit()
    return RedirectResponse("/auth/profile?success=" + quote("App disconnected"), status_code=303)
