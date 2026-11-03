"""Videos, private grants and leased PostgreSQL processing jobs.

Revision ID: 0003_video_pipeline
Revises: 0002_accounts_sessions
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_video_pipeline"
down_revision = "0002_accounts_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "videos",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "visibility", sa.String(16), server_default="private", nullable=False
        ),
        sa.Column(
            "moderation_status", sa.String(16), server_default="visible", nullable=False
        ),
        sa.Column("status", sa.String(16), server_default="UPLOADING", nullable=False),
        sa.Column("expected_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "reserved_bytes", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("source_key", sa.String(240)),
        sa.Column("playback_key", sa.String(240)),
        sa.Column("poster_key", sa.String(240)),
        sa.Column("upload_token", sa.Uuid()),
        sa.Column("upload_expires_at", sa.DateTime(timezone=True)),
        sa.Column("reservation_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("duration", sa.Float()),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("error", sa.String(300)),
        sa.CheckConstraint(
            "visibility IN ('public', 'private')", name="videos_visibility"
        ),
        sa.CheckConstraint(
            "moderation_status IN ('visible', 'hidden')", name="videos_moderation"
        ),
        sa.CheckConstraint(
            "status IN ('UPLOADING', 'QUEUED', 'PROCESSING', 'READY', 'FAILED')",
            name="videos_status",
        ),
        sa.CheckConstraint("length(trim(title)) > 0", name="videos_title_present"),
        sa.CheckConstraint(
            "expected_size BETWEEN 1 AND 524288000 AND "
            "storage_bytes >= 0 AND reserved_bytes >= 0",
            name="videos_sizes",
        ),
        sa.CheckConstraint(
            "status <> 'READY' OR (playback_key IS NOT NULL AND "
            "poster_key IS NOT NULL AND duration > 0 AND width > 0 AND height > 0)",
            name="videos_ready_artifacts",
        ),
    )
    op.create_index("ix_videos_owner_created", "videos", ["owner_id", "created_at"])
    op.create_index("ix_videos_feed", "videos", ["created_at", "id"])
    op.create_index(
        "ix_videos_title_trgm",
        "videos",
        ["title"],
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_table(
        "video_jobs",
        sa.Column(
            "video_id",
            sa.Uuid(),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("status", sa.String(16), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed')",
            name="video_jobs_status",
        ),
        sa.CheckConstraint("attempts BETWEEN 0 AND 3", name="video_jobs_attempts"),
    )
    op.create_index(
        "ix_video_jobs_claim",
        "video_jobs",
        ["status", "available_at", "lease_expires_at"],
    )
    op.create_table(
        "video_grants",
        sa.Column(
            "video_id",
            sa.Uuid(),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.execute(
        "GRANT SELECT, INSERT ON videos, video_jobs, video_grants TO foxtube_api"
    )
    op.execute("GRANT DELETE ON video_grants TO foxtube_api")
    op.execute(
        "GRANT UPDATE (title, description, visibility, status, storage_bytes, "
        "reserved_bytes, source_key, upload_token, upload_expires_at, "
        "reservation_expires_at, updated_at, "
        "deleted_at, error) ON videos TO foxtube_api"
    )
    op.execute("GRANT SELECT ON videos, video_jobs TO foxtube_worker")
    op.execute("GRANT UPDATE ON video_jobs TO foxtube_worker")
    op.execute(
        "GRANT UPDATE (status, storage_bytes, reserved_bytes, source_key, "
        "playback_key, poster_key, upload_token, upload_expires_at, updated_at, "
        "duration, width, height, error) ON videos TO foxtube_worker"
    )


def downgrade() -> None:
    op.drop_table("video_grants")
    op.drop_table("video_jobs")
    op.drop_table("videos")
    # pg_trgm may be used by another schema; do not remove a shared extension.
