"""add agent tool configs json

Revision ID: 20260502_02
Revises: 20260502_01
Create Date: 2026-05-02 00:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260502_02"
down_revision = "20260502_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("tool_configs_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "tool_configs_json")
