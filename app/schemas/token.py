from datetime import datetime

from pydantic import BaseModel


class TokenCreate(BaseModel):
    name: str | None = None
    api_token: str


class TokenOut(BaseModel):
    id: int
    user_id: int
    name: str | None
    api_token: str
    is_active: bool
    created_at: datetime | None
    last_used_at: datetime | None

    class Config:
        from_attributes = True
