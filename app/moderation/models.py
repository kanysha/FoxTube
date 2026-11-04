from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'resolved')", name="reports_status"),
        CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 1000", name="reports_reason"
        ),
        CheckConstraint(
            "(status = 'open' AND resolution IS NULL AND resolved_at IS NULL "
            "AND resolved_by IS NULL) OR (status = 'resolved' "
            "AND resolution IS NOT NULL "
            "AND length(trim(resolution)) BETWEEN 1 AND 1000 "
            "AND resolved_at IS NOT NULL)",
            name="reports_resolution",
        ),
        Index("ix_reports_status_created", "status", "created_at", "id"),
        Index("ix_reports_video_id", "video_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    video_id: Mapped[UUID] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    reporter_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index(
    "uq_reports_open_video_reporter",
    Report.video_id,
    Report.reporter_id,
    unique=True,
    postgresql_where=Report.status == "open",
)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('report.created', 'report.resolved', 'video.hidden', "
            "'video.restored', 'role.changed', 'role.bootstrap')",
            name="audit_events_action",
        ),
        CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 1000", name="audit_events_reason"
        ),
        Index("ix_audit_events_created", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(32))
    # Keep identifiers after deletion without retaining email or private storage paths.
    video_id: Mapped[UUID | None] = mapped_column()
    report_id: Mapped[UUID | None] = mapped_column()
    target_user_id: Mapped[UUID | None] = mapped_column()
    reason: Mapped[str] = mapped_column(Text)
    previous_value: Mapped[str | None] = mapped_column(String(32))
    new_value: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
