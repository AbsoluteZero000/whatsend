import asyncio
import base64
import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.config import settings
from app.database import Base, get_db
from app.models.job import Job, JobGroup
from app.models.oauth import OAuthGrant
from app.models.token import Token
from app.models.user import User
from app.routers.auth import safe_next
from app.services.auth import hash_password
from app.services.crypto import encrypt_token

PASSWORD = "correct-horse-1"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
SESSION = "8f14e45f-ceea-467f-a8f4-6f3f9d1b2c3d"
CSRF = {"X-CSRF-Token": "csrf"}
GROUPS = [
    {"id": "120363111@g.us", "name": "Family"},
    {"id": "120363222@g.us", "name": "Work"},
    {"id": "120363333@g.us", "name": "Twins"},
    {"id": "120363444@g.us", "name": "Twins"},
]
CLIENT = httpx.AsyncClient


def worker(request):
    if request.url.path.endswith("/groups"):
        return httpx.Response(200, json={"data": [{"external_chat_id": group["id"], "name": group["name"]} for group in GROUPS],
                                         "next_cursor": ""})
    return httpx.Response(404)


def run(tmp_path, monkeypatch, scenario, enabled=True):
    async def go():
        monkeypatch.setattr(settings, "claude_connector_enabled", enabled)
        monkeypatch.setattr(settings, "whatsapp_worker_url", "https://native.test")
        monkeypatch.setattr(settings, "whatsapp_worker_secret", "relay-secret")
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: CLIENT(transport=httpx.MockTransport(worker), **kwargs))
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'connector.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(User(id=7, username="member", password_hash=hash_password(PASSWORD), timezone="Africa/Cairo", onboarded=True))
            db.add(User(id=8, username="other", password_hash=hash_password(PASSWORD), timezone="UTC", onboarded=True))
            db.add(User(id=9, username="boss", password_hash=hash_password(PASSWORD), timezone="UTC", onboarded=True, is_admin=True))
            await db.flush()
            db.add(Token(id=3, user_id=7, name="My phone", provider="native", relay_session_id=SESSION,
                         api_token=encrypt_token("relay-secret"), is_active=True))
            db.add(Token(id=4, user_id=8, name="Other phone", provider="native", relay_session_id=SESSION,
                         api_token=encrypt_token("relay-secret"), is_active=True))
            await db.flush()
            db.add(Job(id=50, user_id=8, token_id=4, group_id=GROUPS[0]["id"], message="Not yours",
                       trigger_type="cron", trigger_value="0 9 * * *", status="paused"))
            await db.commit()

        async def database():
            async with sessions() as db:
                yield db

        monkeypatch.setattr(main, "async_session", sessions)
        main.app.dependency_overrides[get_db] = database
        try:
            async with CLIENT(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                await scenario(Browser(client), sessions)
        finally:
            main.app.dependency_overrides.pop(get_db, None)
            main.apscheduler.remove_all_jobs()
            await engine.dispose()

    asyncio.run(go())


class Browser:
    """Requests with exactly the given session cookie and no cookie jar, plus plain API calls."""

    def __init__(self, client):
        self.client = client

    async def page(self, method, path, session=None, **kwargs):
        cookie = "csrf_token=csrf" + (f"; session={session}" if session else "")
        response = await self.client.request(method, path, headers={**CSRF, "Cookie": cookie}, **kwargs)
        self.client.cookies.clear()
        return response

    async def api(self, method, path, **kwargs):
        response = await self.client.request(method, path, **kwargs)
        self.client.cookies.clear()
        return response


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def register(browser, redirect_uris=(CALLBACK,), **extra):
    return await browser.api("POST", "/oauth/register", json={
        "client_name": "Claude", "redirect_uris": list(redirect_uris),
        "grant_types": ["authorization_code", "refresh_token"], "token_endpoint_auth_method": "none", **extra,
    })


def session_from(response):
    cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith("session="))
    return cookie.split(";", 1)[0].split("=", 1)[1]


async def sign_in(browser, username="member", next_url=None):
    data = {"username": username, "password": PASSWORD, **({"next": next_url} if next_url else {})}
    response = await browser.page("POST", "/auth/signin", data=data)
    assert response.status_code == 303
    return response


def authorize_params(client_id, challenge, **overrides):
    return {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "xyz",
            "code_challenge": challenge, "code_challenge_method": "S256", "scope": "messages",
            "resource": "http://test/mcp", **overrides}


