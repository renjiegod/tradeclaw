"""unified async storage

Revision ID: 20260403_01
Revises: 
Create Date: 2026-04-03 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260403_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instances",
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("orchestrator_mode", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("data_provider", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("instance_id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(length=64), nullable=False),
        sa.Column("intent_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("approval_id"),
    )
    op.create_index("ix_approvals_status", "approvals", ["status"], unique=False)
    op.create_index("ix_approvals_expires_at", "approvals", ["expires_at"], unique=False)
    op.create_index(
        "ix_approvals_status_expires_at",
        "approvals",
        ["status", "expires_at"],
        unique=False,
    )
    op.create_table(
        "trace_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_trace_events_run_sequence"),
    )
    op.create_table(
        "system_state",
        sa.Column("state_key", sa.String(length=32), nullable=False),
        sa.Column("kill_switch_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("state_key"),
    )


def downgrade() -> None:
    op.drop_table("system_state")
    op.drop_table("trace_events")
    op.drop_index("ix_approvals_status_expires_at", table_name="approvals")
    op.drop_index("ix_approvals_expires_at", table_name="approvals")
    op.drop_index("ix_approvals_status", table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("instances")
