"""agents table + assistant_sessions.agent_id

Revision ID: 20260430_01
Revises: 20260429_01
Create Date: 2026-04-30 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260430_01"
down_revision = "20260429_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create agents table
    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("model_route_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("tool_names", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("skill_names", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("max_turns", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="FALSE"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    # Use batch mode for SQLite-compatible FK + column addition
    with op.batch_alter_table("assistant_sessions", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("agent_id", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            "fk_assistant_sessions_agent_id",
            "agents",
            ["agent_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_assistant_sessions_agent_id",
        "assistant_sessions",
        ["agent_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_sessions_agent_id", table_name="assistant_sessions")
    with op.batch_alter_table("assistant_sessions", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "fk_assistant_sessions_agent_id",
            type_="foreignkey",
        )
        batch_op.drop_column("agent_id")
    op.drop_table("agents")
