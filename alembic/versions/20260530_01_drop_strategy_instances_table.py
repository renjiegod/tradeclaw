"""Drop deprecated strategy_instances table.

Revision ID: 20260530_01
Revises: 20260528_01
Create Date: 2026-05-30 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260530_01"
down_revision = "20260528_01"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("strategy_instances"):
        op.drop_index(
            "ix_strategy_instances_definition_id",
            table_name="strategy_instances",
        )
        op.drop_index(
            "ix_strategy_instances_name",
            table_name="strategy_instances",
        )
        op.drop_table("strategy_instances")


def downgrade() -> None:
    if not _has_table("strategy_instances"):
        op.create_table(
            "strategy_instances",
            sa.Column("instance_id", sa.String(length=64), nullable=False),
            sa.Column("definition_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("parameters_json", sa.JSON(), nullable=True),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column("tags_json", sa.JSON(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["definition_id"],
                ["strategy_definitions.definition_id"],
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("instance_id"),
        )
        op.create_index(
            "ix_strategy_instances_definition_id",
            "strategy_instances",
            ["definition_id"],
            unique=False,
        )
        op.create_index(
            "ix_strategy_instances_name",
            "strategy_instances",
            ["name"],
            unique=False,
        )
