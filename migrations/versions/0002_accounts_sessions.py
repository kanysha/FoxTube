"""Accounts, one channel per owner, opaque sessions and shared rate limits.

Revision ID: 0002_accounts_sessions
Revises: 0001_service_heartbeats
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_accounts_sessions"
down_revision = "0001_service_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("role", sa.String(16), server_default="user", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('user', 'moderator', 'admin')", name="users_role"),
        sa.CheckConstraint("email = lower(email)", name="users_email_normalized"),
        sa.CheckConstraint("length(trim(display_name)) > 0", name="users_name_present"),
    )
    op.create_table(
        "channels",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("handle", sa.String(40), nullable=False, unique=True),
        sa.Column("title", sa.String(80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expires_at > created_at", name="sessions_positive_lifetime"
        ),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_index("ix_sessions_revoked_at", "sessions", ["revoked_at"])
    op.create_table(
        "auth_rate_limits",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempts >= 1", name="auth_rate_limits_positive_attempts"),
    )
    op.create_index(
        "ix_auth_rate_limits_expires_at", "auth_rate_limits", ["expires_at"]
    )
    op.execute(
        "GRANT SELECT ON users, channels, sessions, auth_rate_limits TO foxtube_api"
    )
    op.execute(
        "GRANT INSERT (id, email, password_hash, display_name) ON users TO foxtube_api"
    )
    op.execute("GRANT UPDATE (password_hash) ON users TO foxtube_api")
    op.execute("GRANT INSERT ON channels, sessions, auth_rate_limits TO foxtube_api")
    op.execute("GRANT UPDATE (revoked_at) ON sessions TO foxtube_api")
    op.execute("GRANT UPDATE, DELETE ON auth_rate_limits TO foxtube_api")
    op.execute("GRANT DELETE ON sessions, auth_rate_limits TO foxtube_worker")
    op.execute(
        "GRANT SELECT (id, expires_at, revoked_at) ON sessions TO foxtube_worker"
    )
    op.execute("GRANT SELECT (key, expires_at) ON auth_rate_limits TO foxtube_worker")


def downgrade() -> None:
    op.drop_table("auth_rate_limits")
    op.drop_table("sessions")
    op.drop_table("channels")
    op.drop_table("users")
