"""Swarm 多智能体编排表。

新增三张表支撑 Swarm Teams 功能：

- ``swarm_runs`` —— 一次 preset 团队执行的聚合根（状态/变量/最终报告/token）。
- ``swarm_tasks`` —— DAG 中的任务节点（绑定 agent role，记录依赖与执行结果）。
- ``swarm_events`` —— 运行事件日志，对标 ``assistant_events``，供 SSE 按 after_id
  分页流式推送各 worker 的实时状态。

backing models 见 :mod:`doyoutrade.persistence.models` 的 ``SwarmRunRecord`` /
``SwarmTaskRecord`` / ``SwarmEventRecord``。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260620_02"
down_revision = "20260620_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "swarm_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("preset_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("user_vars", sa.JSON(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("final_report", sa.Text(), nullable=True),
        sa.Column("total_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_swarm_runs_created_at", "swarm_runs", ["created_at"], unique=False)

    op.create_table(
        "swarm_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("depends_on", sa.JSON(), nullable=False),
        sa.Column("input_from", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("worker_iterations", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["swarm_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "task_id", name="uq_swarm_tasks_run_task"),
    )
    op.create_index("ix_swarm_tasks_run_id", "swarm_tasks", ["run_id"], unique=False)

    op.create_table(
        "swarm_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["swarm_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index("ix_swarm_events_run_id", "swarm_events", ["run_id", "id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_swarm_events_run_id", table_name="swarm_events")
    op.drop_table("swarm_events")
    op.drop_index("ix_swarm_tasks_run_id", table_name="swarm_tasks")
    op.drop_table("swarm_tasks")
    op.drop_index("ix_swarm_runs_created_at", table_name="swarm_runs")
    op.drop_table("swarm_runs")
