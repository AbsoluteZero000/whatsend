from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RelayOperation(Base):
    """Durable API submissions; retain history even if a connection/key is deleted."""

    __tablename__ = "relay_operations"
    __table_args__ = (UniqueConstraint("key_fingerprint", "caller_key", name="uq_relay_operation_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    api_key_id: Mapped[int] = mapped_column(Integer, nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    token_id: Mapped[int] = mapped_column(Integer, nullable=False)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    relay_base_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    caller_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recipient: Mapped[str] = mapped_column(String(128), nullable=False)
    upstream_message_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="submitting")
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
