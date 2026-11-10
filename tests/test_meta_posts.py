"""Facebook Page and Instagram posting, and the Connect Facebook / Connect Instagram flows, against mocked Meta APIs."""
import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_db
from app.models.job import Job
from app.models.token import Token
from app.models.user import User
from app.providers import get_registry
from app.providers._meta import GraphError, dump_credentials
from app.providers.facebook.sender import FacebookPageSender
from app.providers.instagram.sender import InstagramSender
from app.routers import instagram_connect, jobs, media, meta_connect
from app.services import instagram_tokens, public_media, scheduler
from app.services.crypto import decrypt_token, encrypt_token
from test_native_api import exercise
from test_native_jobs import no_schedule

CLIENT = httpx.AsyncClient
HEADERS = {"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"}
PAGE, INSTAGRAM, SECRET = "1001", "1789", "page-secret-token"


@pytest.fixture(autouse=True)
def meta_settings(monkeypatch):
    monkeypatch.setattr(settings, "meta_graph_base_url", "https://graph.facebook.com")
    monkeypatch.setattr(settings, "meta_graph_api_version", "v23.0")
    monkeypatch.setattr(settings, "meta_app_id", "app-id")
    monkeypatch.setattr(settings, "meta_app_secret", "app-secret")
    monkeypatch.setattr(settings, "public_url", "https://echosend.test")
    monkeypatch.setattr(settings, "instagram_app_id", "ig-app-id")
    monkeypatch.setattr(settings, "instagram_app_secret", "ig-app-secret")
    monkeypatch.setattr(settings, "instagram_graph_base_url", "https://graph.instagram.com")
    monkeypatch.setattr(settings, "instagram_login_config_id", "")


def mock_graph(monkeypatch, handler, *, token_in_url_allowed=False):
    calls = []

    def record(request):
        calls.append(request)
        if not token_in_url_allowed:
            assert SECRET not in str(request.url), "access tokens must never appear in URLs"
        return handler(request)

    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kwargs: CLIENT(transport=httpx.MockTransport(record), **kwargs))
    return calls


def form(request):
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def facebook():
    return FacebookPageSender(dump_credentials(SECRET, PAGE))


def instagram():
    return InstagramSender(dump_credentials(SECRET, INSTAGRAM), poll_interval=0)


async def owner_database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(User(id=7, username="owner", password_hash="hash", is_active=True))
        await db.commit()
    return engine, sessions


def login_app(router, module, monkeypatch, sessions):
    async def database():
        async with sessions() as db:
            yield db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(module, "require_user", lambda request: {"sub": "7"})
    return CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_facebook_text_photo_and_video_posts(tmp_path, monkeypatch):
    photo, video = tmp_path / "a.jpg", tmp_path / "b.mp4"
    photo.write_bytes(b"\xff\xd8\xff" + b"0" * 20)
    video.write_bytes(b"0000ftypisom")
    calls = mock_graph(monkeypatch, lambda request: httpx.Response(200, json={"id": f"{PAGE}_1"}))

    async def run():
        sender = facebook()
        assert await sender.send(PAGE, "Hello Page") == {"id": f"{PAGE}_1"}
        await sender.send(PAGE, "Photo caption", str(photo))
        await sender.send(PAGE, "Video text", str(video))

    asyncio.run(run())
    text, photo_call, video_call = calls
    assert text.url.host == "graph.facebook.com" and text.url.path == f"/v23.0/{PAGE}/feed"
    assert form(text) == {"message": "Hello Page"}
    assert text.headers["authorization"] == f"Bearer {SECRET}"
    assert "appsecret_proof" in text.url.params
    assert photo_call.url.path == f"/v23.0/{PAGE}/photos" and b"Photo caption" in photo_call.content
    assert video_call.url.host == "graph-video.facebook.com" and video_call.url.path == f"/v23.0/{PAGE}/videos"
    assert b'name="description"' in video_call.content and b'name="source"' in video_call.content


