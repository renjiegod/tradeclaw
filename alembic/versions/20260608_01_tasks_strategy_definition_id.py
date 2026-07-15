"""Add tasks.strategy_definition_id denormalized column + index.

Mirrors ``settings.strategy.definition_id`` for indexed list filtering.
Backfills existing rows from JSON settings on SQLite and PostgreSQL.

Revision ID: 20260608_01
Revises: 20260607_01
Create Date: 2026-06-08 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260608_01"
down_revision = "20260607_01"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(col["name"] == column for col in inspect(bind).get_columns(table))


def _has_index(name: str) -> bool:
    bind = op.get_bind()
    for table_name in ("tasks",):
        if not inspect(bind).has_table(table_name):
            continue
        if any(idx["name"] == name for idx in inspect(bind).get_indexes(table_name)):
            return True
    return False


def _backfill_strategy_definition_id() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        bind.execute(
            sa.text(
                """
                UPDATE tasks
                SET strategy_definition_id = COALESCE(
                    NULLIF(trim(json_extract(settings, '$.strategy.definition_id')), ''),
                    NULLIF(trim(json_extract(settings, '$.strategy_definition_id')), '')
                )
                WHERE settings IS NOT NULL
                  AND COALESCE(
                    NULLIF(trim(json_extract(settings, '$.strategy.definition_id')), ''),
                    NULLIF(trim(json_extract(settings, '$.strategy_definition_id')), '')
                  ) IS NOT NULL
                """
            )
        )
        return
    if dialect == "postgresql":
        bind.execute(
            sa.text(
                """
                UPDATE tasks
                SET strategy_definition_id = COALESCE(
                    NULLIF(trim(settings->'strategy'->>'definition_id'), ''),
                    NULLIF(trim(settings->>'strategy_definition_id'), '')
                )
                WHERE settings IS NOT NULL
                  AND COALESCE(
                    NULLIF(trim(settings->'strategy'->>'definition_id'), ''),
                    NULLIF(trim(settings->>'strategy_definition_id'), '')
                  ) IS NOT NULL
                """
            )
        )
        return
    raise RuntimeError(
        f"unsupported database dialect for 20260608_01 backfill: {dialect}"
    )


def upgrade() -> None:
    if not _has_column("tasks", "strategy_definition_id"):
        op.add_column(
            "tasks",
            sa.Column("strategy_definition_id", sa.String(length=64), nullable=True),
        )
    _backfill_strategy_definition_id()
    if not _has_index("ix_tasks_strategy_definition_id"):
        op.create_index(
            "ix_tasks_strategy_definition_id",
            "tasks",
            ["strategy_definition_id"],
            unique=False,
        )


def downgrade() -> None:
    if _has_index("ix_tasks_strategy_definition_id"):
        op.drop_index("ix_tasks_strategy_definition_id", table_name="tasks")
    if _has_column("tasks", "strategy_definition_id"):
        op.drop_column("tasks", "strategy_definition_id")
