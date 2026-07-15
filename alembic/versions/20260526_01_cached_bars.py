"""Persistent OHLCV bar cache.

Introduces two tables backing :class:`doyoutrade.data.cached_bars.CachedBarsDataProvider`'s
move from a process-local ``dict`` to a cross-run / cross-process cache:

* ``cached_bars`` — one row per OHLCV bar, keyed by
  ``(provider, symbol, interval, bar_timestamp)``. ``provider`` is part of
  the PK to prevent cross-source contamination (e.g. akshare-qfq rows being
  served when the caller asked for tushare-qfq).
* ``cached_bar_ranges`` — records which ``(start, end)`` windows have
  already been fetched from upstream. An empty range (weekend, holiday
  block, delisted symbol) is itself a valid cache hit; a bar-only schema
  would re-hit upstream forever for those windows.

Per ``project_no_backcompat_phase`` the previous in-memory ``_cache`` is
dropped outright — there's no on-disk data to migrate.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260526_01"
down_revision = "20260525_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cached_bars",
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("bar_timestamp", sa.String(length=32), nullable=False),
        sa.Column("adjust", sa.String(length=16), nullable=False, server_default="qfq"),
        sa.Column("open_price", sa.Float(), nullable=False),
        sa.Column("high_price", sa.Float(), nullable=False),
        sa.Column("low_price", sa.Float(), nullable=False),
        sa.Column("close_price", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("provider", "symbol", "interval", "bar_timestamp"),
    )
    op.create_index(
        "ix_cached_bars_range_lookup",
        "cached_bars",
        ["provider", "symbol", "interval", "bar_timestamp"],
        unique=False,
    )
    op.create_table(
        "cached_bar_ranges",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("range_start", sa.String(length=32), nullable=False),
        sa.Column("range_end", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cached_bar_ranges_lookup",
        "cached_bar_ranges",
        ["provider", "symbol", "interval"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cached_bar_ranges_lookup", table_name="cached_bar_ranges")
    op.drop_table("cached_bar_ranges")
    op.drop_index("ix_cached_bars_range_lookup", table_name="cached_bars")
    op.drop_table("cached_bars")