def test_facebook_rejects_other_pages_empty_posts_and_documents(tmp_path):
    sender, document = facebook(), tmp_path / "notes.pdf"
    document.write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="its own Page"):
        sender.validate_payload("someone-else", "Hi")
    with pytest.raises(ValueError, match="needs text"):
        sender.validate_payload(PAGE, "  ")
    with pytest.raises(ValueError, match="JPEG, PNG or GIF"):
        sender.validate_payload(PAGE, "Hi", str(document))


def test_expired_login_explains_how_to_fix_it(monkeypatch):
    mock_graph(monkeypatch, lambda request: httpx.Response(400, json={
        "error": {"message": "Error validating access token", "code": 190}}))
    with pytest.raises(GraphError, match="Connect Facebook again") as error:
        asyncio.run(facebook().send(PAGE, "Hi"))
    assert SECRET not in str(error.value)
    with pytest.raises(GraphError, match="Connect Instagram again"):
        asyncio.run(instagram().get_groups())


def test_instagram_photo_is_published_from_a_signed_link(tmp_path, monkeypatch):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff" + b"0" * 20)

    def graph(request):
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "container-1"})
        if request.url.path.endswith("/container-1"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        return httpx.Response(200, json={"id": "ig-media-1"})

    calls = mock_graph(monkeypatch, graph)
    result = asyncio.run(instagram().send(INSTAGRAM, "Caption #one", str(photo)))
    assert result == {"id": "ig-media-1", "container_id": "container-1"}
    create, status, publish = calls
    assert {call.url.host for call in calls} == {"graph.instagram.com"}
    assert "appsecret_proof" not in create.url.params
    assert create.headers["authorization"] == f"Bearer {SECRET}"
    data = form(create)
    assert data["caption"] == "Caption #one" and "media_type" not in data
    link = urlsplit(data["image_url"])
    assert link.netloc == "echosend.test"
    _, _, _, expires, signature, filename = link.path.split("/")
    assert filename == "photo.jpg" and public_media.verify_public_media(filename, int(expires), signature)
    assert status.url.params["fields"] == "status_code,status"
    assert publish.url.path == f"/v23.0/{INSTAGRAM}/media_publish" and form(publish) == {"creation_id": "container-1"}


def test_instagram_account_lookup_uses_the_professional_account_id(monkeypatch):
    calls = mock_graph(monkeypatch, lambda request: httpx.Response(200, json={
        "id": "app-scoped", "user_id": INSTAGRAM, "username": "me.creator"}))
    assert asyncio.run(instagram().get_groups()) == [{"id": INSTAGRAM, "name": "@me.creator"}]
    assert calls[0].url.path == "/v23.0/me"


def test_instagram_video_waits_for_processing_and_reports_errors(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0000ftypisom")
    states = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])

    def graph(request):
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "reel-1"})
        if request.url.path.endswith("/reel-1"):
            return httpx.Response(200, json={"status_code": next(states)})
        return httpx.Response(200, json={"id": "ig-reel"})

    calls = mock_graph(monkeypatch, graph)
    assert asyncio.run(instagram().send(INSTAGRAM, "Reel", str(video)))["id"] == "ig-reel"
    assert form(calls[0])["media_type"] == "REELS" and "video_url" in form(calls[0])
    assert len(calls) == 5

    mock_graph(monkeypatch, lambda request: httpx.Response(200, json=(
        {"id": "reel-2"} if request.url.path.endswith("/media") else {"status_code": "ERROR", "status": "Bad codec"})))
    with pytest.raises(GraphError, match="Bad codec"):
        asyncio.run(instagram().send(INSTAGRAM, "Reel", str(video)))


def test_instagram_validation(tmp_path, monkeypatch):
    sender, png, photo = instagram(), tmp_path / "a.png", tmp_path / "a.jpg"
    png.write_bytes(b"\x89PNG")
    photo.write_bytes(b"\xff\xd8\xff")
    with pytest.raises(ValueError, match="need a photo or video"):
        sender.validate_payload(INSTAGRAM, "Text only")
    with pytest.raises(ValueError, match="Save the photo as .jpg"):
        sender.validate_payload(INSTAGRAM, "Hi", str(png))
    with pytest.raises(ValueError, match="hashtags"):
        sender.validate_payload(INSTAGRAM, "#a " * 31, str(photo))
    with pytest.raises(ValueError, match="its own account"):
        sender.validate_payload(PAGE, "Hi", str(photo))
    monkeypatch.setattr(settings, "public_url", "")
    with pytest.raises(ValueError, match="PUBLIC_URL"):
        sender.validate_payload(INSTAGRAM, "Hi", str(photo))


