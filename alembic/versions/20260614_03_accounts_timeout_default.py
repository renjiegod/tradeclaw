"""Raise ``accounts.timeout_seconds`` default from 5s to 30s.

The QMT proxy historical-bar endpoint (``/api/v1/data/market``) has a ~4s
baseline latency (two isolated xtquant subprocess spawns: download + read), so a
5s client timeout left <1s of margin and reproducibly tripped
``httpx.ReadTimeout`` → ``TransportError`` → ``signal_generation_failed`` under
any concurrent load. Bumping the default to 30s gives headroom; the qmt-proxy
side is separately optimized to halve that latency.

Changes:
- New column ``server_default`` becomes ``30.0`` for freshly inserted accounts.
- Existing rows still pinned at the old default ``5.0`` are bumped to ``30.0``
  (operator-customized non-5 values are left untouched).

Revision ID: 20260614_03
Revises: 20260614_02
Create Date: 2026-06-14 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260614_03"
down_revision = "20260614_02"
branch_labels = None
depends_on = None

_TABLE = "accounts"
_COLUMN = "timeout_seconds"
_OLD_DEFAULT = 5.0
_NEW_DEFAULT = 30.0


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    if not inspect(bind).has_table(table):
        return False
    return any(col["name"] == column for col in inspect(bind).get_columns(table))


def _set_default(value: float) -> None:
    # SQLite (used by the isolated e2e/test profile) cannot ``ALTER COLUMN ...
    # SET DEFAULT``. The server default only affects freshly inserted rows, and
    # the application always supplies ``timeout_seconds`` explicitly (model
    # ``default`` + ``account_resolution`` fallback), so skipping the DDL on
    # SQLite is safe — the data bump below still runs on every dialect.
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=sa.Float(),
        existing_nullable=False,
        server_default=str(value),
    )


def upgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        return
    _set_default(_NEW_DEFAULT)

    meta = sa.MetaData()
    accounts = sa.Table(
        _TABLE,
        meta,
        sa.Column(_COLUMN, sa.Float()),
    )
    op.execute(
        accounts.update()
        .where(accounts.c.timeout_seconds == _OLD_DEFAULT)
        .values(timeout_seconds=_NEW_DEFAULT)
    )


def downgrade() -> None:
    if not _has_column(_TABLE, _COLUMN):
        return
    _set_default(_OLD_DEFAULT)