async def connect(browser) -> dict:
    """Everything Claude and the user do to connect: register, sign in, allow, exchange the code."""
    client_id = (await register(browser)).json()["client_id"]
    verifier, challenge = pkce()
    params = authorize_params(client_id, challenge)

    start = await browser.page("GET", "/oauth/authorize", params=params)
    assert start.status_code == 303 and start.headers["location"].startswith("/auth/signin?next=%2Foauth%2Fauthorize%3F")
    next_url = parse_qs(urlsplit(start.headers["location"]).query)["next"][0]
    signin_page = await browser.page("GET", start.headers["location"])
    assert "Sign in to finish connecting Claude" in signin_page.text and f'value="{next_url}"'.replace("&", "&amp;") in signin_page.text
    signed_in = await sign_in(browser, next_url=next_url)
    assert signed_in.headers["location"] == next_url
    session = session_from(signed_in)

    consent = await browser.page("GET", next_url, session)
    assert consent.status_code == 200
    assert "Claude" in consent.text and "wants to use your EchoSend account" in consent.text and "claude.ai" in consent.text
    assert "form-action 'self' https://claude.ai" in consent.headers["content-security-policy"]

    allowed = await browser.page("POST", "/oauth/authorize", session, data={**params, "decision": "allow"})
    assert allowed.status_code == 303
    location = allowed.headers["location"]
    assert location.startswith(CALLBACK + "?")
    query = parse_qs(urlsplit(location).query)
    assert query["state"] == ["xyz"] and query["iss"] == ["http://test"]

    tokens = await browser.api("POST", "/oauth/token", data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": CALLBACK,
        "client_id": client_id, "code_verifier": verifier})
    assert tokens.status_code == 200, tokens.text
    assert tokens.headers["cache-control"] == "no-store"
    body = tokens.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600
    return {**body, "client_id": client_id, "session": session}


async def rpc(browser, access_token, method, params=None, request_id=1):
    message = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
    if request_id is not None:
        message["id"] = request_id
    return await browser.api("POST", "/mcp", json=message, headers={"Authorization": f"Bearer {access_token}"})


