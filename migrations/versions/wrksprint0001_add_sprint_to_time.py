"""Record the sprint on work sessions and time entries.

Revision ID: wrksprint0001
Revises: wrkrls0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "wrksprint0001"
down_revision = "wrkrls0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "work_sessions",
        sa.Column("sprint_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "time_entries",
        sa.Column("sprint_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("time_entries", "sprint_id")
    op.drop_column("work_sessions", "sprint_id")
