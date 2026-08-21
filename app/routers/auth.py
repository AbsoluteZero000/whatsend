from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import TIMEZONE_CHOICES, settings
from app.database import get_db
from app.i18n import _
from app.models.user import User
from app.services.auth import create_jwt, decode_jwt, hash_password, verify_password
from app.services.csrf import csrf_protect
from app.services.rate_limit import check_rate_limit, clear_rate_limit


class RedirectRequired(Exception):
    def __init__(self, url: str):
        self.url = url

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(csrf_protect)])


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="session",
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        max_age=86400,
        samesite="lax",
    )


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
    if user.get("force_username_change") and request.url.path not in {
        "/auth/profile",
        "/auth/signout",
    }:
        raise RedirectRequired("/auth/profile?first_login=1")
    return user


def _must_change_username(user: User) -> bool:
    return user.is_admin and user.username == "admin"


def _session_payload(user: User) -> dict:
    return {
        "sub": str(user.id),
        "username": user.username,
        "tz": user.timezone,
        "lang": user.lang,
        "onboarded": user.onboarded,
        "is_admin": user.is_admin,
        "force_username_change": _must_change_username(user),
    }


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

    if username in settings.admin_username_set:
        admin_exists = await db.scalar(select(func.count(User.id)).where(User.is_admin.is_(True)))
        if admin_exists:
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

    token = create_jwt(_session_payload(user))
    destination = "/auth/profile?first_login=1" if _must_change_username(user) else "/dashboard"
    redirect = RedirectResponse(url=destination, status_code=303)
    _set_session_cookie(redirect, token)
    redirect.set_cookie(key="lang", value=user.lang, max_age=86400 * 365, samesite="lax")
    return redirect


@router.get("/signin")
async def signin_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return request.app.state.render(request, "auth/signin.html")


@router.post("/signin")
async def signin(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    username = username.strip()
    check_rate_limit(request, "signin", username, limit=10, window_seconds=900)
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        return request.app.state.render(request, "auth/signin.html", error="Invalid credentials")

    if user.username in settings.admin_username_set and not user.is_admin:
        user.is_admin = True
        await db.commit()

    token = create_jwt(_session_payload(user))
    clear_rate_limit(request, "signin", username)
    destination = "/auth/profile?first_login=1" if _must_change_username(user) else "/dashboard"
    redirect = RedirectResponse(url=destination, status_code=303)
    _set_session_cookie(redirect, token)
    redirect.set_cookie(key="lang", value=user.lang, max_age=86400 * 365, samesite="lax")
    return redirect


@router.post("/signout")
async def signout():
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
    token = create_jwt({**user_payload, "tz": timezone, "lang": lang, "onboarded": onboarded})
    redirect = RedirectResponse(url="/dashboard", status_code=303)
    _set_session_cookie(redirect, token)
    redirect.set_cookie(key="lang", value=lang, max_age=86400 * 365, samesite="lax")
    return redirect


@router.get("/profile")
async def profile_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_payload = require_user(request)
    user_id = int(user_payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    return request.app.state.render(
        request,
        "auth/profile.html",
        username=user.username if user else "",
        force_username_change=bool(user and _must_change_username(user)),
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

    force_username_change = _must_change_username(user)
    username = (username or "").strip()
    if force_username_change and (not username or username == user.username):
        return request.app.state.render(
            request,
            "auth/profile.html",
            username=user.username,
            force_username_change=True,
            error="Choose a new username before continuing",
        )
    if username and len(username) < 3:
        return request.app.state.render(
            request,
            "auth/profile.html",
            username=username,
            force_username_change=force_username_change,
            error="Username must be at least 3 characters",
        )
    if username and username != user.username:
        existing = await db.execute(select(User).where(User.username == username))
        if existing.scalar_one_or_none():
            return request.app.state.render(
                request,
                "auth/profile.html",
                username=user.username,
                force_username_change=force_username_change,
                error=_("Username already taken", user.lang),
            )
        user.username = username

    if current_password and new_password:
        if not verify_password(current_password, user.password_hash):
            return request.app.state.render(
                request, "auth/profile.html", username=user.username,
                force_username_change=force_username_change,
                error=_("Current password is incorrect", user.lang),
            )
        if new_password != confirm_password:
            return request.app.state.render(
                request, "auth/profile.html", username=user.username,
                force_username_change=force_username_change,
                error=_("Passwords do not match", user.lang),
            )
        if len(new_password) < 10:
            return request.app.state.render(
                request, "auth/profile.html", username=user.username,
                force_username_change=force_username_change,
                error=_("Password must be at least 10 characters", user.lang),
            )
        user.password_hash = hash_password(new_password)

    await db.commit()
    await db.refresh(user)

    token = create_jwt(_session_payload(user))
    redirect = RedirectResponse(url="/auth/profile", status_code=303)
    _set_session_cookie(redirect, token)
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

        token = create_jwt({
            "sub": str(user_id),
            "username": user_payload["username"],
            "tz": user_payload.get("tz", "UTC"),
            "lang": lang,
            "onboarded": user_payload.get("onboarded", True),
            "is_admin": user_payload.get("is_admin", False),
        })
        redirect = RedirectResponse(url=referer, status_code=303)
        _set_session_cookie(redirect, token)
        redirect.set_cookie(key="lang", value=lang, max_age=86400 * 365, samesite="lax")
    else:
        redirect = RedirectResponse(url=referer, status_code=303)
        redirect.set_cookie(key="lang", value=lang, max_age=86400 * 365, samesite="lax")

    return redirect
