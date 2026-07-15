"""Squashed runtime schema (replaces prior linear revisions).

Revision ID: 20260411_01
Revises:
Create Date: 2026-04-11

Merges the historical chain into one migration for new databases.
Uses the same revision id as the previous Alembic head so databases already
stamped ``20260411_01`` continue to resolve; ``upgrade head`` is a no-op for them.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260411_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    json_empty = sa.text("'[]'::json") if is_pg else sa.text("'[]'")
    str_empty = sa.text("''")

    op.create_table(
        "instances",
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("template_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("orchestrator_mode", sa.String(length=32), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=str_empty),
        sa.Column("data_provider", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=str_empty),
        sa.Column("watch_symbols", sa.JSON(), nullable=False, server_default=json_empty),
        sa.Column("universe", sa.JSON(), nullable=False, server_default=json_empty),
        sa.Column("execution_strategy", sa.String(length=128), nullable=False, server_default=str_empty),
        sa.Column("account_id", sa.String(length=128), nullable=False, server_default=str_empty),
        sa.Column("model_id", sa.String(length=128), nullable=False, server_default=str_empty),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("enabled_skills", sa.JSON(), nullable=False, server_default=json_empty),
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
        sa.Column("reason", sa.Text(), nullable=False, server_default=str_empty),
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
    op.create_table(
        "model_invocations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=80), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("span_id", sa.String(length=64), nullable=True),
        sa.Column("call_kind", sa.String(length=32), nullable=False),
        sa.Column("first_token_latency_ms", sa.Integer(), nullable=True),
        sa.Column("total_latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=str_empty),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_model_invocations_created_at", "model_invocations", ["created_at"], unique=False)
    op.create_index("ix_model_invocations_trace_id", "model_invocations", ["trace_id"], unique=False)
    op.create_index("ix_model_invocations_span_id", "model_invocations", ["span_id"], unique=False)
    op.create_table(
        "debug_sessions",
        sa.Column("debug_session_id", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=80), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=str_empty),
        sa.Column("config_overrides", sa.JSON(), nullable=True),
        sa.Column("input_overrides", sa.JSON(), nullable=True),
        sa.Column("effective_config", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("session_type", sa.String(length=16), nullable=False, server_default="debug"),
        sa.PrimaryKeyConstraint("debug_session_id"),
    )
    op.create_index(
        "ix_debug_sessions_instance_created_at",
        "debug_sessions",
        ["instance_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "debug_session_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("debug_session_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("debug_session_id", "sequence", name="uq_debug_session_events_sequence"),
    )
    op.create_table(
        "debug_session_spans",
        sa.Column("span_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=80), nullable=False),
        sa.Column("parent_span_id", sa.String(length=64), nullable=True),
        sa.Column("debug_session_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("span_type", sa.String(length=64), nullable=False),
        sa.Column("start_time", sa.DateTime(), nullable=False),
        sa.Column("end_time", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.Column("span_source", sa.String(length=16), nullable=False, server_default="debug"),
        sa.PrimaryKeyConstraint("span_id"),
    )
    op.create_index("ix_debug_session_spans_trace_id", "debug_session_spans", ["trace_id"], unique=False)
    op.create_index(
        "ix_debug_session_spans_debug_session_id",
        "debug_session_spans",
        ["debug_session_id"],
        unique=False,
    )
    op.create_table(
        "cycle_runs",
        sa.Column("run_id", sa.String(length=80), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("trace_id", sa.String(length=80), nullable=True),
        sa.Column("run_mode", sa.String(length=32), nullable=False),
        sa.Column("run_kind", sa.String(length=16), nullable=False),
        sa.Column("clock_mode", sa.String(length=16), nullable=False),
        sa.Column("cycle_time_utc", sa.DateTime(), nullable=True),
        sa.Column("wall_started_at", sa.DateTime(), nullable=False),
        sa.Column("wall_finished_at", sa.DateTime(), nullable=True),
        sa.Column("runtime_params", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("universe_json", sa.JSON(), nullable=True),
        sa.Column("proposals_json", sa.JSON(), nullable=True),
        sa.Column("reviews_json", sa.JSON(), nullable=True),
        sa.Column("cycle_failed", sa.Boolean(), nullable=False),
        sa.Column("failure_message", sa.Text(), nullable=False),
        sa.Column("completed_phases_json", sa.JSON(), nullable=True),
        sa.Column("submitted_count", sa.Integer(), nullable=True),
        sa.Column("vetoed_count", sa.Integer(), nullable=True),
        sa.Column("pending_approval_count", sa.Integer(), nullable=True),
        sa.Column("backtest_job_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_cycle_runs_instance_started", "cycle_runs", ["instance_id", "wall_started_at"])
    op.create_index("ix_cycle_runs_backtest_job", "cycle_runs", ["backtest_job_id", "wall_started_at"])
    op.create_table(
        "backtest_jobs",
        sa.Column("backtest_job_id", sa.String(length=64), nullable=False),
        sa.Column("instance_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("market_profile", sa.String(length=32), nullable=False),
        sa.Column("bar_interval", sa.String(length=16), nullable=False),
        sa.Column("range_start_utc", sa.DateTime(), nullable=False),
        sa.Column("range_end_utc", sa.DateTime(), nullable=False),
        sa.Column("debug_session_id", sa.String(length=64), nullable=True),
        sa.Column("starting_equity", sa.Float(), nullable=True),
        sa.Column("ending_equity", sa.Float(), nullable=True),
        sa.Column("return_pct", sa.Float(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("bars_total", sa.Integer(), nullable=False),
        sa.Column("bars_completed", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("backtest_job_id"),
    )
    op.create_index("ix_backtest_jobs_instance_created", "backtest_jobs", ["instance_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_backtest_jobs_instance_created", table_name="backtest_jobs")
    op.drop_table("backtest_jobs")
    op.drop_index("ix_cycle_runs_backtest_job", table_name="cycle_runs")
    op.drop_index("ix_cycle_runs_instance_started", table_name="cycle_runs")
    op.drop_table("cycle_runs")
    op.drop_index("ix_debug_session_spans_debug_session_id", table_name="debug_session_spans")
    op.drop_index("ix_debug_session_spans_trace_id", table_name="debug_session_spans")
    op.drop_table("debug_session_spans")
    op.drop_table("debug_session_events")
    op.drop_index("ix_debug_sessions_instance_created_at", table_name="debug_sessions")
    op.drop_table("debug_sessions")
    op.drop_index("ix_model_invocations_span_id", table_name="model_invocations")
    op.drop_index("ix_model_invocations_trace_id", table_name="model_invocations")
    op.drop_index("ix_model_invocations_created_at", table_name="model_invocations")
    op.drop_table("model_invocations")
    op.drop_table("system_state")
    op.drop_table("trace_events")
    op.drop_index("ix_approvals_status_expires_at", table_name="approvals")
    op.drop_index("ix_approvals_expires_at", table_name="approvals")
    op.drop_index("ix_approvals_status", table_name="approvals")
    op.drop_table("approvals")
    op.drop_table("instances")
