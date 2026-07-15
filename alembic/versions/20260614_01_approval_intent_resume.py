"""Add approval intent-resume columns to ``approvals``.

The execution-side approval gate used to persist only ``intent_id`` (a string),
so an approved pending order had no way to actually reach the broker — the
``OrderIntent`` lived only in the cycle's memory and was lost when the cycle
ended. These additive, nullable columns let the scheduler re-dispatch an
approved order: ``intent_payload`` carries the serialized OrderIntent, the run
context (``run_id`` / ``task_id`` / ``trace_id`` / ``account_id``) keeps run_id
threading intact, ``notional`` (decimal string) + ``symbol`` / ``action`` make
the pending order legible on cards / the web UI without re-parsing the payload,
and ``resolver_id`` / ``decision_source`` / ``decided_at`` audit who decided.
``dispatched_at`` / ``dispatch_error`` / ``dispatch_attempts`` drive idempotent,
retry-bounded resume.

All columns are nullable with no backfill — existing rows stay NULL and the old
in-memory path is unchanged.

Revision ID: 20260614_01
Revises: 20260613_01
Create Date: 2026-06-14 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260614_01"
down_revision = "20260613_01"
branch_labels = None
depends_on = None


# (column_name, sqlalchemy type) — additive, all nullable.
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("intent_payload", sa.Text()),
    ("run_id", sa.String(length=64)),
    ("task_id", sa.String(length=64)),
    ("trace_id", sa.String(length=64)),
    ("account_id", sa.String(length=64)),
    ("symbol", sa.String(length=32)),
    ("action", sa.String(length=8)),
    ("notional", sa.String(length=64)),
    ("resolver_id", sa.String(length=64)),
    ("decision_source", sa.String(length=16)),
    ("decided_at", sa.DateTime()),
    ("dispatched_at", sa.DateTime()),
    ("dispatch_error", sa.Text()),
    ("dispatch_attempts", sa.Integer()),
)

# Indexed columns (resume + per-task lookups go through these).
_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_approvals_run_id", "run_id"),
    ("ix_approvals_task_id", "task_id"),
    ("ix_approvals_dispatched_at", "dispatched_at"),
)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    if not inspect(bind).has_table(table):
        return False
    return any(col["name"] == column for col in inspect(bind).get_columns(table))


def _has_index(table: str, index_name: str) -> bool:
    bind = op.get_bind()
    if not inspect(bind).has_table(table):
        return False
    return any(ix["name"] == index_name for ix in inspect(bind).get_indexes(table))


def upgrade() -> None:
    for name, col_type in _COLUMNS:
        if not _has_column("approvals", name):
            op.add_column("approvals", sa.Column(name, col_type, nullable=True))
    for index_name, column in _INDEXES:
        if _has_column("approvals", column) and not _has_index("approvals", index_name):
            op.create_index(index_name, "approvals", [column])


def downgrade() -> None:
    for index_name, _column in _INDEXES:
        if _has_index("approvals", index_name):
            op.drop_index(index_name, table_name="approvals")
    for name, _col_type in reversed(_COLUMNS):
        if _has_column("approvals", name):
            op.drop_column("approvals", name)
