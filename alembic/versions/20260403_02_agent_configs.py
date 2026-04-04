"""agent instance configuration

Revision ID: 20260403_02
Revises: 20260403_01
Create Date: 2026-04-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260403_02"
down_revision = "20260403_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_configs",
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("watch_symbols", sa.JSON(), nullable=False),
        sa.Column("execution_strategy", sa.String(length=128), nullable=False),
        sa.Column("account_id", sa.String(length=128), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["instance_id"],
            ["instances.instance_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("instance_id"),
    )


def downgrade() -> None:
    op.drop_table("agent_configs")
