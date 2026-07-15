"""Add task_triggers table + cycle_runs.trigger_id (Phase 1 of Task-centric Trigger unification).

Introduces first-class child Triggers owned by a Task (FK + cascade delete). Each
Trigger carries the three previously-entangled axes orthogonally: schedule
(schedule_kind interval|cron|at|backtest_range), execution intent
(trade|signal_only), and delivery (delivery_json, None = no push).

``cycle_runs.trigger_id`` is the attribution dimension so a Trigger-fired cycle is
attributable on the run row itself:
trigger_id -> run_id <-> debug_sessions <-> spans <-> model_invocations <-> trade_fills.

Phase 1 is purely additive — nothing reads/writes these yet except the new
TriggerScheduler firing path. tasks.tick_mode / Task.mode='signal_only' are NOT
touched here (Phase 3).

Revision ID: 20260611_02
Revises: 20260611_01
Create Date: 2026-06-11 00:00:01.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260611_02"
down_revision = "20260611_01"
branch_labels = None
depends_on = None


def _has_table(table: str) -> bool:
    return inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    if not inspect(bind).has_table(table):
        return False
    return any(col["name"] == column for col in inspect(bind).get_columns(table))


def _has_index(table: str, index: str) -> bool:
    bind = op.get_bind()
    if not inspect(bind).has_table(table):
        return False
    return any(ix["name"] == index for ix in inspect(bind).get_indexes(table))


def upgrade() -> None:
    if not _has_table("task_triggers"):
        op.create_table(
            "task_triggers",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column(
                "task_id",
                sa.String(length=64),
                sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
            sa.Column("schedule_kind", sa.String(length=16), nullable=False),
            sa.Column("interval_seconds", sa.Integer(), nullable=True),
            sa.Column("cron_expression", sa.String(length=128), nullable=True),
            sa.Column("timezone", sa.String(length=64), nullable=False, server_default="UTC"),
            sa.Column("at_iso", sa.String(length=64), nullable=True),
            sa.Column("range_start", sa.String(length=32), nullable=True),
            sa.Column("range_end", sa.String(length=32), nullable=True),
            sa.Column("bar_interval", sa.String(length=16), nullable=True),
            sa.Column("trading_session", sa.String(length=32), nullable=True),
            sa.Column("delete_after_run", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column(
                "execution_intent",
                sa.String(length=16),
                nullable=False,
                server_default="signal_only",
            ),
            sa.Column("delivery_json", sa.JSON(), nullable=True),
            sa.Column("last_fired_at", sa.DateTime(), nullable=True),
            sa.Column("next_fire_at", sa.DateTime(), nullable=True),
            sa.Column("last_run_id", sa.String(length=80), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "schedule_kind IN ('interval', 'cron', 'at', 'backtest_range')",
                name="ck_task_triggers_schedule_kind",
            ),
            sa.CheckConstraint(
                "execution_intent IN ('trade', 'signal_only')",
                name="ck_task_triggers_execution_intent",
            ),
            sa.CheckConstraint(
                "status IN ('active', 'paused', 'exhausted', 'error')",
                name="ck_task_triggers_status",
            ),
        )
        op.create_index("ix_task_triggers_task_id", "task_triggers", ["task_id"])
        op.create_index("ix_task_triggers_active", "task_triggers", ["enabled", "status"])

    if not _has_column("cycle_runs", "trigger_id"):
        op.add_column(
            "cycle_runs",
            sa.Column("trigger_id", sa.String(length=64), nullable=True),
        )
    if not _has_index("cycle_runs", "ix_cycle_runs_trigger_id"):
        op.create_index("ix_cycle_runs_trigger_id", "cycle_runs", ["trigger_id"])


def downgrade() -> None:
    if _has_index("cycle_runs", "ix_cycle_runs_trigger_id"):
        op.drop_index("ix_cycle_runs_trigger_id", table_name="cycle_runs")
    if _has_column("cycle_runs", "trigger_id"):
        op.drop_column("cycle_runs", "trigger_id")
    if _has_table("task_triggers"):
        op.drop_index("ix_task_triggers_active", table_name="task_triggers")
        op.drop_index("ix_task_triggers_task_id", table_name="task_triggers")
        op.drop_table("task_triggers")
