"""Durable publishing records, separate from messaging jobs and WhatsApp sessions."""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base


class TikTokPost(Base):
    __tablename__ = 'tiktok_posts'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    token_id: Mapped[int | None] = mapped_column(ForeignKey('tokens.id', ondelete='SET NULL'), nullable=True, index=True)
    # The same browser submission key cannot enqueue twice.
    submission_key: Mapped[str] = mapped_column(String(36), unique=True)
    creator_username: Mapped[str] = mapped_column(String(255))
    credential_fingerprint: Mapped[str] = mapped_column(String(64))
    media_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    photo_urls: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_info: Mapped[str] = mapped_column(Text)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consent_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    publish_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
