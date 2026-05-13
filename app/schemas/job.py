from datetime import datetime

from pydantic import BaseModel


class JobCreate(BaseModel):
    token_id: int
    label: str | None = None
    group_id: str
    message: str
    image_path: str | None = None
    trigger_type: str  # "date" or "cron"
    trigger_value: str


class JobUpdate(BaseModel):
    token_id: int | None = None
    label: str | None = None
    group_id: str | None = None
    message: str | None = None
    image_path: str | None = None
    trigger_type: str | None = None
    trigger_value: str | None = None
    status: str | None = None


class JobOut(BaseModel):
    id: int
    user_id: int
    token_id: int
    label: str | None
    group_id: str
    message: str
    image_path: str | None
    trigger_type: str
    trigger_value: str
    status: str
    created_at: datetime | None
    updated_at: datetime | None

    class Config:
        from_attributes = True
