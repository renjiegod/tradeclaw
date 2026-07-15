"""Add normalized ``trade_fills`` table for executed fill details.

Revision ID: 20260426_02
Revises: 20260426_01
Create Date: 2026-04-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260426_02"
down_revision = "20260426_01"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def _has_index(bind, table: str, name: str) -> bool:
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(ix["name"] == name for ix in insp.get_indexes(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "trade_fills"):
        op.create_table(
            "trade_fills",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.String(length=64), nullable=False),
            sa.Column("cycle_run_id", sa.String(length=80), nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=True),
            sa.Column("session_id", sa.String(length=64), nullable=True),
            sa.Column("symbol", sa.String(length=32), nullable=False),
            sa.Column("side", sa.String(length=8), nullable=False),
            sa.Column("quantity", sa.String(length=64), nullable=False),
            sa.Column("price", sa.String(length=64), nullable=False),
            sa.Column("amount", sa.String(length=64), nullable=True),
            sa.Column("fee", sa.String(length=64), nullable=True),
            sa.Column("currency", sa.String(length=16), nullable=True),
            sa.Column("intent_id", sa.String(length=128), nullable=True),
            sa.Column("intent_id_normalized", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("filled_at", sa.DateTime(), nullable=False),
            sa.Column("source_mode", sa.String(length=16), nullable=False),
            sa.Column("raw_payload", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "task_id",
                "cycle_run_id",
                "symbol",
                "side",
                "filled_at",
                "price",
                "quantity",
                "intent_id_normalized",
                name="uq_trade_fills_dedupe",
            ),
        )
    if not _has_index(bind, "trade_fills", "ix_trade_fills_task_run_symbol_time"):
        op.create_index(
            "ix_trade_fills_task_run_symbol_time",
            "trade_fills",
            ["task_id", "run_id", "symbol", "filled_at"],
            unique=False,
        )
    if not _has_index(bind, "trade_fills", "ix_trade_fills_task_cycle"):
        op.create_index(
            "ix_trade_fills_task_cycle",
            "trade_fills",
            ["task_id", "cycle_run_id"],
            unique=False,
        )
    if not _has_index(bind, "trade_fills", "ix_trade_fills_session_time"):
        op.create_index(
            "ix_trade_fills_session_time",
            "trade_fills",
            ["session_id", "filled_at"],
            unique=False,
        )

    # Drop server default so runtime must provide deterministic normalized value.
    with op.batch_alter_table("trade_fills") as batch_op:
        batch_op.alter_column("intent_id_normalized", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "trade_fills"):
        return
    for index_name in (
        "ix_trade_fills_task_run_symbol_time",
        "ix_trade_fills_task_cycle",
        "ix_trade_fills_session_time",
    ):
        if _has_index(bind, "trade_fills", index_name):
            op.drop_index(index_name, table_name="trade_fills")
    op.drop_table("trade_fills")
