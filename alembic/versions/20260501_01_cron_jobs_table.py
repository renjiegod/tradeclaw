"""cron_jobs table

Revision ID: 20260501_01
Revises: 20260430_02
Create Date: 2026-05-01 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260501_01"
down_revision = "20260430_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cron_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("cron_expression", sa.String(length=128), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("input_template", sa.Text(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("120")),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_run_session_id", sa.String(length=64), nullable=True),
        sa.Column("last_status", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_cron_jobs_agent_id", "cron_jobs", ["agent_id"], unique=False)
    op.create_index("ix_cron_jobs_enabled", "cron_jobs", ["enabled"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cron_jobs_enabled", table_name="cron_jobs")
    op.drop_index("ix_cron_jobs_agent_id", table_name="cron_jobs")
    op.drop_table("cron_jobs")