"""Add ``tasks.backtest_summary`` JSON column.

Backtest tasks persist their finalized summary (return / equity curve / FIFO
trade stats / max drawdown / final positions) here. Pre-existing rows keep
``backtest_summary = NULL``; the detail page falls back to the running-state
view when the column is empty.

Revision ID: 20260426_01
Revises: 20260422_03
Create Date: 2026-04-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260426_01"
down_revision = "20260422_03"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "tasks"):
        return
    if _has_column(bind, "tasks", "backtest_summary"):
        return
    op.add_column("tasks", sa.Column("backtest_summary", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "tasks"):
        return
    if not _has_column(bind, "tasks", "backtest_summary"):
        return
    op.drop_column("tasks", "backtest_summary")