def test_signed_media_links_serve_only_valid_unexpired_files(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(media, "UPLOAD_DIR", tmp_path)
    (tmp_path / "pic.jpg").write_bytes(b"\xff\xd8\xffdata")
    app = FastAPI()
    app.include_router(media.router)
    client = TestClient(app)
    path = urlsplit(public_media.public_media_url(str(tmp_path / "pic.jpg"))).path
    assert client.get(path).content == b"\xff\xd8\xffdata"
    assert client.get(path[:-7] + "x" + path[-6:]).status_code == 404
    old = urlsplit(public_media.public_media_url(str(tmp_path / "pic.jpg"), now=0)).path
    assert client.get(old).status_code == 404
    assert client.get(path.replace("pic.jpg", "other.jpg")).status_code == 404


def test_connect_facebook_adds_only_the_shared_pages_once(tmp_path, monkeypatch):
    def graph(request):
        if request.url.path.endswith("/oauth/access_token"):
            params = request.url.params
            assert params["client_secret"] == "app-secret"
            if params.get("code"):
                assert params["code"] == "login-code"
                assert params["redirect_uri"] == "https://echosend.test/tokens/meta/callback"
                return httpx.Response(200, json={"access_token": "short-user"})
            assert params["fb_exchange_token"] == "short-user"
            return httpx.Response(200, json={"access_token": "long-user"})
        assert request.url.path == "/v23.0/me/accounts"
        assert request.headers["authorization"] == "Bearer long-user"
        assert "instagram" not in request.url.params["fields"]
        return httpx.Response(200, json={"data": [
            {"id": PAGE, "name": "My Shop", "access_token": SECRET},
            {"id": "2002", "name": "Side Page", "access_token": SECRET},
        ]})

    async def scenario():
        engine, sessions = await owner_database(tmp_path)
        async with login_app(meta_connect.router, meta_connect, monkeypatch, sessions) as client:
            start = await client.get("/tokens/meta/connect")
            assert start.status_code == 303
            dialog = urlsplit(start.headers["location"])
            query = parse_qs(dialog.query)
            assert dialog.netloc == "www.facebook.com" and query["client_id"] == ["app-id"]
            assert "pages_manage_posts" in query["scope"][0] and "instagram" not in query["scope"][0]
            state = query["state"][0]
            assert client.cookies.get("social_oauth_state") == state

            forged = await client.get("/tokens/meta/callback", params={"code": "login-code", "state": "wrong"})
            assert "expired" in forged.headers["location"]

            mock_graph(monkeypatch, graph)
            for _ in range(2):
                client.cookies.set("social_oauth_state", state, path="/tokens/meta")
                done = await client.get("/tokens/meta/callback", params={"code": "login-code", "state": state})
                assert done.status_code == 303 and "success=Connected+2+Facebook+Pages." in done.headers["location"]
        async with sessions() as db:
            tokens = (await db.execute(select(Token).order_by(Token.id))).scalars().all()
        await engine.dispose()
        return tokens

    tokens = asyncio.run(scenario())
    assert [(token.provider, token.name) for token in tokens] == [("facebook", "My Shop"), ("facebook", "Side Page")]
    assert all(SECRET not in token.api_token for token in tokens)
    assert json.loads(decrypt_token(tokens[0].api_token)) == {"t": SECRET, "id": PAGE}


def test_connect_instagram_needs_no_facebook_page(tmp_path, monkeypatch):
    monkeypatch.setattr(instagram_connect.time, "time", lambda: 1_000_000)

    def instagram_api(request):
        assert request.url.host != "graph.facebook.com", "Instagram Login must not use the Facebook Graph API"
        if request.url.host == "api.instagram.com":
            assert form(request) == {
                "client_id": "ig-app-id", "client_secret": "ig-app-secret", "grant_type": "authorization_code",
                "redirect_uri": "https://echosend.test/tokens/instagram/callback", "code": "ig-code"}
            return httpx.Response(200, json={"data": [{"access_token": "short-ig", "user_id": INSTAGRAM}]})
        if request.url.path == "/access_token":
            assert request.url.params["grant_type"] == "ig_exchange_token"
            assert request.url.params["access_token"] == "short-ig"
            return httpx.Response(200, json={"access_token": SECRET, "expires_in": 5_184_000})
        assert request.url.path == "/v23.0/me" and request.headers["authorization"] == f"Bearer {SECRET}"
        return httpx.Response(200, json={"user_id": INSTAGRAM, "username": "me.creator"})

    async def scenario():
        engine, sessions = await owner_database(tmp_path)
        async with login_app(instagram_connect.router, instagram_connect, monkeypatch, sessions) as client:
            start = await client.get("/tokens/instagram/connect")
            dialog = urlsplit(start.headers["location"])
            query = parse_qs(dialog.query)
            assert dialog.netloc == "www.instagram.com" and dialog.path == "/oauth/authorize"
            assert query["client_id"] == ["ig-app-id"]
            assert query["scope"] == ["instagram_business_basic,instagram_business_content_publish"]
            state = query["state"][0]
            mock_graph(monkeypatch, instagram_api)
            for _ in range(2):
                client.cookies.set("social_oauth_state", state, path="/tokens/instagram")
                done = await client.get("/tokens/instagram/callback", params={"code": "ig-code", "state": state})
                assert "success=Connected+Instagram+account+%40me.creator" in done.headers["location"]
        async with sessions() as db:
            tokens = (await db.execute(select(Token))).scalars().all()
        await engine.dispose()
        return tokens

    tokens = asyncio.run(scenario())
    assert [(token.provider, token.name) for token in tokens] == [("instagram", "@me.creator")]
    assert json.loads(decrypt_token(tokens[0].api_token)) == {"t": SECRET, "id": INSTAGRAM, "exp": 1_000_000 + 5_184_000}


def test_connect_instagram_with_unified_login_adds_every_shared_account(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "instagram_login_config_id", "ig-config")
    other = "1790"

    def graph(request):
        assert request.url.host == "graph.facebook.com", "Unified login uses the Facebook Graph API"
        if request.url.path == "/v23.0/oauth/access_token":
            assert dict(request.url.params) == {
                "client_id": "app-id", "client_secret": "app-secret", "code": "fb-code",
                "redirect_uri": "https://echosend.test/tokens/instagram/callback"}
            return httpx.Response(200, json={"access_token": SECRET, "token_type": "bearer"})
        assert request.url.path == "/v23.0/me/assigned_instagram_accounts"
        assert request.headers["authorization"] == f"Bearer {SECRET}"
        assert request.url.params["fields"] == "id,username" and request.url.params["appsecret_proof"]
        return httpx.Response(200, json={"data": [{"id": INSTAGRAM, "username": "me.creator"}, {"id": other}]})

    async def scenario():
        engine, sessions = await owner_database(tmp_path)
        async with login_app(instagram_connect.router, instagram_connect, monkeypatch, sessions) as client:
            start = await client.get("/tokens/instagram/connect")
            dialog = urlsplit(start.headers["location"])
            query = {key: values[0] for key, values in parse_qs(dialog.query).items()}
            assert dialog.netloc == "www.facebook.com" and dialog.path == "/v23.0/dialog/oauth"
            assert query["client_id"] == "app-id" and query["config_id"] == "ig-config"
            assert query["override_default_response_type"] == "true" and "scope" not in query
            mock_graph(monkeypatch, graph, token_in_url_allowed=False)
            client.cookies.set("social_oauth_state", query["state"], path="/tokens/instagram")
            done = await client.get("/tokens/instagram/callback", params={"code": "fb-code", "state": query["state"]})
            assert "Connected+Instagram+accounts+%40me.creator%2C+1790" in done.headers["location"]
        async with sessions() as db:
            tokens = (await db.execute(select(Token))).scalars().all()
        await engine.dispose()
        return tokens

    tokens = asyncio.run(scenario())
    assert sorted((token.provider, token.name) for token in tokens) == [("instagram", "1790"), ("instagram", "@me.creator")]
    saved = {token.name: json.loads(decrypt_token(token.api_token)) for token in tokens}
    assert saved["@me.creator"] == {"t": SECRET, "id": INSTAGRAM, "api": "facebook"}


def test_unified_login_with_no_shared_account_explains_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "instagram_login_config_id", "ig-config")
    mock_graph(monkeypatch, lambda request: httpx.Response(200, json=(
        {"access_token": SECRET} if request.url.path.endswith("oauth/access_token") else {"data": []})))

    async def scenario():
        engine, sessions = await owner_database(tmp_path)
        async with login_app(instagram_connect.router, instagram_connect, monkeypatch, sessions) as client:
            client.cookies.set("social_oauth_state", "s", path="/tokens/instagram")
            done = await client.get("/tokens/instagram/callback", params={"code": "fb-code", "state": "s"})
        await engine.dispose()
        return done.headers["location"]

    assert "No+Instagram+account+was+shared" in asyncio.run(scenario())


