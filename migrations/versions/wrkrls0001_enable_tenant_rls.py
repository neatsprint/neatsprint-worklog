"""Add RLS policies to worklog tables.

Revision ID: wrkrls0001
Revises: 8c39a6afe8ab
"""

from __future__ import annotations

from alembic import op

revision = "wrkrls0001"
down_revision = "8c39a6afe8ab"
branch_labels = None
depends_on = None

TABLES = (
    "work_sessions",
    "idle_segments",
    "time_entry_audit",
)

POLICY = (
    "(nullif(current_setting('app.tenant_id', true), '') IS NULL OR "
    "tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)"
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({POLICY}) WITH CHECK ({POLICY})"
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
