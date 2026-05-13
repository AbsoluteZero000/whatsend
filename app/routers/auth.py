from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.services.auth import create_jwt, decode_jwt, hash_password, verify_password

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
        raise RedirectResponse(url="/auth/signin", status_code=303)
    return user


@router.get("/signup")
async def signup_page(request: Request):
    if get_current_user(request):
        return RedirectResponse(url="/dashboard", status_code=303)
    return request.app.state.render(request, "auth/signup.html")


@router.post("/signup")
async def signup(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where((User.username == username) | (User.email == email)))
    if result.scalar_one_or_none():
        return request.app.state.render(request, "auth/signup.html", error="Username or email already taken")

    user = User(username=username, email=email, password_hash=hash_password(password))
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_jwt({"sub": str(user.id), "username": user.username})
    redirect = RedirectResponse(url="/dashboard", status_code=303)
    redirect.set_cookie(key="session", value=token, httponly=True, max_age=86400, samesite="lax")
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

    token = create_jwt({"sub": str(user.id), "username": user.username})
    redirect = RedirectResponse(url="/dashboard", status_code=303)
    redirect.set_cookie(key="session", value=token, httponly=True, max_age=86400, samesite="lax")
    return redirect


@router.get("/signout")
async def signout():
    redirect = RedirectResponse(url="/auth/signin", status_code=303)
    redirect.delete_cookie(key="session")
    return redirect
