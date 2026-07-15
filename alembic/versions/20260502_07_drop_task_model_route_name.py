"""drop tasks model_route_name column

Revision ID: 20260502_07
Revises: 20260502_06
Create Date: 2026-05-02 07:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260502_07"
down_revision = "20260502_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tasks", "model_route_name")


def downgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("model_route_name", sa.String(length=128), nullable=True),
    )
