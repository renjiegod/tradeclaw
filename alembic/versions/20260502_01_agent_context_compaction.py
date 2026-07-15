"""add agent context compaction config

Revision ID: 20260502_01
Revises: 20260501_03
Create Date: 2026-05-02 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260502_01"
down_revision = "20260501_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("context_compaction_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "context_compaction_json")
