"""Single-use password recovery credentials and bounded worker cleanup.

Revision ID: 0006_account_security
Revises: 0005_moderation
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_account_security"
down_revision = "0005_moderation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "expires_at > created_at", name="password_reset_positive_lifetime"
        ),
    )
    op.create_index(
        "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
    )
    op.create_index(
        "ix_password_reset_tokens_expires_at", "password_reset_tokens", ["expires_at"]
    )
    op.execute("GRANT SELECT, INSERT ON password_reset_tokens TO foxtube_api")
    op.execute("GRANT UPDATE (used_at) ON password_reset_tokens TO foxtube_api")
    op.execute("GRANT DELETE ON password_reset_tokens TO foxtube_worker")
    op.execute(
        "GRANT SELECT (id, expires_at, used_at) "
        "ON password_reset_tokens TO foxtube_worker"
    )


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
