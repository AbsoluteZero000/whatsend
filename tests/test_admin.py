import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.user import User
from app.routers import admin, auth
from app.services.auth import create_jwt


class ScalarDb:
    def __init__(self, user):
        self.user = user

    async def scalar(self, _query):
        return self.user


class BootstrapDb:
    def __init__(self):
        self.added = None
        self.commits = 0

    async def scalar(self, _query):
        return None

    def add(self, user):
        self.added = user

    async def commit(self):
        self.commits += 1

    async def refresh(self, _user):
        return None


def test_require_admin_accepts_active_admin(monkeypatch):
    monkeypatch.setattr(admin, "require_user", lambda _request: {"sub": "7"})
    user = User(id=7, username="admin", password_hash="hash", is_admin=True, is_active=True)
    assert asyncio.run(admin.require_admin(object(), ScalarDb(user))) is user


@pytest.mark.parametrize(
    "user",
    [
        User(id=7, username="member", password_hash="hash", is_admin=False, is_active=True),
        User(id=7, username="disabled", password_hash="hash", is_admin=True, is_active=False),
        None,
    ],
)
def test_require_admin_rejects_non_admin_accounts(monkeypatch, user):
    monkeypatch.setattr(admin, "require_user", lambda _request: {"sub": "7"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.require_admin(object(), ScalarDb(user)))
    assert exc.value.status_code == 403


def test_bootstrap_admin_uses_default_password_and_requires_change(monkeypatch):
    db = BootstrapDb()
    monkeypatch.setattr(auth, "hash_password", lambda password: f"hashed:{password}")
    user = asyncio.run(auth.ensure_default_admin(db))
    assert user.username == "admin"
    assert user.password_hash == "hashed:admin"
    assert user.is_admin is True
    assert user.must_change_password is True
    assert db.commits == 1


def test_admin_with_changed_password_has_no_checkpoint():
    user = User(
        username="admin", password_hash="hash", is_admin=True,
        must_change_password=False,
    )
    payload = auth._session_payload(user)
    assert payload["is_admin"] is True
    assert payload["force_password_change"] is False


def test_default_admin_is_redirected_to_password_checkpoint():
    token = create_jwt({
        "sub": "7",
        "username": "admin",
        "force_password_change": True,
    })
    request = SimpleNamespace(cookies={"session": token}, url=SimpleNamespace(path="/dashboard"))
    with pytest.raises(auth.RedirectRequired) as exc:
        auth.require_user(request)
    assert exc.value.url == "/auth/profile?first_login=1"


def test_default_admin_can_access_profile_checkpoint():
    token = create_jwt({
        "sub": "7",
        "username": "admin",
        "force_password_change": True,
    })
    request = SimpleNamespace(cookies={"session": token}, url=SimpleNamespace(path="/auth/profile"))
    assert auth.require_user(request)["username"] == "admin"


def test_admin_cannot_deactivate_self(monkeypatch):
    current = User(id=7, username="admin", password_hash="hash", is_admin=True, is_active=True)

    async def current_admin(_request, _db):
        return current

    monkeypatch.setattr(admin, "require_admin", current_admin)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(admin.toggle_user_active(7, object(), q="", page=1, db=ScalarDb(current)))
    assert exc.value.status_code == 400


def test_admin_can_deactivate_user(monkeypatch):
    current = User(id=7, username="admin", password_hash="hash", is_admin=True, is_active=True)
    target = User(id=8, username="member", password_hash="hash", is_admin=False, is_active=True)

    async def current_admin(_request, _db):
        return current

    class ToggleDb(ScalarDb):
        committed = False

        async def commit(self):
            self.committed = True

    db = ToggleDb(target)
    monkeypatch.setattr(admin, "require_admin", current_admin)
    response = asyncio.run(admin.toggle_user_active(8, object(), q="ali", page=2, db=db))
    assert target.is_active is False
    assert db.committed is True
    assert response.status_code == 303
    assert response.headers["location"] == "/admin?page=2&q=ali"
