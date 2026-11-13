"""Reject incomplete READY metadata, including SQL NULL values.

Revision ID: 0007_ready_metadata
Revises: 0006_account_security
"""

from alembic import op

revision = "0007_ready_metadata"
down_revision = "0006_account_security"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("videos_ready_artifacts", "videos", type_="check")
    op.create_check_constraint(
        "videos_ready_artifacts",
        "videos",
        "status <> 'READY' OR (playback_key IS NOT NULL AND "
        "poster_key IS NOT NULL AND duration IS NOT NULL AND duration > 0 AND "
        "width IS NOT NULL AND width > 0 AND height IS NOT NULL AND height > 0)",
    )


def downgrade() -> None:
    op.drop_constraint("videos_ready_artifacts", "videos", type_="check")
    op.create_check_constraint(
        "videos_ready_artifacts",
        "videos",
        "status <> 'READY' OR (playback_key IS NOT NULL AND "
        "poster_key IS NOT NULL AND duration > 0 AND width > 0 AND height > 0)",
    )
