"""Track the background worker without granting API write access.

Revision ID: 0001_service_heartbeats
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_service_heartbeats"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeats",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("GRANT SELECT ON alembic_version TO foxtube_api, foxtube_worker")
    op.execute("GRANT SELECT ON service_heartbeats TO foxtube_api")
    op.execute("GRANT SELECT, INSERT, UPDATE ON service_heartbeats TO foxtube_worker")


def downgrade() -> None:
    op.drop_table("service_heartbeats")
