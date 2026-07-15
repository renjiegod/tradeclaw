"""trade_fills: add entry_tag / exit_tag columns for new Strategy API.

Part of the SignalEngine → Strategy refactor. Strategies now emit
``Signal.buy(tag="ma_cross+rsi_ok")`` etc.; the runner copies the tag
onto the corresponding OrderIntent, and the execution layer persists it
onto each TradeFillRecord here so trade analytics can group fills by
which factor combination triggered the entry / exit.

Both columns are nullable: pre-existing fills predate the new API and
won't have a tag. No backfill — historical analytics should treat NULL
as "untagged (legacy SignalEngine era)".

Indexes are added so a "fills by entry_tag" query (common during factor
attribution) doesn't full-scan trade_fills.

Revision ID: 20260522_01
Revises: 20260517_01
Create Date: 2026-05-22 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260522_01"
down_revision = "20260517_01"
branch_labels = None
depends_on = None


def _existing_columns(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def _existing_indexes(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {idx["name"] for idx in insp.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "trade_fills")
    with op.batch_alter_table("trade_fills") as batch_op:
        if "entry_tag" not in cols:
            batch_op.add_column(sa.Column("entry_tag", sa.String(length=255), nullable=True))
        if "exit_tag" not in cols:
            batch_op.add_column(sa.Column("exit_tag", sa.String(length=255), nullable=True))

    idx_names = _existing_indexes(bind, "trade_fills")
    if "ix_trade_fills_entry_tag" not in idx_names:
        op.create_index("ix_trade_fills_entry_tag", "trade_fills", ["entry_tag"])
    if "ix_trade_fills_exit_tag" not in idx_names:
        op.create_index("ix_trade_fills_exit_tag", "trade_fills", ["exit_tag"])


def downgrade() -> None:
    bind = op.get_bind()
    idx_names = _existing_indexes(bind, "trade_fills")
    if "ix_trade_fills_exit_tag" in idx_names:
        op.drop_index("ix_trade_fills_exit_tag", table_name="trade_fills")
    if "ix_trade_fills_entry_tag" in idx_names:
        op.drop_index("ix_trade_fills_entry_tag", table_name="trade_fills")

    cols = _existing_columns(bind, "trade_fills")
    with op.batch_alter_table("trade_fills") as batch_op:
        if "exit_tag" in cols:
            batch_op.drop_column("exit_tag")
        if "entry_tag" in cols:
            batch_op.drop_column("entry_tag")
