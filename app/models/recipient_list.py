from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RecipientList(Base):
    __tablename__ = "recipient_lists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_id: Mapped[int] = mapped_column(Integer, ForeignKey("tokens.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    recipients: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
