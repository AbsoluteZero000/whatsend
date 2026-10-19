import time

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import TIMEZONE_CHOICES, settings
from app.database import get_db
from app.i18n import _
from app.models.oauth import OAuthGrant
from app.models.user import User
from app.services.auth import create_jwt, decode_jwt, hash_password, verify_password
from app.services.csrf import csrf_protect
from app.services.oauth import public_base_url
from app.services.rate_limit import check_rate_limit, clear_rate_limit


class RedirectRequired(Exception):
    def __init__(self, url: str):
        self.url = url

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(csrf_protect)])


def session_lifetime_seconds(remember: bool) -> int:
    return settings.remember_me_days * 86400 if remember else settings.jwt_expire_minutes * 60


def set_session_cookie(response: Response, payload: dict, remember: bool = False) -> None:
    """Issue a session. "remember" is stored in the token so every re-issue keeps the same lifetime."""
    claims = {key: value for key, value in payload.items() if key != "exp"}
    claims["remember"] = remember
    lifetime = session_lifetime_seconds(remember)
    response.set_cookie(
        key="session",
        value=create_jwt(claims, expires_minutes=lifetime // 60),
        httponly=True,
        secure=settings.cookie_secure,
        # Without "keep me signed in" the cookie is dropped when the browser session ends.
        max_age=lifetime if remember else None,
        samesite="lax",
    )


def session_needs_renewal(payload: dict) -> bool:
    """Renew active sessions once they are past half their lifetime."""
    expires_at = payload.get("exp")
    if not isinstance(expires_at, (int, float)):
        return False
    return expires_at - time.time() < session_lifetime_seconds(bool(payload.get("remember"))) / 2


def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get("session")
    if not token:
        return None
    payload = decode_jwt(token)
    if payload is None:
        return None
    return payload


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if user is None:
        raise RedirectRequired("/auth/signin")
    if user.get("force_password_change") and request.url.path not in {
        "/auth/profile",
        "/auth/signout",
    }:
        raise RedirectRequired("/auth/profile?first_login=1")
    if user.get("is_admin") and not admin_path_allowed(request.url.path):
        raise RedirectRequired("/admin")
    return user


def admin_path_allowed(path: str) -> bool:
    return (
        path == "/admin"
        or path.startswith("/admin/")
        or path in {"/auth/profile", "/auth/signout", "/auth/signout-all", "/auth/lang"}
        or path.startswith("/static/")
        or path.startswith("/health/")
    )


def _landing_page(user: User) -> str:
    return "/admin" if user.is_admin else "/dashboard"


def safe_next(value: str | None) -> str | None:
    """A page on this site to return to after signing in, e.g. the Claude connection page. Never another site."""
    if not value or len(value) > 2048 or not value.startswith("/") or value.startswith(("//", "/\\")):
        return None
    if any(ord(char) < 33 or char == "\\" for char in value):
        return None
    return value


def _session_payload(user: User) -> dict:
    return {
        "sub": str(user.id),
        "username": user.username,
        "tz": user.timezone,
        "lang": user.lang,
        "onboarded": user.onboarded,
        "is_admin": user.is_admin,
        "force_password_change": user.must_change_password,
        "sv": user.session_version or 0,
    }


async def ensure_default_admin(db: AsyncSession) -> User:
    username = "admin"
    user = await db.scalar(select(User).where(User.username == username))
    if user is None:
        user = User(
            username=username,
            password_hash=hash_password(settings.default_admin_password),
            timezone="UTC",
            onboarded=True,
            is_admin=True,
            must_change_password=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    elif not user.is_admin:
        user.is_admin = True
        await db.commit()
    return user


@router.get("/signup")
async def signup_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return request.app.state.render(request, "auth/signup.html", timezones=TIMEZONE_CHOICES)


@router.post("/signup")
async def signup(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    timezone: str = Form("UTC"),
    remember: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    username = username.strip()
    check_rate_limit(request, "signup", username, limit=5, window_seconds=3600)
    if len(username) < 3:
        return request.app.state.render(
            request, "auth/signup.html", error="Username must be at least 3 characters", timezones=TIMEZONE_CHOICES,
            username=username, selected_timezone=timezone,
        )
    if len(password) < 10:
        return request.app.state.render(
            request, "auth/signup.html", error="Password must be at least 10 characters", timezones=TIMEZONE_CHOICES,
            username=username, selected_timezone=timezone,
        )
    if timezone not in TIMEZONE_CHOICES:
        return request.app.state.render(
            request,
            "auth/signup.html",
            error="Choose a timezone from the list",
            timezones=TIMEZONE_CHOICES,
            username=username,
            selected_timezone=timezone,
        )

    result = await db.execute(select(User).where(User.username == username))
    if result.scalar_one_or_none():
        return request.app.state.render(
            request,
            "auth/signup.html",
            error="Username already taken",
            timezones=TIMEZONE_CHOICES,
            username=username,
            selected_timezone=timezone if timezone in TIMEZONE_CHOICES else "UTC",
        )

    user = User(
        username=username,
        password_hash=hash_password(password),
        timezone=timezone,
        is_admin=username in settings.admin_username_set,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    destination = "/auth/profile?first_login=1" if user.must_change_password else _landing_page(user)
    redirect = RedirectResponse(url=destination, status_code=303)
    set_session_cookie(redirect, _session_payload(user), remember=bool(remember))
    redirect.set_cookie(key="lang", value=user.lang, max_age=86400 * 365, samesite="lax")
    return redirect


@router.get("/signin")
async def signin_page(request: Request, next: str | None = None):
    if get_current_user(request):
        return RedirectResponse(url=safe_next(next) or "/dashboard", status_code=303)
    return request.app.state.render(request, "auth/signin.html", next_url=safe_next(next))


@router.post("/signin")
async def signin(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    remember: str | None = Form(None),
    next: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
):
    username = username.strip()
    next_url = safe_next(next)
    check_rate_limit(request, "signin", username, limit=10, window_seconds=900)
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return request.app.state.render(
            request, "auth/signin.html", error="Invalid credentials", username=username, remember=bool(remember),
            next_url=next_url,
        )

    if user.username in settings.admin_username_set and not user.is_admin:
        user.is_admin = True
        await db.commit()

    clear_rate_limit(request, "signin", username)
    if user.must_change_password:
        destination = "/auth/profile?first_login=1"
    else:
        destination = (not user.is_admin and next_url) or _landing_page(user)
    redirect = RedirectResponse(url=destination, status_code=303)
    set_session_cookie(redirect, _session_payload(user), remember=bool(remember))
    redirect.set_cookie(key="lang", value=user.lang, max_age=86400 * 365, samesite="lax")
    return redirect


@router.post("/signout")
async def signout():
    redirect = RedirectResponse(url="/auth/signin", status_code=303)
    redirect.delete_cookie(key="session")
    return redirect


@router.post("/signout-all")
async def signout_everywhere(request: Request, db: AsyncSession = Depends(get_db)):
    user_payload = require_user(request)
    user = await db.get(User, int(user_payload["sub"]))
    if user:
        user.session_version = (user.session_version or 0) + 1
        await db.commit()
    redirect = RedirectResponse(url="/auth/signin", status_code=303)
    redirect.delete_cookie(key="session")
    return redirect


@router.post("/onboarded")
async def mark_onboarded(request: Request, db: AsyncSession = Depends(get_db)):
    user_payload = require_user(request)
    user_id = int(user_payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.onboarded = True
        await db.commit()
    return JSONResponse({"ok": True})


@router.get("/timezone")
async def timezone_page(request: Request):
    user = require_user(request)
    return request.app.state.render(request, "auth/timezone.html", timezones=TIMEZONE_CHOICES, current_tz=user.get("tz", "UTC"))


@router.post("/timezone")
async def timezone_update(
    request: Request,
    timezone: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user_payload = require_user(request)
    user_id = int(user_payload["sub"])

    if timezone not in TIMEZONE_CHOICES:
        return request.app.state.render(
            request,
            "auth/timezone.html",
            error="Choose a timezone from the list",
            timezones=TIMEZONE_CHOICES,
            current_tz=timezone,
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.timezone = timezone
        await db.commit()

    lang = user_payload.get("lang", "en")
    onboarded = user_payload.get("onboarded", True)
    redirect = RedirectResponse(url="/dashboard", status_code=303)
    set_session_cookie(
        redirect, {**user_payload, "tz": timezone, "lang": lang, "onboarded": onboarded},
        remember=bool(user_payload.get("remember")),
    )
    redirect.set_cookie(key="lang", value=lang, max_age=86400 * 365, samesite="lax")
    return redirect


@router.get("/profile")
async def profile_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_payload = require_user(request)
    user_id = int(user_payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    connected_apps = []
    if settings.claude_connector_enabled:
        connected_apps = (await db.execute(select(OAuthGrant).where(
            OAuthGrant.user_id == user_id, OAuthGrant.access_hash.isnot(None),
        ).order_by(OAuthGrant.created_at.desc()))).scalars().all()
    return request.app.state.render(
        request,
        "auth/profile.html",
        username=user.username if user else "",
        force_password_change=bool(user and user.must_change_password),
        connected_apps=connected_apps,
        connector_url=public_base_url(request) + "/mcp",
    )


@router.post("/profile")
async def profile_update(
    request: Request,
    username: str = Form(None),
    current_password: str = Form(None),
    new_password: str = Form(None),
    confirm_password: str = Form(None),
    db: AsyncSession = Depends(get_db),
):
    user_payload = require_user(request)
    user_id = int(user_payload["sub"])

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return RedirectResponse(url="/auth/signin", status_code=303)

    force_password_change = user.must_change_password
    username = (username or "").strip()
    if username and len(username) < 3:
        return request.app.state.render(
            request,
            "auth/profile.html",
            username=username,
            force_password_change=force_password_change,
            error="Username must be at least 3 characters",
        )
    if username and username != user.username:
        existing = await db.execute(select(User).where(User.username == username))
        if existing.scalar_one_or_none():
            return request.app.state.render(
                request,
                "auth/profile.html",
                username=user.username,
                force_password_change=force_password_change,
                error=_("Username already taken", user.lang),
            )
        user.username = username

    password_fields_present = bool(current_password or new_password or confirm_password)
    if password_fields_present and not (current_password and new_password and confirm_password):
        return request.app.state.render(
            request, "auth/profile.html", username=user.username,
            force_password_change=force_password_change,
            error=(
                "Change the default password before continuing"
                if force_password_change
                else "Complete all password fields"
            ),
        )
    if password_fields_present:
        if not verify_password(current_password, user.password_hash):
            return request.app.state.render(
                request, "auth/profile.html", username=user.username,
                force_password_change=force_password_change,
                error=_("Current password is incorrect", user.lang),
            )
        if new_password != confirm_password:
            return request.app.state.render(
                request, "auth/profile.html", username=user.username,
                force_password_change=force_password_change,
                error=_("Passwords do not match", user.lang),
            )
        if len(new_password) < 10:
            return request.app.state.render(
                request, "auth/profile.html", username=user.username,
                force_password_change=force_password_change,
                error=_("Password must be at least 10 characters", user.lang),
            )
        if verify_password(new_password, user.password_hash):
            return request.app.state.render(
                request, "auth/profile.html", username=user.username,
                force_password_change=force_password_change,
                error="New password must be different from the current password",
            )
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        # A new password signs out every other device; this one gets a fresh session below.
        user.session_version = (user.session_version or 0) + 1

    await db.commit()
    await db.refresh(user)

    destination = _landing_page(user) if force_password_change else "/auth/profile"
    redirect = RedirectResponse(url=destination, status_code=303)
    set_session_cookie(redirect, _session_payload(user), remember=bool(user_payload.get("remember")))
    return redirect


@router.post("/lang")
async def lang_toggle(
    request: Request,
    lang: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if lang not in ("en", "ar"):
        lang = "en"

    user_payload = get_current_user(request)
    referer = request.headers.get("Referer", "/dashboard")

    if user_payload:
        user_id = int(user_payload["sub"])
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.lang = lang
            await db.commit()

        redirect = RedirectResponse(url=referer, status_code=303)
        set_session_cookie(redirect, {**user_payload, "lang": lang}, remember=bool(user_payload.get("remember")))
        redirect.set_cookie(key="lang", value=lang, max_age=86400 * 365, samesite="lax")
    else:
        redirect = RedirectResponse(url=referer, status_code=303)
        redirect.set_cookie(key="lang", value=lang, max_age=86400 * 365, samesite="lax")

    return redirect
