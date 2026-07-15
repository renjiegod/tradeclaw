"""Rename ``instances`` -> ``tasks`` and ``backtest_jobs`` -> ``runs``.

Aligns migrated databases with ORM table names. Adds ``runs.mode`` (the split
``20260422_01`` revision is a no-op until ``runs`` exists; we finalize the
column here).

Idempotency: databases where the physical renames have already been applied
(e.g. from an earlier, since-reverted version of the Task/Run refactor) must
still converge cleanly. Each operation checks the current shape via SQLAlchemy
``Inspector`` and skips if the target state is already present.

Revision ID: 20260422_03
Revises: 20260422_02
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260422_03"
down_revision = "20260422_02"
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


def _rename_table(bind, old: str, new: str) -> None:
    if _has_table(bind, new):
        return
    if not _has_table(bind, old):
        return
    op.execute(sa.text(f"ALTER TABLE {old} RENAME TO {new}"))


def _drop_index_if_exists(bind, name: str, table: str) -> None:
    if _has_index(bind, table, name):
        op.drop_index(name, table_name=table)


def _create_index_if_missing(bind, name: str, table: str, columns: list[str]) -> None:
    if not _has_table(bind, table):
        return
    if _has_index(bind, table, name):
        return
    op.create_index(name, table, columns, unique=False)


def _add_mode_column_if_missing(bind) -> None:
    if not _has_table(bind, "runs"):
        return
    if _has_column(bind, "runs", "mode"):
        return
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(
            sa.text(
                'ALTER TABLE runs ADD COLUMN IF NOT EXISTS "mode" VARCHAR(16) '
                "NOT NULL DEFAULT 'backtest'"
            )
        )
        op.execute(sa.text('ALTER TABLE runs ALTER COLUMN "mode" DROP DEFAULT'))
    else:
        op.execute(
            sa.text(
                'ALTER TABLE runs ADD COLUMN "mode" VARCHAR(16) NOT NULL '
                "DEFAULT 'backtest'"
            )
        )


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect not in ("sqlite", "postgresql"):
        raise RuntimeError(f"unsupported database dialect for 20260422_03: {dialect}")

    # instances -> tasks
    _rename_column(bind, "instances", "instance_id", "task_id")
    _rename_table(bind, "instances", "tasks")

    # backtest_jobs -> runs (with column renames)
    _drop_index_if_exists(
        bind, "ix_backtest_jobs_instance_created", "backtest_jobs"
    )
    _rename_column(bind, "backtest_jobs", "instance_id", "task_id")
    _rename_column(bind, "backtest_jobs", "backtest_job_id", "run_id")
    _rename_table(bind, "backtest_jobs", "runs")

    _add_mode_column_if_missing(bind)

    _create_index_if_missing(
        bind, "ix_runs_task_created", "runs", ["task_id", "created_at"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect not in ("sqlite", "postgresql"):
        raise RuntimeError(
            f"unsupported database dialect for 20260422_03 downgrade: {dialect}"
        )

    _drop_index_if_exists(bind, "ix_runs_task_created", "runs")

    if _has_column(bind, "runs", "mode"):
        if dialect == "postgresql":
            op.execute(sa.text('ALTER TABLE runs DROP COLUMN IF EXISTS "mode"'))
        else:
            op.execute(sa.text('ALTER TABLE runs DROP COLUMN "mode"'))

    _rename_table(bind, "runs", "backtest_jobs")
    _rename_column(bind, "backtest_jobs", "run_id", "backtest_job_id")
    _rename_column(bind, "backtest_jobs", "task_id", "instance_id")
    _create_index_if_missing(
        bind,
        "ix_backtest_jobs_instance_created",
        "backtest_jobs",
        ["instance_id", "created_at"],
    )

    _rename_table(bind, "tasks", "instances")
    _rename_column(bind, "instances", "task_id", "instance_id")
