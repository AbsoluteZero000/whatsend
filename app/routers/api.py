from datetime import datetime, timezone
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
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


class SendMessageRequest(BaseModel):
    group_id: str | None = Field(default=None, min_length=1)
    number: str | None = Field(default=None, min_length=1)
    message: str = Field(min_length=1)
    provider: str = "whatsapp"


@dataclass
class ApiKeyContext:
    user: User
    api_key: ApiKey


async def get_api_key_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ApiKeyContext:
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

    return ApiKeyContext(user=user, api_key=api_key)


@router.post("/send")
async def api_send(
    body: SendMessageRequest,
    context: ApiKeyContext = Depends(get_api_key_user),
    db: AsyncSession = Depends(get_db),
):
    group_id = body.group_id or body.number
    message = body.message
    provider = body.provider

    if not group_id:
        raise HTTPException(status_code=400, detail="group_id is required")

    if provider != "whatsapp":
        raise HTTPException(status_code=400, detail="provider must be 'whatsapp'")

    user = context.user
    api_key = context.api_key

    if not api_key.token_id:
        raise HTTPException(status_code=400, detail="API key is not linked to a token. Generate a new API key and select a token.")

    result = await db.execute(
        select(Token).where(
            Token.id == api_key.token_id,
            Token.user_id == user.id,
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
        resp = await sender.send(group_id, message)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    token.last_used_at = datetime.now(timezone.utc)
    await db.commit()

    return {"success": True, "group_id": group_id, "data": resp}
