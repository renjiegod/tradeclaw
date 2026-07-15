"""drop tasks.template_id, orchestrator_mode, watch_symbols

Revision ID: 20260502_06
Revises: 20260502_05
Create Date: 2026-05-02 21:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260502_06"
down_revision = "20260502_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tasks", "template_id")
    op.drop_column("tasks", "orchestrator_mode")
    op.drop_column("tasks", "watch_symbols")


def downgrade() -> None:
    op.add_column(
        "tasks",
        # Was non-nullable with no default; reintroduce as nullable for downgrade safety.
        # Production data already existed without template_id after upgrade.
        sa.Column("template_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("orchestrator_mode", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("watch_symbols", sa.JSON(), nullable=True),
    )