async def tool(browser, access_token, name, **arguments):
    response = await rpc(browser, access_token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()["result"]


def future_day(days):
    return (datetime.now(ZoneInfo("Africa/Cairo")) + timedelta(days=days)).strftime("%Y-%m-%d")


def test_claude_discovers_how_to_sign_in(tmp_path, monkeypatch):
    async def scenario(browser, sessions):
        unauthenticated = await browser.api("POST", "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert unauthenticated.status_code == 401
        assert 'resource_metadata="http://test/.well-known/oauth-protected-resource/mcp"' in unauthenticated.headers["www-authenticate"]
        bad_token = await browser.api("POST", "/mcp", json={}, headers={"Authorization": "Bearer es_at_nope"})
        assert bad_token.status_code == 401 and 'error="invalid_token"' in bad_token.headers["www-authenticate"]

        resource = (await browser.api("GET", "/.well-known/oauth-protected-resource/mcp")).json()
        assert resource["resource"] == "http://test/mcp" and resource["authorization_servers"] == ["http://test"]
        server = (await browser.api("GET", "/.well-known/oauth-authorization-server")).json()
        assert server["issuer"] == "http://test"
        assert server["registration_endpoint"] == "http://test/oauth/register"
        assert server["code_challenge_methods_supported"] == ["S256"]

        preflight = await browser.api("OPTIONS", "/mcp", headers={"Origin": "https://claude.ai", "Access-Control-Request-Method": "POST"})
        assert preflight.status_code == 204 and preflight.headers["access-control-allow-origin"] == "*"
        assert (await browser.api("GET", "/mcp")).status_code == 405

    run(tmp_path, monkeypatch, scenario)


def test_connect_claude_and_manage_messages(tmp_path, monkeypatch):
    async def scenario(browser, sessions):
        token = (await connect(browser))["access_token"]

        initialized = (await rpc(browser, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                "clientInfo": {"name": "claude-ai", "version": "1"}})).json()
        assert initialized["result"]["protocolVersion"] == "2025-06-18"
        assert initialized["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert (await rpc(browser, token, "notifications/initialized", request_id=None)).status_code == 202
        listed = (await rpc(browser, token, "tools/list")).json()["result"]["tools"]
        assert {item["name"] for item in listed} == {
            "get_account_overview", "list_recipients", "list_messages", "get_message",
            "create_message", "pause_message", "resume_message", "cancel_message"}
        create_schema = next(item for item in listed if item["name"] == "create_message")
        assert create_schema["inputSchema"]["required"] == ["message", "when"]
        assert create_schema["annotations"]["openWorldHint"] is True

        overview = (await tool(browser, token, "get_account_overview"))["structuredContent"]
        assert overview["username"] == "member" and overview["timezone"] == "Africa/Cairo"
        assert overview["whatsapp_connections"] == [{"id": 3, "name": "My phone", "type": "WhatsApp (QR code)", "ready": True}]

        recipients = (await tool(browser, token, "list_recipients", search="fam"))["structuredContent"]
        assert recipients["groups"] == [GROUPS[0]], recipients

        days = [future_day(3), future_day(10)]
        created = await tool(browser, token, "create_message", message="Happy Eid! 🌙", recipients=["Family", "+20 100 123 4567"],
                             when="specific_dates", dates=days, time="09:00", label="Eid")
        assert created["isError"] is False, created
        result = created["structuredContent"]
        assert result["created"] and result["status"] == "active" and result["recipients"] == ["Family", "+201001234567"]
        assert result["next_send"] == f"{days[0]} 09:00" and result["link"] == f"http://test/jobs/{result['id']}"
        async with sessions() as db:
            job = await db.get(Job, result["id"])
            assert (job.user_id, job.token_id, job.trigger_type, job.schedule_timezone) == (7, 3, "dates", "Africa/Cairo")
            groups = (await db.execute(select(JobGroup.group_id, JobGroup.group_name).where(JobGroup.job_id == job.id))).all()
            assert sorted(groups) == [("+201001234567", "+201001234567"), ("120363111@g.us", "Family")]

        weekly = (await tool(browser, token, "create_message", message="Standup", recipients=["120363222@g.us"],
                             when="repeating", repeat="weekly", weekdays=["sunday", "wednesday"], time="08:30"))["structuredContent"]
        async with sessions() as db:
            assert (await db.get(Job, weekly["id"])).trigger_value == "30 08 * * 2,6"  # APScheduler: 0 is Monday
        assert weekly["recipients"] == ["Work"]

        ambiguous = await tool(browser, token, "create_message", message="Hi", recipients=["Twins"], when="now")
        assert ambiguous["isError"] and "More than one group is called" in ambiguous["content"][0]["text"]
        missing = await tool(browser, token, "create_message", message="Hi", recipients=["Nobody"], when="now")
        assert missing["isError"] and "No WhatsApp group is called" in missing["content"][0]["text"]
        past = await tool(browser, token, "create_message", message="Hi", recipients=["Family"], when="once", send_at="2020-01-01 10:00")
        assert past["isError"] and "already passed in Africa/Cairo" in past["content"][0]["text"]
        typo = await tool(browser, token, "create_message", message="Hi", recipients=["Family"], when="now", sendAt="x")
        assert typo["isError"] and "Invalid arguments" in typo["content"][0]["text"] and "sendAt" in typo["content"][0]["text"]

        upcoming = (await tool(browser, token, "list_messages"))["structuredContent"]["messages"]
        assert [item["id"] for item in upcoming] == [weekly["id"], result["id"]]

        message_id = result["id"]
        assert (await tool(browser, token, "pause_message", message_id=message_id))["structuredContent"]["status"] == "paused"
        assert (await tool(browser, token, "resume_message", message_id=message_id))["structuredContent"]["status"] == "active"
        assert (await tool(browser, token, "cancel_message", message_id=message_id))["structuredContent"]["status"] == "cancelled"
        again = await tool(browser, token, "cancel_message", message_id=message_id)
        assert again["isError"] and "nothing to cancel" in again["content"][0]["text"]

        details = (await tool(browser, token, "get_message", message_id=message_id))["structuredContent"]
        assert details["message"] == "Happy Eid! 🌙" and [d["at"] for d in details["dates"]] == [f"{day} 09:00" for day in days]

        other_users_message = await tool(browser, token, "get_message", message_id=50)
        assert other_users_message["isError"] and "no message 50" in other_users_message["content"][0]["text"]
        assert (await tool(browser, token, "resume_message", message_id=50))["isError"]
        async with sessions() as db:
            assert (await db.get(Job, 50)).status == "paused"

    run(tmp_path, monkeypatch, scenario)


def test_refresh_rotates_tokens_and_disconnect_ends_access(tmp_path, monkeypatch):
    async def scenario(browser, sessions):
        first = await connect(browser)
        refreshed = await browser.api("POST", "/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": first["client_id"]})
        assert refreshed.status_code == 200
        second = refreshed.json()
        assert second["access_token"] != first["access_token"]

        reused = await browser.api("POST", "/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": first["refresh_token"], "client_id": first["client_id"]})
        assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"
        assert (await rpc(browser, first["access_token"], "ping")).status_code == 401
        assert (await rpc(browser, second["access_token"], "ping")).json()["result"] == {}

        profile = await browser.page("GET", "/auth/profile", first["session"])
        assert "Connected apps" in profile.text and "http://test/mcp" in profile.text and "Claude" in profile.text
        async with sessions() as db:
            grant = (await db.execute(select(OAuthGrant))).scalar_one()
            assert grant.user_id == 7 and grant.access_hash and second["access_token"] not in grant.access_hash

        # Another user can't disconnect it.
        other = session_from(await sign_in(browser, "other"))
        await browser.page("POST", f"/oauth/connections/{grant.id}/disconnect", other)
        assert (await rpc(browser, second["access_token"], "ping")).status_code == 200

        disconnected = await browser.page("POST", f"/oauth/connections/{grant.id}/disconnect", first["session"])
        assert disconnected.status_code == 303 and disconnected.headers["location"].startswith("/auth/profile?success=")
        assert (await rpc(browser, second["access_token"], "ping")).status_code == 401
        after = await browser.api("POST", "/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": second["refresh_token"], "client_id": first["client_id"]})
        assert after.json()["error"] == "invalid_grant"

    run(tmp_path, monkeypatch, scenario)


