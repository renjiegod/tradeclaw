"""Add quoted ``mode`` column to ``runs`` (backtest / paper / live).

PostgreSQL treats ``mode`` as a special identifier in some contexts; the ORM
uses a quoted column name. Older databases may have ``runs`` without this
column.

This migration predates the ``backtest_jobs`` -> ``runs`` rename in revision
``20260422_03`` in the revision history (id ``20260422_01``), so when a fresh
database is first brought up the ``runs`` table does not yet exist. We make
this step a no-op in that case; revision ``20260422_03`` is responsible for
adding the column on the renamed table and will itself skip when the column
is already present.

Revision ID: 20260422_01
Revises: 20260421_01
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260422_01"
down_revision = "20260421_01"
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
    if bind.dialect.name != "postgresql":
        return
    if not _has_table(bind, "runs"):
        # ``runs`` is created later by revision 20260422_03 which also adds
        # ``mode``; nothing to do here for databases that haven't done the
        # rename yet.
        return
    if not _has_column(bind, "runs", "mode"):
        op.execute(
            sa.text(
                'ALTER TABLE runs ADD COLUMN IF NOT EXISTS "mode" VARCHAR(16) '
                "NOT NULL DEFAULT 'backtest'"
            )
        )
        op.execute(sa.text('ALTER TABLE runs ALTER COLUMN "mode" DROP DEFAULT'))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if _has_column(bind, "runs", "mode"):
        op.execute(sa.text('ALTER TABLE runs DROP COLUMN IF EXISTS "mode"'))
