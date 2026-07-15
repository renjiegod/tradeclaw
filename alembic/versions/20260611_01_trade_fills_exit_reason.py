"""Add trade_fills.exit_reason column.

SELL-only exit categorization (strategy_sdk.signal.ExitReason: signal /
stop_loss / take_profit / trailing_stop / roi / circuit_breaker) copied from
``OrderIntent.exit_reason``. Nullable and additive — existing rows stay NULL,
so no backfill is required and historic analytics are unchanged.

Revision ID: 20260611_01
Revises: 20260609_04
Create Date: 2026-06-11 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260611_01"
down_revision = "20260609_04"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    if not inspect(bind).has_table(table):
        return False
    return any(col["name"] == column for col in inspect(bind).get_columns(table))


def upgrade() -> None:
    if not _has_column("trade_fills", "exit_reason"):
        op.add_column(
            "trade_fills",
            sa.Column("exit_reason", sa.String(length=32), nullable=True),
        )


def downgrade() -> None:
    if _has_column("trade_fills", "exit_reason"):
        op.drop_column("trade_fills", "exit_reason")
