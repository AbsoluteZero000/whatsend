"""Minimal client for Meta's Graph APIs (graph.facebook.com and graph.instagram.com).

Access tokens travel in the Authorization header, never in URLs.
"""
import hashlib
import hmac
import json

import httpx

from app.config import settings

# Meta's code for an expired, revoked or otherwise invalid access token.
INVALID_TOKEN = 190


class GraphError(httpx.HTTPError):
    """A Graph API failure, described without credentials."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def graph_url(path: str = "", *, video: bool = False, base_url: str | None = None) -> str:
    base = (base_url or settings.meta_graph_base_url).rstrip("/")
    if video and base == "https://graph.facebook.com":
        # Meta takes Facebook video uploads on a separate host.
        base = "https://graph-video.facebook.com"
    return f"{base}/{settings.meta_graph_api_version}/{path.lstrip('/')}"


def appsecret_proof(access_token: str, app_secret: str | None = None) -> str | None:
    """Proves the call comes from our app; required when the app enables "Require App Secret"."""
    secret = settings.meta_app_secret if app_secret is None else app_secret
    if not secret:
        return None
    return hmac.new(secret.encode(), access_token.encode(), hashlib.sha256).hexdigest()


def raise_for_graph_error(response: httpx.Response, service: str = "Facebook") -> None:
    if response.is_success:
        return
    try:
        body = response.json()
        error = body.get("error")
        if not isinstance(error, dict):  # Instagram's login endpoints return flat errors.
            error = {"message": body.get("error_message") or error, "code": body.get("code")}
    except (ValueError, AttributeError):
        error = {}
    code = error.get("code")
    message = error.get("error_user_msg") or error.get("message") or f"HTTP {response.status_code}"
    if code == INVALID_TOKEN:
        message = (f"The {service} login expired or was revoked. "
                   f"Connect {service} again on the Connections page. ({message})")
    raise GraphError(f"{service} API error{f' {code}' if code else ''}: {message}", code)


# Instagram logins made through Facebook Login for Business ("Unified login") call graph.facebook.com.
FACEBOOK_API = "facebook"


def dump_credentials(access_token: str, account_id: str, expires_at: int | None = None,
                     api: str | None = None) -> str:
    """Stored encrypted in Token.api_token: the token, the account it posts as, when it expires and,
    for Instagram, which Graph API it belongs to (absent: graph.instagram.com)."""
    data = {"t": access_token, "id": str(account_id)}
    if expires_at:
        data["exp"] = int(expires_at)
    if api:
        data["api"] = api
    return json.dumps(data, separators=(",", ":"))


def load_credentials(raw: str, service: str = "Facebook") -> dict:
    try:
        data = json.loads(raw)
        if data["t"] and data["id"]:
            return data
    except (ValueError, TypeError, KeyError):
        pass
    raise ValueError(f"This connection's {service} login is missing. Connect {service} again.")


def parse_credentials(raw: str, service: str = "Facebook") -> tuple[str, str]:
    data = load_credentials(raw, service)
    return str(data["t"]), str(data["id"])


class GraphClient:
    def __init__(self, access_token: str, timeout: float = 60, *, base_url: str | None = None,
                 app_secret: str | None = None, service: str = "Facebook"):
        self.access_token = access_token
        self.timeout = timeout
        self.base_url = base_url
        self.app_secret = app_secret
        self.service = service

    async def request(self, method: str, path: str, *, video: bool = False, params: dict | None = None,
                      data: dict | None = None, files: dict | None = None, timeout: float | None = None) -> dict:
        params = dict(params or {})
        proof = appsecret_proof(self.access_token, self.app_secret)
        if proof:
            params["appsecret_proof"] = proof
        async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
            response = await client.request(
                method, graph_url(path, video=video, base_url=self.base_url), params=params, data=data,
                files=files, headers={"Authorization": f"Bearer {self.access_token}"},
            )
        raise_for_graph_error(response, self.service)
        try:
            return response.json()
        except ValueError:
            raise GraphError(f"{self.service} API returned an unreadable response") from None
