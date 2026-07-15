"""Instrument catalog (canonical symbols, sync metadata).

Revision ID: 20260419_01
Revises: 20260416_01
Create Date: 2026-04-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260419_01"
down_revision = "20260416_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instrument_catalog",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("market", sa.String(length=16), nullable=True),
        sa.Column("instrument_type", sa.String(length=64), nullable=True),
        sa.Column("is_tradable", sa.Boolean(), nullable=True),
        sa.Column("last_sync_source", sa.String(length=16), nullable=False),
        sa.Column("last_sync_at", sa.DateTime(), nullable=False),
        sa.Column("raw", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("symbol"),
    )
    op.create_index(
        "ix_instrument_catalog_display_name",
        "instrument_catalog",
        ["display_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_instrument_catalog_display_name", table_name="instrument_catalog")
    op.drop_table("instrument_catalog")
