"""Add ``accounts.qmt_terminal_id`` for multi-terminal qmt-proxy routing.

A single qmt-proxy can now front multiple QMT terminals (one ``XtQuantTrader``
per broker/terminal), selected per-request via the ``X-QMT-Terminal`` header.
Each account records which terminal (client_id) it routes to; ``NULL`` means the
proxy's configured default terminal (single-terminal deployments leave it null).

Nullable, no backfill: existing rows keep ``NULL`` and continue hitting the
default terminal, so this is a backward-compatible additive change.

Revision ID: 20260619_01
Revises: 20260616_01
Create Date: 2026-06-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260619_01"
down_revision = "20260616_01"
branch_labels = None
depends_on = None

_TABLE = "accounts"
_COLUMN = "qmt_terminal_id"


def _has_table(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return any(col["name"] == column for col in inspect(op.get_bind()).get_columns(table))


def upgrade() -> None:
    if not _has_table(_TABLE) or _has_column(_TABLE, _COLUMN):
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=128), nullable=True))


def downgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        return
    op.drop_column(_TABLE, _COLUMN)
