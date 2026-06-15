from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.apikey import ApiKey
from app.models.token import Token
from app.models.user import User
from app.services.auth import verify_password
from app.services.crypto import decrypt_token
from app.services.sender import get_sender

router = APIRouter(prefix="/api", tags=["api"])


async def get_api_key_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    auth = request.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    raw_key = auth.removeprefix("Bearer ")

    parts = raw_key.split("_", 2)
    if len(parts) != 3 or parts[0] != "wts":
        raise HTTPException(status_code=401, detail="Invalid API key format")

    try:
        key_id = int(parts[1])
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid API key format")

    result = await db.execute(
        select(ApiKey).where(ApiKey.id == key_id, ApiKey.is_active == True)
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    if not verify_password(raw_key, api_key.key_hash):
        raise HTTPException(status_code=401, detail="Invalid API key")

    result = await db.execute(select(User).where(User.id == api_key.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")

    api_key.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    return user


@router.post("/send")
async def api_send(
    body: dict,
    user: User = Depends(get_api_key_user),
    db: AsyncSession = Depends(get_db),
):
    number = body.get("number")
    message = body.get("message")
    provider = body.get("provider", "whatsapp")

    if not number or not message:
        raise HTTPException(status_code=400, detail="number and message are required")

    if provider not in ("whatsapp", "signal"):
        raise HTTPException(status_code=400, detail="provider must be 'whatsapp' or 'signal'")

    result = await db.execute(
        select(Token).where(
            Token.user_id == user.id,
            Token.provider == provider,
            Token.is_active == True,
        )
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(
            status_code=400,
            detail=f"No active {provider} token found. Add one in the dashboard first.",
        )

    api_token = decrypt_token(token.api_token)
    sender = get_sender(provider, api_token)

    try:
        resp = await sender.send(number, message)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    token.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    return {"success": True, "data": resp}
