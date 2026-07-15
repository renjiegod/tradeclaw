"""add error_type / traceback_tail columns to debug_sessions

Revision ID: 20260508_01
Revises: 20260505_02
Create Date: 2026-05-08 00:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260508_01"
down_revision = "20260505_02"
branch_labels = None
depends_on = None


def _existing_columns(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "debug_sessions")
    with op.batch_alter_table("debug_sessions") as batch_op:
        if "error_type" not in cols:
            batch_op.add_column(sa.Column("error_type", sa.String(length=255), nullable=True))
        if "traceback_tail" not in cols:
            batch_op.add_column(sa.Column("traceback_tail", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _existing_columns(bind, "debug_sessions")
    with op.batch_alter_table("debug_sessions") as batch_op:
        if "traceback_tail" in cols:
            batch_op.drop_column("traceback_tail")
        if "error_type" in cols:
            batch_op.drop_column("error_type")
