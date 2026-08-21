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


def test_default_admin_must_change_username():
    user = User(username="admin", password_hash="hash", is_admin=True)
    assert auth._must_change_username(user) is True
    assert auth._session_payload(user)["force_username_change"] is True


def test_renamed_admin_keeps_access_without_checkpoint():
    user = User(username="owner", password_hash="hash", is_admin=True)
    assert auth._must_change_username(user) is False
    assert auth._session_payload(user)["is_admin"] is True


def test_default_admin_is_redirected_to_username_checkpoint():
    token = create_jwt({
        "sub": "7",
        "username": "admin",
        "force_username_change": True,
    })
    request = SimpleNamespace(cookies={"session": token}, url=SimpleNamespace(path="/dashboard"))
    with pytest.raises(auth.RedirectRequired) as exc:
        auth.require_user(request)
    assert exc.value.url == "/auth/profile?first_login=1"


def test_default_admin_can_access_profile_checkpoint():
    token = create_jwt({
        "sub": "7",
        "username": "admin",
        "force_username_change": True,
    })
    request = SimpleNamespace(cookies={"session": token}, url=SimpleNamespace(path="/auth/profile"))
    assert auth.require_user(request)["username"] == "admin"
