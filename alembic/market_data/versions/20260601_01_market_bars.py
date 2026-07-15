"""Create market bars storage (TimescaleDB hypertable on PostgreSQL, plain table on SQLite).

Revision ID: 20260601_01_market_bars
Revises:
Create Date: 2026-06-01 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260601_01_market_bars"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.create_table(
        "market_bars",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("adjust", sa.String(length=16), nullable=False),
        sa.Column("bar_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open_price", sa.Float(), nullable=False),
        sa.Column("high_price", sa.Float(), nullable=False),
        sa.Column("low_price", sa.Float(), nullable=False),
        sa.Column("close_price", sa.Float(), nullable=False),
        sa.Column("volume", sa.Float(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=True),
        sa.Column("source_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "symbol",
            "interval",
            "provider",
            "adjust",
            "bar_time",
            name="pk_market_bars",
        ),
    )
    if dialect == "postgresql":
        op.execute(
            """
            SELECT create_hypertable(
                'market_bars',
                'bar_time',
                if_not_exists => TRUE
            )
            """
        )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_market_bars_symbol_interval_adjust_time
        ON market_bars (symbol, interval, adjust, bar_time DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_market_bars_provider_symbol_interval_adjust_time
        ON market_bars (provider, symbol, interval, adjust, bar_time DESC)
        """
    )
    op.create_table(
        "market_bar_sync_state",
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("interval", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("adjust", sa.String(length=16), nullable=False),
        sa.Column("target_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("covered_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("covered_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_error_type", sa.String(length=128), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "interval", "provider", "adjust"),
    )


def downgrade() -> None:
    op.drop_table("market_bar_sync_state")
    op.execute(
        "DROP INDEX IF EXISTS ix_market_bars_provider_symbol_interval_adjust_time"
    )
    op.execute("DROP INDEX IF EXISTS ix_market_bars_symbol_interval_adjust_time")
    op.drop_table("market_bars")
