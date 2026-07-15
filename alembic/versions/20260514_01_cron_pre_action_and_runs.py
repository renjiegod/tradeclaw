"""cron pre_action column, cron_job_runs table, tasks.tick_mode

Revision ID: 20260514_01
Revises: 20260508_01
Create Date: 2026-05-14 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260514_01"
down_revision = "20260508_01"
branch_labels = None
depends_on = None


def _existing_columns(bind, table: str) -> set[str]:
    insp = inspect(bind)
    if not insp.has_table(table):
        return set()
    return {col["name"] for col in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    task_cols = _existing_columns(bind, "tasks")
    with op.batch_alter_table("tasks") as batch_op:
        if "tick_mode" not in task_cols:
            batch_op.add_column(
                sa.Column(
                    "tick_mode",
                    sa.String(length=32),
                    nullable=False,
                    server_default="interval",
                )
            )
            batch_op.create_check_constraint(
                "ck_tasks_tick_mode",
                "tick_mode IN ('interval', 'cron_driven')",
            )

    cron_cols = _existing_columns(bind, "cron_jobs")
    with op.batch_alter_table("cron_jobs") as batch_op:
        if "pre_action" not in cron_cols:
            batch_op.add_column(sa.Column("pre_action", sa.JSON(), nullable=True))

    if not inspect(bind).has_table("cron_job_runs"):
        op.create_table(
            "cron_job_runs",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("job_id", sa.String(length=64), nullable=False),
            sa.Column("fired_at", sa.DateTime(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("pre_kind", sa.String(length=64), nullable=True),
            sa.Column("pre_status", sa.String(length=32), nullable=True),
            sa.Column("pre_run_id", sa.String(length=64), nullable=True),
            sa.Column("pre_debug_session_id", sa.String(length=128), nullable=True),
            sa.Column("pre_result_json", sa.JSON(), nullable=True),
            sa.Column("pre_error", sa.Text(), nullable=True),
            sa.Column("agent_session_id", sa.String(length=64), nullable=True),
            sa.Column("agent_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["cron_jobs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_cron_job_runs_job_id_fired_at",
            "cron_job_runs",
            ["job_id", "fired_at"],
            unique=False,
        )
        op.create_index(
            "ix_cron_job_runs_pre_run_id",
            "cron_job_runs",
            ["pre_run_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()

    if inspect(bind).has_table("cron_job_runs"):
        op.drop_index("ix_cron_job_runs_pre_run_id", table_name="cron_job_runs")
        op.drop_index("ix_cron_job_runs_job_id_fired_at", table_name="cron_job_runs")
        op.drop_table("cron_job_runs")

    cron_cols = _existing_columns(bind, "cron_jobs")
    if "pre_action" in cron_cols:
        with op.batch_alter_table("cron_jobs") as batch_op:
            batch_op.drop_column("pre_action")

    insp = inspect(bind)
    existing_checks: set[str] = set()
    if insp.has_table("tasks"):
        existing_checks = {
            c.get("name")
            for c in insp.get_check_constraints("tasks")
            if c.get("name")
        }
    task_cols = _existing_columns(bind, "tasks")
    with op.batch_alter_table("tasks") as batch_op:
        if "ck_tasks_tick_mode" in existing_checks:
            batch_op.drop_constraint("ck_tasks_tick_mode", type_="check")
        if "tick_mode" in task_cols:
            batch_op.drop_column("tick_mode")
