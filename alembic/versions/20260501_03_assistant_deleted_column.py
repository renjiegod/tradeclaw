"""add deleted column to assistant_messages and assistant_events

Revision ID: 20260501_03
Revises: 20260501_02
Create Date: 2026-05-01 23:15:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260501_03"
down_revision = "20260501_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("assistant_messages", sa.Column("deleted", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("assistant_events", sa.Column("deleted", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("assistant_events", "deleted")
    op.drop_column("assistant_messages", "deleted")
