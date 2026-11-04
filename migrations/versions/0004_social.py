"""Persistent likes, comments, subscriptions and playlists.

Revision ID: 0004_social
Revises: 0003_video_pipeline
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_social"
down_revision = "0003_video_pipeline"
branch_labels = None
depends_on = None


def created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )


def upgrade() -> None:
    op.create_table(
        "likes",
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
        created_at(),
    )
    op.create_index("ix_likes_user", "likes", ["user_id"])
    op.create_table(
        "comments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "video_id",
            sa.Uuid(),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        created_at(),
        sa.CheckConstraint(
            "length(trim(body)) BETWEEN 1 AND 2000", name="comments_body"
        ),
    )
    op.create_index(
        "ix_comments_video_created", "comments", ["video_id", "created_at", "id"]
    )
    op.create_index("ix_comments_user", "comments", ["user_id"])
    op.create_table(
        "subscriptions",
        sa.Column(
            "channel_id",
            sa.Uuid(),
            sa.ForeignKey("channels.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        created_at(),
    )
    op.create_index(
        "ix_subscriptions_user_created", "subscriptions", ["user_id", "created_at"]
    )
    op.create_table(
        "playlists",
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
        created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("length(trim(title)) > 0", name="playlists_title"),
        sa.CheckConstraint("length(description) <= 2000", name="playlists_description"),
        sa.CheckConstraint(
            "visibility IN ('public', 'private')", name="playlists_visibility"
        ),
    )
    op.create_index(
        "ix_playlists_owner_created", "playlists", ["owner_id", "created_at", "id"]
    )
    op.create_table(
        "playlist_items",
        sa.Column(
            "playlist_id",
            sa.Uuid(),
            sa.ForeignKey("playlists.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "video_id",
            sa.Uuid(),
            sa.ForeignKey("videos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        created_at(),
    )
    op.create_index(
        "ix_playlist_items_created",
        "playlist_items",
        ["playlist_id", "created_at", "video_id"],
    )
    op.create_index("ix_playlist_items_video", "playlist_items", ["video_id"])
    op.execute(
        "GRANT SELECT, INSERT, DELETE ON likes, comments, subscriptions, "
        "playlists, playlist_items TO foxtube_api"
    )
    op.execute(
        "GRANT UPDATE (title, description, visibility, updated_at) "
        "ON playlists TO foxtube_api"
    )


def downgrade() -> None:
    for table in ("playlist_items", "playlists", "subscriptions", "comments", "likes"):
        op.drop_table(table)