def test_unified_login_posts_through_the_facebook_graph_api(tmp_path, monkeypatch):
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(bytes([0xFF, 0xD8, 0xFF]) + b"0" * 20)

    def graph(request):
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "container-1"})
        if request.url.path.endswith("/container-1"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if request.method == "GET":
            return httpx.Response(200, json={"id": INSTAGRAM, "username": "me.creator"})
        return httpx.Response(200, json={"id": "ig-media-1"})

    calls = mock_graph(monkeypatch, graph)
    sender = InstagramSender(dump_credentials(SECRET, INSTAGRAM, api="facebook"), poll_interval=0)
    assert asyncio.run(sender.get_groups()) == [{"id": INSTAGRAM, "name": "@me.creator"}]
    assert calls[0].url.path == f"/v23.0/{INSTAGRAM}"
    result = asyncio.run(sender.send(INSTAGRAM, "Caption", str(photo)))
    assert result == {"id": "ig-media-1", "container_id": "container-1"}
    assert {call.url.host for call in calls} == {"graph.facebook.com"}
    assert all(call.url.params.get("appsecret_proof") for call in calls)
    assert calls[-1].url.path == f"/v23.0/{INSTAGRAM}/media_publish"


def test_instagram_sign_in_failures_suggest_a_professional_account(tmp_path, monkeypatch):
    mock_graph(monkeypatch, lambda request: httpx.Response(400, json={
        "error_type": "OAuthException", "code": 400, "error_message": "Invalid user type"}))

    async def scenario():
        engine, sessions = await owner_database(tmp_path)
        async with login_app(instagram_connect.router, instagram_connect, monkeypatch, sessions) as client:
            client.cookies.set("social_oauth_state", "s", path="/tokens/instagram")
            done = await client.get("/tokens/instagram/callback", params={"code": "ig-code", "state": "s"})
        await engine.dispose()
        return done.headers["location"]

    location = asyncio.run(scenario())
    assert "Invalid+user+type" in location and "Creator+or+Business" in location


def test_instagram_logins_are_renewed_before_they_expire(tmp_path, monkeypatch):
    day, now = 86400, 50_000_000
    renewals = []

    def instagram_api(request):
        assert request.url.path == "/refresh_access_token"
        assert request.url.params["grant_type"] == "ig_refresh_token"
        renewals.append(request.url.params["access_token"])
        return httpx.Response(200, json={"access_token": "renewed", "expires_in": 60 * day})

    async def scenario():
        engine, sessions = await owner_database(tmp_path)
        monkeypatch.setattr(instagram_tokens, "async_session", sessions)
        async with sessions() as db:
            for name, expires in (("soon", now + 5 * day), ("later", now + 50 * day), ("expired", now - day)):
                db.add(Token(user_id=7, name=name, provider="instagram",
                             api_token=encrypt_token(dump_credentials(f"{name}-token", INSTAGRAM, expires))))
            await db.commit()
        mock_graph(monkeypatch, instagram_api)
        assert await instagram_tokens.renew_instagram_tokens(now=now) == 1
        async with sessions() as db:
            tokens = {token.name: json.loads(decrypt_token(token.api_token))
                      for token in (await db.execute(select(Token))).scalars()}
        await engine.dispose()
        return tokens

    tokens = asyncio.run(scenario())
    assert renewals == ["soon-token"]
    assert tokens["soon"] == {"t": "renewed", "id": INSTAGRAM, "exp": now + 60 * day}
    assert tokens["later"]["t"] == "later-token" and tokens["expired"]["t"] == "expired-token"


@pytest.mark.parametrize("module, setting, path, hint", [
    (meta_connect, "meta_app_id", "/tokens/meta/connect", "META_APP_ID"),
    (instagram_connect, "instagram_app_id", "/tokens/instagram/connect", "INSTAGRAM_APP_ID"),
])
def test_connect_buttons_need_app_credentials(monkeypatch, module, setting, path, hint):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(settings, setting, "")
    app = FastAPI()
    app.include_router(module.router)
    monkeypatch.setattr(module, "require_user", lambda request: {"sub": "7"})
    response = TestClient(app).get(path, follow_redirects=False)
    assert hint in response.headers["location"]


@pytest.mark.parametrize("config_id, expected", [
    ("", {"scope": "pages_show_list,pages_read_engagement,pages_manage_posts,business_management"}),
    ("1113", {"config_id": "1113"}),
])
def test_facebook_login_uses_the_business_login_configuration_when_set(monkeypatch, config_id, expected):
    from urllib.parse import parse_qs, urlparse
    from fastapi.testclient import TestClient
    monkeypatch.setattr(settings, "meta_app_id", "app")
    monkeypatch.setattr(settings, "meta_app_secret", "secret")
    monkeypatch.setattr(settings, "meta_login_config_id", config_id)
    app = FastAPI()
    app.include_router(meta_connect.router)
    monkeypatch.setattr(meta_connect, "require_user", lambda request: {"sub": "7"})
    location = TestClient(app).get("/tokens/meta/connect", follow_redirects=False).headers["location"]
    query = {key: values[0] for key, values in parse_qs(urlparse(location).query).items()}
    assert {key: query.get(key) for key in expected} == expected
    assert not ({"scope", "config_id"} - set(expected)) & set(query)


def test_scheduled_facebook_post_runs_through_the_normal_job_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "register_job", no_schedule)
    posts = []

    def graph(request):
        if request.url.host in {"native.test"}:
            pytest.fail("Facebook posts must never contact the WhatsApp worker")
        if request.method == "GET":
            return httpx.Response(200, json={"id": PAGE, "name": "My Shop"})
        posts.append(form(request))
        return httpx.Response(200, json={"id": f"{PAGE}_9"})

    async def scenario(client, sessions, context):
        async with sessions() as db:
            token = Token(user_id=7, name="My Shop", provider="facebook",
                          api_token=encrypt_token(dump_credentials(SECRET, PAGE)))
            db.add(token)
            await db.commit()
            token_id = token.id
        assert (await client.get(f"/jobs/groups?token_id={token_id}")).json() == {"groups": [{"id": PAGE, "name": "My Shop"}]}
        rejected = await client.post("/jobs/create", headers=HEADERS, data={
            "token_id": str(token_id), "group_ids": "not-my-page", "message": "Hi", "trigger_type": "trigger"})
        assert rejected.status_code == 400
        response = await client.post("/jobs/create", headers=HEADERS, data={
            "token_id": str(token_id), "group_ids": PAGE, "group_names": "My Shop",
            "message": "Weekly special", "trigger_type": "trigger"})
        assert response.status_code == 303, response.text
        async with sessions() as db:
            job_id = (await db.execute(select(Job.id))).scalar_one()
        await scheduler.send_job(job_id)
        assert posts == [{"message": "Weekly special"}]
        async with sessions() as db:
            assert (await db.get(Job, job_id)).status == "trigger"

    asyncio.run(exercise(tmp_path, monkeypatch, graph, scenario))


def test_social_providers_are_registered_but_hidden_from_the_token_form():
    registry = get_registry()
    assert registry.get("facebook").auth == registry.get("instagram").auth == "managed"
    assert registry.label("instagram") == "Instagram"
