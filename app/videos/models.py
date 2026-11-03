from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('public', 'private')", name="videos_visibility"
        ),
        CheckConstraint(
            "moderation_status IN ('visible', 'hidden')", name="videos_moderation"
        ),
        CheckConstraint(
            "status IN ('UPLOADING', 'QUEUED', 'PROCESSING', 'READY', 'FAILED')",
            name="videos_status",
        ),
        CheckConstraint("length(trim(title)) > 0", name="videos_title_present"),
        CheckConstraint(
            "expected_size BETWEEN 1 AND 524288000 AND "
            "storage_bytes >= 0 AND reserved_bytes >= 0",
            name="videos_sizes",
        ),
        CheckConstraint(
            "status <> 'READY' OR (playback_key IS NOT NULL AND "
            "poster_key IS NOT NULL AND duration > 0 AND width > 0 AND height > 0)",
            name="videos_ready_artifacts",
        ),
        Index("ix_videos_owner_created", "owner_id", "created_at"),
        Index("ix_videos_feed", "created_at", "id"),
        Index(
            "ix_videos_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, server_default="")
    visibility: Mapped[str] = mapped_column(String(16), server_default="private")
    moderation_status: Mapped[str] = mapped_column(String(16), server_default="visible")
    status: Mapped[str] = mapped_column(String(16), server_default="UPLOADING")
    expected_size: Mapped[int] = mapped_column(BigInteger)
    storage_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    source_key: Mapped[str | None] = mapped_column(String(240))
    playback_key: Mapped[str | None] = mapped_column(String(240))
    poster_key: Mapped[str | None] = mapped_column(String(240))
    upload_token: Mapped[UUID | None]
    upload_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reservation_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None]
    height: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(String(300))


class VideoJob(Base):
    __tablename__ = "video_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')",
            name="video_jobs_status",
        ),
        CheckConstraint("attempts BETWEEN 0 AND 3", name="video_jobs_attempts"),
        Index("ix_video_jobs_claim", "status", "available_at", "lease_expires_at"),
    )

    video_id: Mapped[UUID] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), server_default="queued")
    attempts: Mapped[int] = mapped_column(server_default="0")
    attempt_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class VideoGrant(Base):
    __tablename__ = "video_grants"

    video_id: Mapped[UUID] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
