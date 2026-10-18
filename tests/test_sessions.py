import asyncio
import time

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main as main
from app.database import Base, get_db
from app.models.user import User
from app.services.auth import create_jwt, decode_jwt, hash_password

PASSWORD = "correct-horse-1"
CSRF = {"X-CSRF-Token": "csrf"}
DAY = 86400


def session_cookie(response):
    """The raw Set-Cookie header for the session cookie, or None."""
    return next((value for value in response.headers.get_list("set-cookie") if value.startswith("session=")), None)


def session_token(response):
    return session_cookie(response).split(";", 1)[0].split("=", 1)[1]


def run(tmp_path, monkeypatch, scenario):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(User(id=7, username="member", password_hash=hash_password(PASSWORD), timezone="UTC", onboarded=True))
            await db.commit()

        async def database():
            async with sessions() as db:
                yield db

        monkeypatch.setattr(main, "async_session", sessions)
        main.app.dependency_overrides[get_db] = database
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                await scenario(Client(client), sessions)
        finally:
            main.app.dependency_overrides.pop(get_db, None)
            await engine.dispose()

    asyncio.run(go())


class Client:
    """Sends each request with exactly the given session, like a separate device, and keeps no cookie jar."""

    def __init__(self, client):
        self.client = client

    async def request(self, method, path, token=None, **kwargs):
        cookie = "csrf_token=csrf" + (f"; session={token}" if token else "")
        response = await self.client.request(method, path, headers={**CSRF, "Cookie": cookie}, **kwargs)
        self.client.cookies.clear()
        return response


async def sign_in(client, remember):
    data = {"username": "member", "password": PASSWORD, **({"remember": "1"} if remember else {})}
    response = await client.request("POST", "/auth/signin", data=data)
    assert response.status_code == 303
    return response


def visit(client, token, path="/auth/profile"):
    return client.request("GET", path, token)


def test_unticked_sign_in_lasts_for_the_browser_session(tmp_path, monkeypatch):
    async def scenario(client, sessions):
        response = await sign_in(client, remember=False)
        assert "max-age" not in session_cookie(response).lower()  # cleared when the browser closes
        payload = decode_jwt(session_token(response))
        assert payload["remember"] is False
        assert abs(payload["exp"] - time.time() - DAY) < 60

    run(tmp_path, monkeypatch, scenario)


def test_keep_me_signed_in_lasts_thirty_days(tmp_path, monkeypatch):
    async def scenario(client, sessions):
        response = await sign_in(client, remember=True)
        assert "Max-Age=2592000" in session_cookie(response)
        payload = decode_jwt(session_token(response))
        assert payload["remember"] is True
        assert abs(payload["exp"] - time.time() - 30 * DAY) < 60

    run(tmp_path, monkeypatch, scenario)


def test_changing_language_or_timezone_keeps_the_thirty_day_session(tmp_path, monkeypatch):
    async def scenario(client, sessions):
        token = session_token(await sign_in(client, remember=True))
        for path, data in [("/auth/lang", {"lang": "ar"}), ("/auth/timezone", {"timezone": "Africa/Cairo"})]:
            response = await client.request("POST", path, token, data=data)
            assert "Max-Age=2592000" in session_cookie(response), path
            token = session_token(response)
            assert decode_jwt(token)["remember"] is True
        payload = decode_jwt(token)
        assert payload["lang"] == "ar" and payload["tz"] == "Africa/Cairo"

    run(tmp_path, monkeypatch, scenario)


def test_active_sessions_renew_past_half_their_lifetime(tmp_path, monkeypatch):
    async def scenario(client, sessions):
        claims = {"sub": "7", "username": "member", "remember": True, "sv": 0}
        fresh = await visit(client, create_jwt(claims, expires_minutes=29 * 24 * 60))
        assert fresh.status_code == 200 and session_cookie(fresh) is None
        aging = await visit(client, create_jwt(claims, expires_minutes=10 * 24 * 60))
        assert aging.status_code == 200 and "Max-Age=2592000" in session_cookie(aging)
        assert decode_jwt(session_token(aging))["exp"] - time.time() > 29 * DAY

    run(tmp_path, monkeypatch, scenario)


def test_sign_out_of_all_devices_ends_every_session(tmp_path, monkeypatch):
    async def scenario(client, sessions):
        phone = session_token(await sign_in(client, remember=True))
        laptop = session_token(await sign_in(client, remember=True))
        assert (await visit(client, phone)).status_code == 200
        response = await client.request("POST", "/auth/signout-all", laptop)
        assert response.status_code == 303 and response.headers["location"] == "/auth/signin"
        for token in (phone, laptop):
            ended = await visit(client, token)
            assert ended.status_code == 303 and ended.headers["location"] == "/auth/signin"
        assert session_token(await sign_in(client, remember=False))  # signing in again still works

    run(tmp_path, monkeypatch, scenario)


def test_password_change_signs_out_other_devices_but_not_this_one(tmp_path, monkeypatch):
    async def scenario(client, sessions):
        other = session_token(await sign_in(client, remember=True))
        this = session_token(await sign_in(client, remember=True))
        response = await client.request("POST", "/auth/profile", this, data={
            "username": "member", "current_password": PASSWORD,
            "new_password": "brand-new-pass-2", "confirm_password": "brand-new-pass-2",
        })
        assert response.status_code == 303
        renewed = session_token(response)
        assert decode_jwt(renewed)["remember"] is True
        assert (await visit(client, renewed)).status_code == 200
        assert (await visit(client, other)).headers["location"] == "/auth/signin"

    run(tmp_path, monkeypatch, scenario)


def test_sessions_issued_before_this_change_stay_valid(tmp_path, monkeypatch):
    async def scenario(client, sessions):
        legacy = create_jwt({"sub": "7", "username": "member", "tz": "UTC", "lang": "en"})  # no "sv" or "remember"
        assert (await visit(client, legacy)).status_code == 200

    run(tmp_path, monkeypatch, scenario)
