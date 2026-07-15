"""Add generated_strategies table for AI-generated strategy persistence.

Revision ID: 20260428_01
Revises: 20260427_01
Create Date: 2026-04-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260428_01"
down_revision = "20260427_01"
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
    if _has_table(bind, "generated_strategies"):
        return
    op.create_table(
        "generated_strategies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(80), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("class_name", sa.String(128), nullable=False),
        sa.Column("code_hash", sa.String(32), nullable=False),
        sa.Column("source_code", sa.Text(), nullable=False),
        sa.Column("generation_prompt", sa.Text(), nullable=True),
        sa.Column("generation_model", sa.String(255), nullable=True),
        sa.Column("generation_metadata_json", sa.JSON(), nullable=True),
        sa.Column("strategy_tag", sa.String(128), nullable=True),
        sa.Column("bar_interval", sa.String(16), nullable=False),
        sa.Column("default_params_json", sa.JSON(), nullable=True),
        sa.Column("param_constraints_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_id"),
    )
    op.create_index(
        "ix_generated_strategies_task_created",
        "generated_strategies",
        ["task_id", "created_at"],
    )
    op.create_index(
        "ix_generated_strategies_run_id",
        "generated_strategies",
        ["run_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind, "generated_strategies"):
        return
    with op.batch_drop_table("generated_strategies") as batch_op:
        pass
