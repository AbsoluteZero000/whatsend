from datetime import datetime

from pydantic import BaseModel


class LogOut(BaseModel):
    id: int
    job_id: int
    status: str
    response: str | None
    sent_at: datetime | None

    class Config:
        from_attributes = True
