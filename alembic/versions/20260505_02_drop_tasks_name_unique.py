"""drop unique constraint on tasks.name

Revision ID: 20260505_02
Revises: 20260505_01
Create Date: 2026-05-05 22:50:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260505_02"
down_revision = "20260505_01"
branch_labels = None
depends_on = None


def _rebuild_sqlite_tasks_table(*, with_unique_name: bool) -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'")
    ).fetchone()
    if row is None or not row[0]:
        raise RuntimeError("tasks table definition not found")
    create_sql = str(row[0])
    if with_unique_name:
        if "name VARCHAR(255) NOT NULL UNIQUE" in create_sql:
            return
        new_create_sql = create_sql.replace(
            "name VARCHAR(255) NOT NULL,",
            "name VARCHAR(255) NOT NULL UNIQUE,",
            1,
        )
    else:
        if "name VARCHAR(255) NOT NULL UNIQUE" not in create_sql:
            return
        new_create_sql = create_sql.replace(
            "name VARCHAR(255) NOT NULL UNIQUE,",
            "name VARCHAR(255) NOT NULL,",
            1,
        )
    if new_create_sql == create_sql:
        raise RuntimeError("failed to rewrite tasks.name uniqueness in sqlite schema")

    op.execute("ALTER TABLE tasks RENAME TO tasks_old")
    op.execute(new_create_sql)
    op.execute(
        """
        INSERT INTO tasks (
            task_id,
            name,
            mode,
            description,
            data_provider,
            status,
            last_error,
            universe,
            execution_strategy,
            account_id,
            model_id,
            settings,
            enabled_skills,
            backtest_summary,
            created_at,
            updated_at
        )
        SELECT
            task_id,
            name,
            mode,
            description,
            data_provider,
            status,
            last_error,
            universe,
            execution_strategy,
            account_id,
            model_id,
            settings,
            enabled_skills,
            backtest_summary,
            created_at,
            updated_at
        FROM tasks_old
        """
    )
    op.execute("DROP TABLE tasks_old")


def _task_name_unique_constraint_name(bind) -> str | None:
    insp = inspect(bind)
    if not insp.has_table("tasks"):
        return None
    for constraint in insp.get_unique_constraints("tasks"):
        columns = list(constraint.get("column_names") or [])
        if columns == ["name"]:
            return str(constraint.get("name") or "")
    return None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    unique_name = _task_name_unique_constraint_name(bind)
    if dialect == "postgresql":
        if unique_name:
            op.drop_constraint(unique_name, "tasks", type_="unique")
        return
    if dialect == "sqlite":
        _rebuild_sqlite_tasks_table(with_unique_name=False)
        return
    if unique_name:
        with op.batch_alter_table("tasks") as batch_op:
            batch_op.drop_constraint(unique_name, type_="unique")


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.create_unique_constraint("tasks_name_key", "tasks", ["name"])
        return
    if dialect == "sqlite":
        _rebuild_sqlite_tasks_table(with_unique_name=True)
        return
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.create_unique_constraint("tasks_name_key", ["name"])
