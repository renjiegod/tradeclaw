"""watchlist_entries: single watchlist pool (collected symbols + tags).

One row per collected symbol (``symbol`` canonical ``CODE.EXCHANGE``, unique).
``tags`` is a JSON array used as the categorization mechanism (no m2m table,
following the repository convention). IDs are ``wl-<hex12>``. Backs the
``watchlist`` REST/CLI surface, the K-line sync scope, eager
``@watchlist:<tag>`` universe resolution, and the per-cycle frozen snapshot
consumed by ``ctx.dp.watchlist_symbols``. No seeding / data-copy shim
(No-backcompat): the table is created empty.

Revision ID: 20260607_01
Revises: 20260603_02
Create Date: 2026-06-07 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260607_01"
down_revision = "20260603_02"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("watchlist_entries"):
        return
    op.create_table(
        "watchlist_entries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        # JSON server_default behaves inconsistently across SQLite/Postgres,
        # so the ORM fills [] via default=list while the column stays NOT NULL.
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", name="uq_watchlist_entries_symbol"),
    )
    op.create_index(
        "ix_watchlist_entries_sort_order", "watchlist_entries", ["sort_order"], unique=False,
    )
    op.create_index(
        "ix_watchlist_entries_created_at", "watchlist_entries", ["created_at"], unique=False,
    )


def downgrade() -> None:
    if not _has_table("watchlist_entries"):
        return
    op.drop_index("ix_watchlist_entries_created_at", table_name="watchlist_entries")
    op.drop_index("ix_watchlist_entries_sort_order", table_name="watchlist_entries")
    op.drop_table("watchlist_entries")
