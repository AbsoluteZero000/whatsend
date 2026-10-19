"""OAuth 2.1 authorization server used by the Claude connector (authorization code + PKCE, refresh tokens).

Codes and tokens are random strings; only their SHA-256 hashes are stored (see models/oauth.py).
"""
import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.oauth import OAuthClient, OAuthGrant
from app.models.user import User

SCOPE = "messages"
CODE_LIFETIME = timedelta(minutes=10)
ACCESS_LIFETIME = timedelta(hours=1)
REFRESH_LIFETIME = timedelta(days=90)
MAX_REDIRECT_URIS = 10
AUTH_METHODS = {"none", "client_secret_post", "client_secret_basic"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class OAuthError(Exception):
    """An error returned to the client in the standard OAuth JSON shape."""

    def __init__(self, error: str, description: str, status_code: int = 400):
        super().__init__(description)
        self.error = error
        self.description = description
        self.status_code = status_code


def utcnow() -> datetime:
    # Stored naive, like every other timestamp in the SQLite database.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def public_base_url(request: Request) -> str:
    if settings.public_url:
        return settings.public_url.rstrip("/")
    # Fly terminates TLS in front of the app, so the request itself arrives over http.
    scheme = "https" if settings.cookie_secure else request.url.scheme
    return f"{scheme}://{request.url.netloc}"


def mcp_resource_url(request: Request) -> str:
    return public_base_url(request) + "/mcp"


def valid_redirect_uri(uri: str) -> bool:
    """HTTPS callbacks, or http on this computer for desktop apps. No fragments, credentials or odd characters."""
    if not isinstance(uri, str) or len(uri) > 2048 or re.search(r"[\s\"'<>;,\\]", uri):
        return False
    try:
        parts = urlsplit(uri)
        hostname = parts.hostname
    except ValueError:
        return False
    if parts.fragment or parts.username or parts.password or not hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and hostname in LOOPBACK_HOSTS


def redirect_origin(uri: str) -> str:
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"


def redirect_host(uri: str) -> str:
    return urlsplit(uri).hostname or uri


async def register_client(db: AsyncSession, metadata: dict) -> dict:
    """RFC 7591 dynamic client registration."""
    if not isinstance(metadata, dict):
        raise OAuthError("invalid_client_metadata", "Send the client metadata as a JSON object")
    redirect_uris = metadata.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= MAX_REDIRECT_URIS:
        raise OAuthError("invalid_redirect_uri", f"Register between 1 and {MAX_REDIRECT_URIS} redirect URIs")
    if not all(valid_redirect_uri(uri) for uri in redirect_uris):
        raise OAuthError("invalid_redirect_uri", "Redirect URIs must use https, or http on localhost")
    auth_method = metadata.get("token_endpoint_auth_method") or "none"
    if auth_method not in AUTH_METHODS:
        raise OAuthError("invalid_client_metadata", "Unsupported token_endpoint_auth_method")
    for grant_type in metadata.get("grant_types") or ["authorization_code"]:
        if grant_type not in {"authorization_code", "refresh_token"}:
            raise OAuthError("invalid_client_metadata", f"Unsupported grant type: {grant_type}")
    name = str(metadata.get("client_name") or "").strip()[:100] or "Unnamed app"

    client = OAuthClient(id=secrets.token_urlsafe(24), name=name, redirect_uris=json.dumps(redirect_uris),
                         auth_method=auth_method)
    secret = None
    if auth_method != "none":
        secret = secrets.token_urlsafe(32)
        client.secret_hash = digest(secret)
    db.add(client)
    await db.commit()
    response = {
        "client_id": client.id,
        "client_id_issued_at": int(datetime.now(timezone.utc).timestamp()),
        "client_name": name,
        "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": auth_method,
        "scope": SCOPE,
    }
    if secret:
        response["client_secret"] = secret
        response["client_secret_expires_at"] = 0
    return response


async def get_client(db: AsyncSession, client_id: str | None) -> OAuthClient | None:
    if not client_id or len(client_id) > 64:
        return None
    return await db.get(OAuthClient, client_id)


def client_redirect_uri(client: OAuthClient, requested: str | None) -> str | None:
    """The registered redirect URI to use, or None when the request doesn't match one exactly."""
    registered = json.loads(client.redirect_uris)
    if not requested:
        return registered[0] if len(registered) == 1 else None
    return requested if requested in registered else None


def check_resource(request: Request, resource: str | None) -> str:
    """RFC 8707: tokens are only for this server's MCP endpoint."""
    expected = mcp_resource_url(request)
    if resource and resource.rstrip("/") not in {expected, public_base_url(request)}:
        raise OAuthError("invalid_target", "This server only issues tokens for " + expected)
    return expected


async def create_authorization_code(db: AsyncSession, user_id: int, client: OAuthClient, redirect_uri: str,
                                    code_challenge: str, resource: str) -> str:
    now = utcnow()
    # Approving an app again replaces its earlier connection; abandoned codes are cleared too.
    await db.execute(delete(OAuthGrant).where(OAuthGrant.user_id == user_id, OAuthGrant.client_id == client.id))
    await db.execute(delete(OAuthGrant).where(OAuthGrant.access_hash.is_(None), OAuthGrant.code_expires_at < now))
    code = secrets.token_urlsafe(32)
    db.add(OAuthGrant(user_id=user_id, client_id=client.id, client_name=client.name, scope=SCOPE, resource=resource,
                      redirect_uri=redirect_uri, code_hash=digest(code), code_challenge=code_challenge,
                      code_expires_at=now + CODE_LIFETIME))
    await db.commit()
    return code


def pkce_matches(verifier: str, challenge: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", verifier or ""):
        return False
    computed = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    return secrets.compare_digest(computed, challenge)


def authenticate_client(client: OAuthClient | None, form: dict, authorization: str | None) -> OAuthClient:
    if client is None:
        raise OAuthError("invalid_client", "Unknown client", 401)
    if client.auth_method == "none":
        return client
    secret = form.get("client_secret")
    if client.auth_method == "client_secret_basic" and authorization and authorization.lower().startswith("basic "):
        try:
            client_id, _, secret = base64.b64decode(authorization[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            secret = None
        else:
            if client_id != client.id:
                secret = None
    if not secret or not client.secret_hash or not secrets.compare_digest(digest(secret), client.secret_hash):
        raise OAuthError("invalid_client", "Client authentication failed", 401)
    return client


async def issue_tokens(db: AsyncSession, grant: OAuthGrant) -> dict:
    now = utcnow()
    access_token = "es_at_" + secrets.token_urlsafe(32)
    refresh_token = "es_rt_" + secrets.token_urlsafe(32)
    grant.access_hash = digest(access_token)
    grant.access_expires_at = now + ACCESS_LIFETIME
    grant.refresh_hash = digest(refresh_token)
    grant.refresh_expires_at = now + REFRESH_LIFETIME
    grant.last_used_at = now
    await db.commit()
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_LIFETIME.total_seconds()),
        "refresh_token": refresh_token,
        "scope": grant.scope,
    }


async def exchange_code(db: AsyncSession, client: OAuthClient, form: dict) -> dict:
    code = form.get("code") or ""
    grant = (await db.execute(select(OAuthGrant).where(OAuthGrant.code_hash == digest(code)))).scalar_one_or_none() if code else None
    if grant is None or grant.client_id != client.id or grant.code_expires_at is None or grant.code_expires_at < utcnow():
        raise OAuthError("invalid_grant", "The authorization code is invalid or has expired")
    if form.get("redirect_uri") and form["redirect_uri"] != grant.redirect_uri:
        raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
    if not pkce_matches(form.get("code_verifier") or "", grant.code_challenge or ""):
        raise OAuthError("invalid_grant", "The PKCE code_verifier is missing or wrong")
    # A code works once.
    grant.code_hash = None
    grant.code_challenge = None
    grant.code_expires_at = None
    return await issue_tokens(db, grant)


async def refresh(db: AsyncSession, client: OAuthClient, form: dict) -> dict:
    token = form.get("refresh_token") or ""
    grant = (await db.execute(select(OAuthGrant).where(OAuthGrant.refresh_hash == digest(token)))).scalar_one_or_none() if token else None
    if grant is None or grant.client_id != client.id or grant.refresh_expires_at is None or grant.refresh_expires_at < utcnow():
        raise OAuthError("invalid_grant", "The refresh token is invalid or has expired")
    user = await db.get(User, grant.user_id)
    if user is None or not user.is_active or user.is_admin:
        raise OAuthError("invalid_grant", "This account can no longer be used")
    # Rotation: the old refresh and access tokens stop working.
    return await issue_tokens(db, grant)


async def revoke_token(db: AsyncSession, client: OAuthClient, token: str) -> None:
    """RFC 7009: revoking either token disconnects the app."""
    hashed = digest(token or "")
    grant = (await db.execute(select(OAuthGrant).where(
        (OAuthGrant.access_hash == hashed) | (OAuthGrant.refresh_hash == hashed)
    ))).scalar_one_or_none()
    if grant is not None and grant.client_id == client.id:
        await db.delete(grant)
        await db.commit()


async def user_for_access_token(db: AsyncSession, token: str) -> tuple[User, OAuthGrant] | None:
    if not token.startswith("es_at_"):
        return None
    grant = (await db.execute(select(OAuthGrant).where(OAuthGrant.access_hash == digest(token)))).scalar_one_or_none()
    now = utcnow()
    if grant is None or grant.access_expires_at is None or grant.access_expires_at < now:
        return None
    user = await db.get(User, grant.user_id)
    if user is None or not user.is_active or user.is_admin:
        return None
    if grant.last_used_at is None or now - grant.last_used_at > timedelta(minutes=1):
        grant.last_used_at = now
        await db.commit()
    return user, grant


def authorization_server_metadata(request: Request) -> dict:
    base = public_base_url(request)
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "revocation_endpoint": base + "/oauth/revoke",
        "scopes_supported": [SCOPE],
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": sorted(AUTH_METHODS),
        "revocation_endpoint_auth_methods_supported": sorted(AUTH_METHODS),
        "code_challenge_methods_supported": ["S256"],
        "authorization_response_iss_parameter_supported": True,
    }


def protected_resource_metadata(request: Request) -> dict:
    return {
        "resource": mcp_resource_url(request),
        "authorization_servers": [public_base_url(request)],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "EchoSend",
    }
