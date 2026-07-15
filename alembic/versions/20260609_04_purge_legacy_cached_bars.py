"""Purge legacy cached_bars poisoned by qfq→none relabel; drop data_cleaned columns.

Revision ID: 20260609_04
Revises: 20260609_03
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260609_04"
down_revision = "20260609_03"
branch_labels = None
depends_on = None


def _column_names(table: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    op.execute("DELETE FROM cached_bars")
    op.execute("DELETE FROM cached_bar_ranges")

    if "data_cleaned" in _column_names("cached_bars"):
        op.drop_column("cached_bars", "data_cleaned")
    if "data_cleaned" in _column_names("cached_bar_ranges"):
        op.drop_column("cached_bar_ranges", "data_cleaned")


def downgrade() -> None:
  pass
