from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import TIMEZONE_CHOICES
from app.database import get_db
from app.i18n import _
from app.models.user import User
from app.services.auth import create_jwt, decode_jwt, hash_password, verify_password


class RedirectRequired(Exception):
    def __init__(self, url: str):
        self.url = url

router = APIRouter(prefix="/auth", tags=["auth"])


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
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.username == username))
    if result.scalar_one_or_none():
        return request.app.state.render(request, "auth/signup.html", error="Username already taken", timezones=TIMEZONE_CHOICES)

    if timezone not in TIMEZONE_CHOICES:
        timezone = "UTC"
    user = User(username=username, password_hash=hash_password(password), timezone=timezone)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_jwt({"sub": str(user.id), "username": user.username, "tz": user.timezone, "lang": user.lang})
    redirect = RedirectResponse(url="/dashboard", status_code=303)
    redirect.set_cookie(key="session", value=token, httponly=True, max_age=86400, samesite="lax")
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
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return request.app.state.render(request, "auth/signin.html", error="Invalid credentials")

    token = create_jwt({"sub": str(user.id), "username": user.username, "tz": user.timezone, "lang": user.lang})
    redirect = RedirectResponse(url="/dashboard", status_code=303)
    redirect.set_cookie(key="session", value=token, httponly=True, max_age=86400, samesite="lax")
    redirect.set_cookie(key="lang", value=user.lang, max_age=86400 * 365, samesite="lax")
    return redirect


@router.get("/signout")
async def signout():
    redirect = RedirectResponse(url="/auth/signin", status_code=303)
    redirect.delete_cookie(key="session")
    return redirect


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
        timezone = "UTC"

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.timezone = timezone
        await db.commit()

    lang = user_payload.get("lang", "en")
    token = create_jwt({"sub": str(user_id), "username": user_payload["username"], "tz": timezone, "lang": lang})
    redirect = RedirectResponse(url="/dashboard", status_code=303)
    redirect.set_cookie(key="session", value=token, httponly=True, max_age=86400, samesite="lax")
    redirect.set_cookie(key="lang", value=lang, max_age=86400 * 365, samesite="lax")
    return redirect


@router.get("/profile")
async def profile_page(request: Request, db: AsyncSession = Depends(get_db)):
    user_payload = require_user(request)
    user_id = int(user_payload["sub"])
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    return request.app.state.render(request, "auth/profile.html", username=user.username if user else "")


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

    if username and username != user.username:
        existing = await db.execute(select(User).where(User.username == username))
        if existing.scalar_one_or_none():
            return request.app.state.render(request, "auth/profile.html", username=user.username, error=_("Username already taken", user.lang))
        user.username = username

    if current_password and new_password:
        if not verify_password(current_password, user.password_hash):
            return request.app.state.render(request, "auth/profile.html", username=user.username, error=_("Current password is incorrect", user.lang))
        if new_password != confirm_password:
            return request.app.state.render(request, "auth/profile.html", username=user.username, error=_("Passwords do not match", user.lang))
        user.password_hash = hash_password(new_password)

    await db.commit()
    await db.refresh(user)

    token = create_jwt({"sub": str(user.id), "username": user.username, "tz": user.timezone, "lang": user.lang})
    redirect = RedirectResponse(url="/auth/profile", status_code=303)
    redirect.set_cookie(key="session", value=token, httponly=True, max_age=86400, samesite="lax")
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
        })
        redirect = RedirectResponse(url=referer, status_code=303)
        redirect.set_cookie(key="session", value=token, httponly=True, max_age=86400, samesite="lax")
        redirect.set_cookie(key="lang", value=lang, max_age=86400 * 365, samesite="lax")
    else:
        redirect = RedirectResponse(url=referer, status_code=303)
        redirect.set_cookie(key="lang", value=lang, max_age=86400 * 365, samesite="lax")

    return redirect
