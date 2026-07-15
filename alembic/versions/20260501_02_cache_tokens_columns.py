"""add cache_read_tokens and cache_write_tokens to model_invocations

Revision ID: 20260501_02
Revises: 20260501_01
Create Date: 2026-05-01 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260501_02"
down_revision = "20260501_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_invocations", sa.Column("cache_read_tokens", sa.Integer(), nullable=True))
    op.add_column("model_invocations", sa.Column("cache_write_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_invocations", "cache_write_tokens")
    op.drop_column("model_invocations", "cache_read_tokens")
