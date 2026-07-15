"""add agent system prompt template reference

Revision ID: 20260502_05
Revises: 20260502_04
Create Date: 2026-05-02 21:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260502_05"
down_revision = "20260502_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("system_prompt_template_id", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agents", "system_prompt_template_id")
