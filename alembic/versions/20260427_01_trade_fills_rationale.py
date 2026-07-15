"""Add rationale column to trade_fills for execution reasons.

Revision ID: 20260427_01
Revises: 20260426_02
Create Date: 2026-04-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260427_01"
down_revision = "20260426_02"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def _has_column(bind, table: str, name: str) -> bool:
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(col["name"] == name for col in insp.get_columns(table))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "trade_fills"):
        return
    if _has_column(bind, "trade_fills", "rationale"):
        return
    with op.batch_alter_table("trade_fills") as batch_op:
        batch_op.add_column(sa.Column("rationale", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "trade_fills"):
        return
    if not _has_column(bind, "trade_fills", "rationale"):
        return
    with op.batch_alter_table("trade_fills") as batch_op:
        batch_op.drop_column("rationale")
