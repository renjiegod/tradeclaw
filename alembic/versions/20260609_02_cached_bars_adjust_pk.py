"""Support per-call adjust mode (none/qfq/hfq) for cached bars.

Changes:
- cached_bars: add adjust column to PK, change default from "qfq" to "none"
- cached_bar_ranges: add adjust column and index, default "none"

This aligns data source defaults with user-visible charts (不复权) and
avoids 复权断崖 misleading technical indicators like SMA crossovers.

Revision ID: 20260609_02
Revises: 20260609_01
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260609_02"
down_revision = "20260609_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        # SQLite doesn't support ALTER TABLE ... DROP PRIMARY KEY directly.
        # Re-create tables with new schema and migrate data.

        # Step 1: Create new tables with adjust as part of PK
        op.execute(
            """
            CREATE TABLE cached_bars_new (
                provider TEXT NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                adjust TEXT NOT NULL DEFAULT 'none',
                bar_timestamp TEXT NOT NULL,
                open_price REAL NOT NULL,
                high_price REAL NOT NULL,
                low_price REAL NOT NULL,
                close_price REAL NOT NULL,
                volume REAL NOT NULL,
                amount REAL,
                fetched_at DATETIME NOT NULL,
                PRIMARY KEY (provider, symbol, interval, adjust, bar_timestamp)
            )
            """
        )
        op.execute(
            """
            CREATE TABLE cached_bar_ranges_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                adjust TEXT NOT NULL DEFAULT 'none',
                range_start TEXT NOT NULL,
                range_end TEXT NOT NULL,
                fetched_at DATETIME NOT NULL
            )
            """
        )

        # Step 2: Migrate existing data (default adjust to "none" for legacy rows)
        # Drop old tables to avoid conflicts
        op.drop_index("ix_cached_bars_range_lookup", table_name="cached_bars")
        op.drop_index("ix_cached_bar_ranges_lookup", table_name="cached_bar_ranges")

        # Copy existing bars with adjust='none' (legacy qfq data is discarded)
        op.execute(
            """
            INSERT INTO cached_bars_new (
                provider, symbol, interval, adjust, bar_timestamp,
                open_price, high_price, low_price, close_price, volume, amount, fetched_at
            )
            SELECT provider, symbol, interval, 'none', bar_timestamp,
                   open_price, high_price, low_price, close_price, volume, amount, fetched_at
            FROM cached_bars
            """
        )

        # Copy existing ranges with adjust='none'
        op.execute(
            """
            INSERT INTO cached_bar_ranges_new (
                provider, symbol, interval, adjust, range_start, range_end, fetched_at
            )
            SELECT provider, symbol, interval, 'none', range_start, range_end, fetched_at
            FROM cached_bar_ranges
            """
        )

        # Step 3: Drop old tables and rename new ones
        op.drop_table("cached_bars")
        op.drop_table("cached_bar_ranges")
        op.execute("ALTER TABLE cached_bars_new RENAME TO cached_bars")
        op.execute("ALTER TABLE cached_bar_ranges_new RENAME TO cached_bar_ranges")

        # Step 4: Recreate indexes
        op.create_index(
            "ix_cached_bars_range_lookup",
            "cached_bars",
            ["provider", "symbol", "interval", "adjust", "bar_timestamp"],
            unique=False,
        )
        op.create_index(
            "ix_cached_bar_ranges_lookup",
            "cached_bar_ranges",
            ["provider", "symbol", "interval", "adjust"],
            unique=False,
        )
        return

    if dialect == "postgresql":
        # PostgreSQL supports ALTER PRIMARY KEY more directly

        # Step 1: Add adjust column to cached_bar_ranges
        op.add_column(
            "cached_bar_ranges",
            sa.Column("adjust", sa.String(length=16), nullable=False, server_default="none"),
        )

        # Step 2: Backfill existing rows with adjust='none'
        op.execute(
            sa.text("UPDATE cached_bar_ranges SET adjust = 'none' WHERE adjust IS NULL")
        )

        # Step 3: Create new index with adjust
        op.create_index(
            "ix_cached_bar_ranges_lookup_new",
            "cached_bar_ranges",
            ["provider", "symbol", "interval", "adjust"],
            unique=False,
        )

        # Step 4: Drop old index
        op.drop_index("ix_cached_bar_ranges_lookup", table_name="cached_bar_ranges")
        op.execute("ALTER INDEX ix_cached_bar_ranges_lookup_new RENAME TO ix_cached_bar_ranges_lookup")

        # Step 5: Make adjust column NOT NULL
        op.alter_column(
            "cached_bar_ranges",
            "adjust",
            nullable=False,
            server_default="none",
        )

        # Step 6: cached_bars table - need to recreate with adjust in PK
        # First, create new table
        op.execute(
            """
            CREATE TABLE cached_bars_new (
                provider VARCHAR(16) NOT NULL,
                symbol VARCHAR(32) NOT NULL,
                interval VARCHAR(16) NOT NULL,
                adjust VARCHAR(16) NOT NULL DEFAULT 'none',
                bar_timestamp VARCHAR(32) NOT NULL,
                open_price FLOAT NOT NULL,
                high_price FLOAT NOT NULL,
                low_price FLOAT NOT NULL,
                close_price FLOAT NOT NULL,
                volume FLOAT NOT NULL,
                amount FLOAT,
                fetched_at TIMESTAMP NOT NULL,
                PRIMARY KEY (provider, symbol, interval, adjust, bar_timestamp)
            )
            """
        )

        # Migrate data (legacy qfq → none)
        op.execute(
            """
            INSERT INTO cached_bars_new (
                provider, symbol, interval, adjust, bar_timestamp,
                open_price, high_price, low_price, close_price, volume, amount, fetched_at
            )
            SELECT provider, symbol, interval, 'none', bar_timestamp,
                   open_price, high_price, low_price, close_price, volume, amount, fetched_at
            FROM cached_bars
            """
        )

        # Drop old table and rename
        op.drop_table("cached_bars")
        op.execute("ALTER TABLE cached_bars_new RENAME TO cached_bars")

        # Recreate index
        op.create_index(
            "ix_cached_bars_range_lookup",
            "cached_bars",
            ["provider", "symbol", "interval", "adjust", "bar_timestamp"],
        )
        return

    raise RuntimeError(f"unsupported database dialect for 20260609_02: {dialect}")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        # Recreate tables without adjust in PK
        op.execute(
            """
            CREATE TABLE cached_bars_old (
                provider TEXT NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                bar_timestamp TEXT NOT NULL,
                adjust TEXT NOT NULL DEFAULT 'qfq',
                open_price REAL NOT NULL,
                high_price REAL NOT NULL,
                low_price REAL NOT NULL,
                close_price REAL NOT NULL,
                volume REAL NOT NULL,
                amount REAL,
                fetched_at DATETIME NOT NULL,
                PRIMARY KEY (provider, symbol, interval, bar_timestamp)
            )
            """
        )
        op.execute(
            """
            CREATE TABLE cached_bar_ranges_old (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                range_start TEXT NOT NULL,
                range_end TEXT NOT NULL,
                fetched_at DATETIME NOT NULL
            )
            """
        )

        op.drop_index("ix_cached_bars_range_lookup", table_name="cached_bars")
        op.drop_index("ix_cached_bar_ranges_lookup", table_name="cached_bar_ranges")

        # Migrate data (none → qfq)
        op.execute(
            """
            INSERT INTO cached_bars_old (
                provider, symbol, interval, bar_timestamp, adjust,
                open_price, high_price, low_price, close_price, volume, amount, fetched_at
            )
            SELECT provider, symbol, interval, bar_timestamp, 'qfq',
                   open_price, high_price, low_price, close_price, volume, amount, fetched_at
            FROM cached_bars
            """
        )
        op.execute(
            """
            INSERT INTO cached_bar_ranges_old (
                provider, symbol, interval, range_start, range_end, fetched_at
            )
            SELECT provider, symbol, interval, range_start, range_end, fetched_at
            FROM cached_bar_ranges
            """
        )

        op.drop_table("cached_bars")
        op.drop_table("cached_bar_ranges")
        op.execute("ALTER TABLE cached_bars_old RENAME TO cached_bars")
        op.execute("ALTER TABLE cached_bar_ranges_old RENAME TO cached_bar_ranges")

        op.create_index(
            "ix_cached_bars_range_lookup",
            "cached_bars",
            ["provider", "symbol", "interval", "bar_timestamp"],
            unique=False,
        )
        op.create_index(
            "ix_cached_bar_ranges_lookup",
            "cached_bar_ranges",
            ["provider", "symbol", "interval"],
            unique=False,
        )
        return

    if dialect == "postgresql":
        # Recreate cached_bars without adjust in PK
        op.execute(
            """
            CREATE TABLE cached_bars_old (
                provider VARCHAR(16) NOT NULL,
                symbol VARCHAR(32) NOT NULL,
                interval VARCHAR(16) NOT NULL,
                bar_timestamp VARCHAR(32) NOT NULL,
                adjust VARCHAR(16) NOT NULL DEFAULT 'qfq',
                open_price FLOAT NOT NULL,
                high_price FLOAT NOT NULL,
                low_price FLOAT NOT NULL,
                close_price FLOAT NOT NULL,
                volume FLOAT NOT NULL,
                amount FLOAT,
                fetched_at TIMESTAMP NOT NULL,
                PRIMARY KEY (provider, symbol, interval, bar_timestamp)
            )
            """
        )

        op.drop_index("ix_cached_bars_range_lookup", table_name="cached_bars")

        # Migrate data
        op.execute(
            """
            INSERT INTO cached_bars_old (
                provider, symbol, interval, bar_timestamp, adjust,
                open_price, high_price, low_price, close_price, volume, amount, fetched_at
            )
            SELECT provider, symbol, interval, bar_timestamp, 'qfq',
                   open_price, high_price, low_price, close_price, volume, amount, fetched_at
            FROM cached_bars
            """
        )

        op.drop_table("cached_bars")
        op.execute("ALTER TABLE cached_bars_old RENAME TO cached_bars")

        op.create_index(
            "ix_cached_bars_range_lookup",
            "cached_bars",
            ["provider", "symbol", "interval", "bar_timestamp"],
        )

        # Drop adjust column from cached_bar_ranges
        op.drop_index("ix_cached_bar_ranges_lookup", table_name="cached_bar_ranges")
        op.drop_column("cached_bar_ranges", "adjust")
        op.create_index(
            "ix_cached_bar_ranges_lookup",
            "cached_bar_ranges",
            ["provider", "symbol", "interval"],
            unique=False,
        )
        return

    raise RuntimeError(f"unsupported database dialect for 20260609_02 downgrade: {dialect}")