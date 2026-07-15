"""Rename ``instance_id`` to ``task_id`` on cycle_runs, debug_sessions, model_invocations.

The migration is idempotent: databases that already applied an earlier version
of the Task/Run refactor (before it was re-sequenced into the current chain)
may have some or all of these columns/indexes already in their renamed form.
We detect the current shape via SQLAlchemy ``Inspector`` and only perform the
DDL that is still needed.

Revision ID: 20260422_02
Revises: 20260422_01
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260422_02"
down_revision = "20260422_01"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return inspect(bind).has_table(name)


def _has_column(bind, table: str, column: str) -> bool:
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def _has_index(bind, table: str, name: str) -> bool:
    insp = inspect(bind)
    if not insp.has_table(table):
        return False
    return any(ix["name"] == name for ix in insp.get_indexes(table))


def _rename_column(bind, table: str, old: str, new: str) -> None:
    if not _has_table(bind, table):
        return
    if _has_column(bind, table, new):
        return
    if not _has_column(bind, table, old):
        return
    op.execute(sa.text(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}"))


def _drop_index_if_exists(bind, name: str, table: str) -> None:
    if _has_index(bind, table, name):
        op.drop_index(name, table_name=table)


def _create_index_if_missing(bind, name: str, table: str, columns: list[str]) -> None:
    if not _has_table(bind, table):
        return
    if _has_index(bind, table, name):
        return
    op.create_index(name, table, columns, unique=False)


def upgrade() -> None:
    bind = op.get_bind()

    _drop_index_if_exists(bind, "ix_cycle_runs_instance_started", "cycle_runs")
    _drop_index_if_exists(
        bind, "ix_debug_sessions_instance_created_at", "debug_sessions"
    )

    _rename_column(bind, "cycle_runs", "instance_id", "task_id")
    _rename_column(bind, "debug_sessions", "instance_id", "task_id")
    _rename_column(bind, "model_invocations", "instance_id", "task_id")

    _create_index_if_missing(
        bind, "ix_cycle_runs_task_started", "cycle_runs", ["task_id", "wall_started_at"]
    )
    _create_index_if_missing(
        bind,
        "ix_debug_sessions_task_created_at",
        "debug_sessions",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()

    _drop_index_if_exists(bind, "ix_debug_sessions_task_created_at", "debug_sessions")
    _drop_index_if_exists(bind, "ix_cycle_runs_task_started", "cycle_runs")

    _rename_column(bind, "model_invocations", "task_id", "instance_id")
    _rename_column(bind, "debug_sessions", "task_id", "instance_id")
    _rename_column(bind, "cycle_runs", "task_id", "instance_id")

    _create_index_if_missing(
        bind,
        "ix_cycle_runs_instance_started",
        "cycle_runs",
        ["instance_id", "wall_started_at"],
    )
    _create_index_if_missing(
        bind,
        "ix_debug_sessions_instance_created_at",
        "debug_sessions",
        ["instance_id", "created_at"],
    )
