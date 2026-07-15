"""决策信号落库 → 回测验证闭环（功能 5）。

新增两张表：

- ``decision_signals`` —— 决策信号生命周期聚合根（来源 strategy/backtest/assistant，
  归因 task_id/run_id/cycle_run_id/trace_id/session_id 均为软引用 + 索引；
  ``dedupe_key`` 唯一键支撑 ``create_if_absent`` 幂等）。
- ``decision_signal_outcomes`` —— 每个 (signal, horizon, engine_version) 的回测
  验证结果（真实 FK → decision_signals.id, ON DELETE CASCADE；唯一键支撑
  ``upsert_outcome`` 幂等重估）。

backing models 见 :mod:`doyoutrade.persistence.models` 的
``DecisionSignalRecord`` / ``DecisionSignalOutcomeRecord``。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260713_01"
down_revision = "20260704_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "decision_signals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=80), nullable=True),
        sa.Column("cycle_run_id", sa.String(length=80), nullable=True),
        sa.Column("trace_id", sa.String(length=80), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("horizon", sa.String(length=16), nullable=False, server_default="5d"),
        sa.Column("entry_low", sa.String(length=64), nullable=True),
        sa.Column("entry_high", sa.String(length=64), nullable=True),
        sa.Column("stop_loss", sa.String(length=64), nullable=True),
        sa.Column("target_price", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "source IN ('strategy', 'backtest', 'assistant')",
            name="ck_decision_signals_source",
        ),
        sa.CheckConstraint(
            "action IN ('buy', 'sell', 'hold', 'add', 'reduce', 'watch', "
            "'take_profit', 'stop_loss')",
            name="ck_decision_signals_action",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'invalidated', 'evaluated')",
            name="ck_decision_signals_status",
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_decision_signals_dedupe_key"),
    )
    op.create_index("ix_decision_signals_run_id", "decision_signals", ["run_id"], unique=False)
    op.create_index("ix_decision_signals_task_id", "decision_signals", ["task_id"], unique=False)
    op.create_index(
        "ix_decision_signals_symbol_created",
        "decision_signals",
        ["symbol", "created_at"],
        unique=False,
    )
    op.create_index("ix_decision_signals_status", "decision_signals", ["status"], unique=False)

    op.create_table(
        "decision_signal_outcomes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("signal_id", sa.String(length=64), nullable=False),
        sa.Column("horizon", sa.String(length=16), nullable=False),
        sa.Column("engine_version", sa.String(length=64), nullable=False, server_default="v1"),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("direction_expected", sa.String(length=8), nullable=False),
        sa.Column("direction_correct", sa.Boolean(), nullable=True),
        sa.Column("anchor_date", sa.String(length=10), nullable=False),
        sa.Column("eval_window_days", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("max_gain_pct", sa.Float(), nullable=True),
        sa.Column("max_drawdown_pct", sa.Float(), nullable=True),
        sa.Column("return_pct", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["signal_id"], ["decision_signals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "outcome IN ('hit', 'miss', 'neutral')",
            name="ck_decision_signal_outcomes_outcome",
        ),
        sa.UniqueConstraint(
            "signal_id", "horizon", "engine_version",
            name="uq_decision_signal_outcomes_key",
        ),
    )
    op.create_index(
        "ix_decision_signal_outcomes_signal_id",
        "decision_signal_outcomes",
        ["signal_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_decision_signal_outcomes_signal_id", table_name="decision_signal_outcomes")
    op.drop_table("decision_signal_outcomes")
    op.drop_index("ix_decision_signals_status", table_name="decision_signals")
    op.drop_index("ix_decision_signals_symbol_created", table_name="decision_signals")
    op.drop_index("ix_decision_signals_task_id", table_name="decision_signals")
    op.drop_index("ix_decision_signals_run_id", table_name="decision_signals")
    op.drop_table("decision_signals")
