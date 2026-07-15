"""channels table

Revision ID: 20260430_02
Revises: 20260430_01
Create Date: 2026-04-30 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260430_02"
down_revision = "20260430_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="stopped"),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_connected_at", sa.DateTime(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("secrets", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_channels_type_enabled", "channels", ["type", "enabled"], unique=False)
    op.create_index("ix_channels_agent_id", "channels", ["agent_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_channels_agent_id", table_name="channels")
    op.drop_index("ix_channels_type_enabled", table_name="channels")
    op.drop_table("channels")