def test_connection_requests_that_must_be_refused(tmp_path, monkeypatch):
    async def scenario(browser, sessions):
        assert (await register(browser, ["http://evil.example/callback"])).status_code == 400
        assert (await register(browser, ["javascript:alert(1)"])).status_code == 400
        assert (await register(browser, ["http://localhost:6274/oauth/callback"])).status_code == 201
        client_id = (await register(browser)).json()["client_id"]
        session = session_from(await sign_in(browser))
        verifier, challenge = pkce()

        # An address that wasn't registered is never redirected to.
        wrong_redirect = await browser.page("GET", "/oauth/authorize", session,
                                            params=authorize_params(client_id, challenge, redirect_uri="https://evil.example/cb"))
        assert wrong_redirect.status_code == 400 and "location" not in wrong_redirect.headers
        no_pkce = await browser.page("GET", "/oauth/authorize", session, params=authorize_params(client_id, "", code_challenge_method=""))
        assert no_pkce.status_code == 303 and "error=invalid_request" in no_pkce.headers["location"]
        other_resource = await browser.page("GET", "/oauth/authorize", session,
                                            params=authorize_params(client_id, challenge, resource="https://other.example/mcp"))
        assert "error=invalid_target" in other_resource.headers["location"]

        denied = await browser.page("POST", "/oauth/authorize", session, data={**authorize_params(client_id, challenge), "decision": "deny"})
        assert denied.headers["location"].startswith(CALLBACK + "?error=access_denied")
        no_csrf = await browser.client.post("/oauth/authorize", data={**authorize_params(client_id, challenge), "decision": "allow"},
                                            headers={"Cookie": f"session={session}"})
        assert no_csrf.status_code == 403

        allowed = await browser.page("POST", "/oauth/authorize", session, data={**authorize_params(client_id, challenge), "decision": "allow"})
        code = parse_qs(urlsplit(allowed.headers["location"]).query)["code"][0]
        exchange = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "client_id": client_id}
        wrong_verifier = await browser.api("POST", "/oauth/token", data={**exchange, "code_verifier": pkce()[0]})
        assert wrong_verifier.status_code == 400 and wrong_verifier.json()["error"] == "invalid_grant"
        assert (await browser.api("POST", "/oauth/token", data={**exchange, "code_verifier": verifier})).status_code == 200
        replay = await browser.api("POST", "/oauth/token", data={**exchange, "code_verifier": verifier})
        assert replay.json()["error"] == "invalid_grant"

        admin = session_from(await sign_in(browser, "boss"))
        admin_consent = await browser.page("GET", "/oauth/authorize", admin, params=authorize_params(client_id, challenge))
        assert admin_consent.status_code in {303, 403}  # admins stay on the admin pages
        assert "code=" not in admin_consent.headers.get("location", "")

        assert safe_next("//evil.example") is None and safe_next("https://evil.example") is None
        assert safe_next("/\\evil.example") is None and safe_next("/oauth/authorize?x=1") == "/oauth/authorize?x=1"

    run(tmp_path, monkeypatch, scenario)


def test_deactivated_account_loses_claude_access(tmp_path, monkeypatch):
    async def scenario(browser, sessions):
        tokens = await connect(browser)
        async with sessions() as db:
            (await db.get(User, 7)).is_active = False
            await db.commit()
        assert (await rpc(browser, tokens["access_token"], "ping")).status_code == 401
        refreshed = await browser.api("POST", "/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": tokens["client_id"]})
        assert refreshed.json()["error"] == "invalid_grant"

    run(tmp_path, monkeypatch, scenario)


def test_connector_is_off_unless_enabled(tmp_path, monkeypatch):
    async def scenario(browser, sessions):
        assert (await browser.api("GET", "/.well-known/oauth-authorization-server")).status_code == 404
        assert (await browser.api("POST", "/oauth/register", json={})).status_code == 404
        assert (await browser.api("POST", "/mcp", json={})).status_code == 404
        session = session_from(await sign_in(browser))
        assert "Connected apps" not in (await browser.page("GET", "/auth/profile", session)).text

    run(tmp_path, monkeypatch, scenario, enabled=False)
