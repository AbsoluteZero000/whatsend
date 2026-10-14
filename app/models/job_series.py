from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


class SeriesEntry(Base):
    __tablename__ = "series_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    job: Mapped["Job"] = relationship("Job", back_populates="series_entries")
    attachments: Mapped[list["SeriesAttachment"]] = relationship("SeriesAttachment", back_populates="entry", cascade="all, delete-orphan", order_by="SeriesAttachment.position")


class SeriesAttachment(Base):
    __tablename__ = "series_attachments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    entry_id: Mapped[int | None] = mapped_column(ForeignKey("series_entries.id"), nullable=True, index=True)
    path: Mapped[str] = mapped_column(String(512))
    position: Mapped[int] = mapped_column(Integer, default=0)
    filename: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    entry: Mapped["SeriesEntry | None"] = relationship("SeriesEntry", back_populates="attachments")
