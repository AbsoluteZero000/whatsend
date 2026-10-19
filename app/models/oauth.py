from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OAuthClient(Base):
    """An app registered through OAuth dynamic client registration, e.g. Claude."""
    __tablename__ = "oauth_clients"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    redirect_uris: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list
    auth_method: Mapped[str] = mapped_column(String(30), default="none", nullable=False)
    secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class OAuthGrant(Base):
    """One approval of an app by a user ("connected app"). Holds the pending code, then the tokens.

    Only SHA-256 hashes of codes and tokens are stored. Deleting the row disconnects the app.
    """
    __tablename__ = "oauth_grants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(String(64), ForeignKey("oauth_clients.id", ondelete="CASCADE"), nullable=False)
    client_name: Mapped[str] = mapped_column(String(100), nullable=False)
    scope: Mapped[str] = mapped_column(String(100), default="messages", nullable=False)
    resource: Mapped[str | None] = mapped_column(String(512), nullable=True)
    redirect_uri: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    code_challenge: Mapped[str | None] = mapped_column(String(128), nullable=True)
    code_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    access_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refresh_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="oauth_grants")
