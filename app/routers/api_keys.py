import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.apikey import ApiKey
from app.models.token import Token
from app.routers.auth import require_user
from app.services.auth import hash_password
from app.services.csrf import csrf_protect

router = APIRouter(prefix="/api-keys", tags=["api_keys"], dependencies=[Depends(csrf_protect)])


def _flash(url: str, success: str = "") -> RedirectResponse:
    if success:
        url += ("&" if "?" in url else "?") + urlencode({"success": success})
    return RedirectResponse(url=url, status_code=303)


@router.get("")
async def list_api_keys(request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(
        select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.created_at.desc())
    )
    api_keys = result.scalars().all()
    result = await db.execute(select(Token).where(Token.user_id == user_id))
    tokens = result.scalars().all()
    token_names = {token.id: token.name or f"Token #{token.id}" for token in tokens}
    return request.app.state.render(request, "api_keys/list.html", api_keys=api_keys, token_names=token_names)


@router.get("/create")
async def create_api_key_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])
    result = await db.execute(
        select(Token).where(Token.user_id == user_id, Token.is_active == True).order_by(Token.created_at.desc())
    )
    tokens = result.scalars().all()
    return request.app.state.render(request, "api_keys/create.html", tokens=tokens)


@router.post("/create")
async def create_api_key(
    request: Request,
    name: str = Form(default=""),
    token_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(
        select(Token).where(Token.id == token_id, Token.user_id == user_id, Token.is_active == True)
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=400, detail="Select an active token for this API key")

    api_key = ApiKey(
        user_id=user_id,
        token_id=token.id,
        name=name or None,
        key_prefix="",
        key_hash="pending",
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    raw_key = f"wts_{api_key.id}_{secrets.token_hex(20)}"
    api_key.key_prefix = f"wts_{api_key.id}_{raw_key.split('_')[2][:8]}"
    api_key.key_hash = hash_password(raw_key)
    await db.commit()

    return request.app.state.render(request, "api_keys/create.html", new_key=raw_key, tokens=[token])


@router.post("/{key_id}/toggle")
async def toggle_api_key(key_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
    )
    api_key = result.scalar_one_or_none()
    if api_key:
        api_key.is_active = not api_key.is_active
        await db.commit()
    return _flash("/api-keys", success="API key toggled")


@router.post("/{key_id}/delete")
async def delete_api_key(key_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    user_id = int(user["sub"])

    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
    )
    api_key = result.scalar_one_or_none()
    if api_key:
        await db.delete(api_key)
        await db.commit()
    return _flash("/api-keys", success="API key deleted")
