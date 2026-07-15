"""Persisted trading-day suspensions for the bar cache.

Introduces ``cached_bar_suspensions`` backing
:class:`doyoutrade.persistence.models.CachedBarSuspensionRecord`.

A halted trading day (baostock ``tradestatus==0`` / blank volume) produces no
tradeable bar, so ``cached_bars`` deliberately stores nothing for it. But the
backtest mark overlay (``merge_simulated_bar_marks_into_market``) needs to tell
a genuine halt apart from a plain data gap: a halt must block a buy
(``symbol_suspended``) while a gap should carry the last close forward and let
the buy price. The only authoritative signal is baostock's per-day
``tradestatus``, which is dropped at fetch time and lives only on the transient
``BaostockDataProvider.last_suspended_days`` attribute — gone on a warm cache.
Persisting it next to the bars it explains keeps the distinction available on
cache hits.

Keyed identically to ``cached_bars`` (``provider`` + ``adjust`` in the PK) so a
halt recorded under one source / 复权 mode never leaks into another's lookups.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260616_01"
down_revision = "20260615_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cached_bar_suspensions",
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("adjust", sa.String(length=16), nullable=False, server_default="qfq"),
        sa.Column("suspended_day", sa.String(length=16), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint(
            "provider", "symbol", "interval", "adjust", "suspended_day"
        ),
    )
    op.create_index(
        "ix_cached_bar_suspensions_lookup",
        "cached_bar_suspensions",
        ["provider", "symbol", "interval", "adjust", "suspended_day"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cached_bar_suspensions_lookup", table_name="cached_bar_suspensions"
    )
    op.drop_table("cached_bar_suspensions")
