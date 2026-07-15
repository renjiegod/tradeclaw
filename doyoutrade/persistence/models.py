from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.persistence.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class InstrumentCatalog(Base):
    """Canonical tradable / watchable symbols synced from akshare or QMT."""

    __tablename__ = "instrument_catalog"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    market: Mapped[str | None] = mapped_column(String(16), nullable=True)
    instrument_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_tradable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_sync_source: Mapped[str] = mapped_column(String(16), nullable=False)
    last_sync_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class ModelRoute(Base):
    """Self-contained model configuration.

    Formerly split into ``model_providers`` (connection + credentials) and
    ``model_routes`` (named handle + overrides). Merged into a single concept so
    users configure one entity per model: it holds both the connection/credential
    columns (``provider_kind`` / ``base_url`` / ``api_key``) and the named handle
    (``route_name``) plus a single override layer (``settings``).
    """

    __tablename__ = "model_routes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    route_name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    provider_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    target_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class Task(Base):
    """Immutable task configuration."""
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_strategy_definition_id", "strategy_definition_id"),
    )

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mode: Mapped[str] = mapped_column("mode", String(32), nullable=False, default="paper", quote=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    data_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="configured")
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    universe: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    execution_strategy: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    account_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    strategy_definition_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled_skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    backtest_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class TaskTrigger(Base):
    """A schedule + execution-intent + delivery binding OWNED by a Task.

    One Task may own N triggers (FK + cascade-delete), so a single strategy can
    both trade during the day and push a signal card at 14:50, or push to two
    channels — without a separate cron object bound back by opaque id. The three
    axes that were historically entangled in ``Task.mode`` + ``Task.tick_mode`` +
    a separate ``cron_jobs`` row live here orthogonally:

    - schedule: ``schedule_kind`` (interval | cron | at | backtest_range) + its fields
    - execution intent: ``execution_intent`` (trade | signal_only) — the per-fire
      run-mode lens applied via run_mode_override, replacing the old 3-condition
      readiness gate.
    - delivery: ``delivery_json`` (None = no push) — consumed by the post-cycle
      delivery hook in Phase 2; in Phase 1 it is stored but not yet delivered.
    """

    __tablename__ = "task_triggers"
    __table_args__ = (
        CheckConstraint(
            "schedule_kind IN ('interval', 'cron', 'at', 'backtest_range')",
            name="ck_task_triggers_schedule_kind",
        ),
        CheckConstraint(
            "execution_intent IN ('trade', 'signal_only')",
            name="ck_task_triggers_execution_intent",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'exhausted', 'error')",
            name="ck_task_triggers_status",
        ),
        Index("ix_task_triggers_task_id", "task_id"),
        Index("ix_task_triggers_active", "enabled", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # trg-<hex12>
    task_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")

    # --- schedule axis (tagged-union on schedule_kind) ---
    schedule_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cron_expression: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    at_iso: Mapped[str | None] = mapped_column(String(64), nullable=True)
    range_start: Mapped[str | None] = mapped_column(String(32), nullable=True)
    range_end: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bar_interval: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # shared schedule modifiers
    trading_session: Mapped[str | None] = mapped_column(String(32), nullable=True)
    delete_after_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- execution axis ---
    execution_intent: Mapped[str] = mapped_column(
        String(16), nullable=False, default="signal_only"
    )

    # --- delivery axis (None = no push) ---
    delivery_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- bookkeeping ---
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column("mode", String(32), nullable=False, quote=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Intent-resume context (migration 20260614_01). All nullable so legacy rows
    # and the in-memory path are unchanged. Lets an approved pending order be
    # re-dispatched to the broker after the cycle that created it has ended.
    intent_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str | None] = mapped_column(String(8), nullable=True)
    #: Quote-currency notional as an exact decimal string (§金额十进制).
    notional: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolver_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: One of: ``web`` / ``api`` / ``feishu_card``.
    decision_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    dispatch_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatch_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SystemStateRecord(Base):
    __tablename__ = "system_state"

    state_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    kill_switch_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class ModelInvocationRecord(Base):
    __tablename__ = "model_invocations"
    __table_args__ = (
        Index("ix_model_invocations_created_at", "created_at"),
        Index("ix_model_invocations_trace_id", "trace_id"),
        Index("ix_model_invocations_span_id", "span_id"),
        Index("ix_model_invocations_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    model_route_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    call_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    first_token_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DebugSessionRecord(Base):
    __tablename__ = "debug_sessions"
    __table_args__ = (
        Index("ix_debug_sessions_task_created_at", "task_id", "created_at"),
        # Standalone created_at index for the retention TTL prune; the composite
        # (task_id, created_at) above cannot serve a bare ``created_at < cutoff``.
        Index("ix_debug_sessions_created_at", "created_at"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    traceback_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    input_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    effective_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    session_type: Mapped[str] = mapped_column(String(16), nullable=False, default="debug")


class DebugSessionEventRecord(Base):
    __tablename__ = "debug_session_events"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_debug_session_events_sequence"),
        # Age index for the retention TTL prune (delete WHERE timestamp < cutoff).
        Index("ix_debug_session_events_timestamp", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class CycleRunRecord(Base):
    """One row per :meth:`~doyoutrade.core.worker.TradingWorker.run_cycle` (including debug runs)."""

    __tablename__ = "cycle_runs"
    __table_args__ = (
        Index("ix_cycle_runs_task_started", "task_id", "wall_started_at"),
        Index("ix_cycle_runs_session_started", "session_id", "wall_started_at"),
        Index("ix_cycle_runs_trace_id", "trace_id"),
        Index("ix_cycle_runs_trigger_id", "trigger_id"),
    )

    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # debug / tick span session (debug_sessions.session_id), optional
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    run_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="paper")
    # scheduled | manual | cron | debug | backtest_bar | trigger
    run_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="scheduled")
    # task_triggers.id when this cycle was fired by a Trigger (run_kind='trigger'); NULL otherwise.
    # Attribution dimension: trigger_id -> run_id <-> debug_sessions <-> spans <-> model_invocations <-> trade_fills.
    trigger_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # wall: use real time; simulated: LLM/data stack use cycle_time_utc as logical "now"
    clock_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="wall")
    cycle_time_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    wall_started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    wall_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Debug/tick payloads: input_overrides, config_overrides, etc.
    runtime_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    # Per-cycle artifacts (universe, proposals, reviews, future keys) — see cycle_runs.details migration.
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cycle_failed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    completed_phases_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Strategy code version pinned at cycle-start (e.g. "v0001-abc123ef").
    # Protects in-flight cycles from concurrent edits that bump current_version.
    code_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    # CycleReport counters when successful
    submitted_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vetoed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_approval_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Run(Base):
    """One row per full multi-bar backtest run for a task."""
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_task_created", "task_id", "created_at"),)

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column("mode", String(16), nullable=False, quote=True)  # backtest / paper / live
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    market_profile: Mapped[str] = mapped_column(String(32), nullable=False, default="cn_a_share")
    bar_interval: Mapped[str] = mapped_column(String(16), nullable=False, default="1d")
    range_start_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    range_end_utc: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Whether this run captured full debug observability (debug_sessions /
    # debug_session_spans / span events / cycle_runs / model_invocations). When
    # False the run executed in fast mode: only run status + report + trade_fills
    # were persisted, so the absence of trace detail is intentional, not a fault.
    # Defaults True to preserve historical behavior on pre-migration rows.
    debug_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    # Normalized per-run backtest config_overrides (settings / universe). In
    # debug mode these also live on the debug session; persisting them here lets
    # a fast-mode (no debug session) run rebuild its merged config on resume.
    # NULL when no overrides were supplied.
    config_overrides_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model_route_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    starting_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    ending_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    bars_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bars_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stop_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ledger_checkpoint_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reference_starting_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Run-time snapshot of the effective CycleTaskConfig (parameters +
    # position constraints + approval policy etc.). Lets analytics replay
    # the exact configuration this run executed under even after the
    # source task has been edited. NULL on pre-migration rows.
    config_snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Identifier of the worker/runner/compiler version that produced this
    # run (e.g. ``"doyoutrade-0.4.1"``). Distinguishes results from
    # different engine releases when comparing strategy returns across
    # time. NULL on pre-migration rows.
    engine_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # ``StrategyDefinition.code_hash`` captured at run start. The
    # definition row itself may be edited later; this snapshot records
    # which source the run actually compiled and executed. NULL on
    # pre-migration rows.
    strategy_code_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Versioned-directory label (e.g. "v0001-abc123ef") for the strategy code
    # compiled at backtest-start. Complements strategy_code_hash so analytics
    # can reconstruct the exact on-disk path without joining strategy_definitions.
    # NULL on pre-migration rows.
    code_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TradeFillRecord(Base):
    """One row per executed fill across backtest/paper/live cycles."""

    __tablename__ = "trade_fills"
    __table_args__ = (
        Index("ix_trade_fills_task_run_symbol_time", "task_id", "run_id", "symbol", "filled_at"),
        Index("ix_trade_fills_task_cycle", "task_id", "cycle_run_id"),
        Index("ix_trade_fills_session_time", "session_id", "filled_at"),
        UniqueConstraint(
            "task_id",
            "cycle_run_id",
            "symbol",
            "side",
            "filled_at",
            "price",
            "quantity",
            "intent_id_normalized",
            name="uq_trade_fills_dedupe",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cycle_run_id: Mapped[str] = mapped_column(String(80), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[str] = mapped_column(String(64), nullable=False)
    price: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fee: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    intent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Strategy.on_bar(...) attaches a factor identifier (e.g. "ma_cross+rsi_ok").
    # The runner copies it from Signal.tag onto the corresponding OrderIntent,
    # and the execution layer persists it onto fills so trade analytics can
    # group by which factor combination triggered each entry/exit.
    entry_tag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    exit_tag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Exit categorization for SELL fills (one of strategy_sdk.signal.ExitReason:
    # signal / stop_loss / take_profit / trailing_stop / roi / circuit_breaker).
    # Copied from OrderIntent.exit_reason; NULL on buys and on exits that did
    # not categorize. Powers closed-trade attribution by exit kind.
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Stable uniqueness part: `NULL` values are distinct under UNIQUE in SQLite/Postgres.
    intent_id_normalized: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    filled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class StrategyDefinitionRecord(Base):
    __tablename__ = "strategy_definitions"
    __table_args__ = (
        Index("ix_strategy_definitions_name", "name"),
        Index("ix_strategy_definitions_status", "status"),
    )

    definition_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    current_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_contract_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parameter_schema_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    default_parameters_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    capabilities_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    provenance_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    generation_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    generation_model: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    generation_metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class DebugSessionSpanRecord(Base):
    __tablename__ = "debug_session_spans"
    __table_args__ = (
        Index("ix_debug_session_spans_trace_id", "trace_id"),
        Index("ix_debug_session_spans_session_id", "session_id"),
        # Age index for the retention TTL prune (delete WHERE start_time < cutoff);
        # this table has no created_at, so start_time is the age column.
        Index("ix_debug_session_spans_start_time", "start_time"),
    )

    span_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(80), nullable=False)
    parent_span_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    span_type: Mapped[str] = mapped_column(String(64), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    span_source: Mapped[str] = mapped_column(String(16), nullable=False, default="debug")


class AssistantSessionRecord(Base):
    __tablename__ = "assistant_sessions"
    __table_args__ = (
        Index("ix_assistant_sessions_updated_at", "updated_at"),
        Index("ix_assistant_sessions_agent_id", "agent_id"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="idle")
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class ChannelPeerSessionRecord(Base):
    """Durable peer → active-session routing for external channels.

    A channel peer (e.g. a Feishu user, keyed by the deterministic
    ``channel:{channel_id}:{sender_id}`` id) normally talks to that same
    deterministic session. When the peer issues ``/new``, the
    ``ChannelManager`` rebinds the peer to a freshly created ``asst-…``
    session so subsequent messages land there. That rebinding used to live
    only in :class:`ChannelManager._active_peer_sessions` (process memory) and
    was lost on restart, silently snapping the peer back to the old session.
    This table persists the rebinding so it survives restarts; the in-memory
    map is now just a hot cache seeded from here.
    """

    __tablename__ = "channel_peer_sessions"

    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    peer_session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    active_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class AgentRecord(Base):
    """Agent template for assistant sessions."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt_template_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_route_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    tool_names: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    tool_configs_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    skill_names: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    max_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Code-fixed builtin main agent marker. Exactly one row (``agent_default``)
    # carries this; it is undeletable / unrenamable and its skills/tools/prompt
    # are controlled in code (see doyoutrade/assistant/main_agent.py). Distinct
    # from ``is_default`` (routing fallback) so custom agents stay distinguishable.
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    context_compaction_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class ChannelRecord(Base):
    """External assistant communication channel configuration and runtime status."""

    __tablename__ = "channels"
    __table_args__ = (
        Index("ix_channels_type_enabled", "type", "enabled"),
        Index("ix_channels_agent_id", "agent_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="stopped")
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    secrets: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class CronJobRecord(Base):
    """Cron job for Assistant Agent scheduled execution."""

    __tablename__ = "cron_jobs"
    __table_args__ = (
        Index("ix_cron_jobs_agent_id", "agent_id"),
        Index("ix_cron_jobs_enabled", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # ``cron_expression`` is the schedule for ``schedule_kind="cron"`` rows
    # (and remains the source of truth for legacy rows created before the
    # tagged-union schedule landed). For ``schedule_kind="at"`` we still
    # populate this column with a synthetic 5-field expression that pins
    # the same minute, so legacy readers / ``list_jobs_with_statuses``
    # consumers keep working — but ``at_iso`` is the authoritative wall
    # clock and the manager uses ``DateTrigger`` for second-level fire
    # precision.
    cron_expression: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    # Tagged-union schedule kind. ``cron`` = recurring via APScheduler
    # ``CronTrigger`` (existing behaviour, default for back-compat).
    # ``at`` = one-shot fire at ``at_iso``, used for the dominant LLM
    # "fire in N seconds/minutes" intent (eliminates TZ drift because
    # ``at_iso`` carries an explicit offset).
    schedule_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cron", server_default="cron",
    )
    # ISO-8601 instant with offset (e.g. ``2026-05-24T10:23:00+08:00``)
    # for ``schedule_kind="at"``. Null otherwise.
    at_iso: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # If true, the job is deleted after its terminal-state fire. Default
    # false for back-compat (recurring jobs). API/CLI auto-default this
    # to true for ``schedule_kind="at"`` so the LLM doesn't have to bake
    # self-destruct instructions into the input_template.
    delete_after_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # ``input_template`` is the legacy "raw user-message Jinja template" used
    # by the pre-Task-3 pipeline; new rows leave it null and store their
    # payload under ``task_params_json``. cron_manager keeps the legacy path
    # for backward compatibility with rows that predate the migration.
    input_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    pre_action: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Polymorphic task dispatch — ``JobTaskRegistry`` picks an executor by
    # ``task_kind`` and passes ``task_params_json`` as its params dict. Null
    # for legacy rows; cron_manager falls back to the pre_action + render +
    # send_message pipeline in that case.
    task_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_params_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class AccountRecord(Base):
    """A persisted QMT account: proxy connection + trading identity + mode.

    Replaces the single ``config.data.qmt`` block — there can now be many
    accounts (live or mock) and a task selects one by ``account_id``.
    Credentials are stored in plaintext (local single-user deployment).
    Exactly one row should carry ``is_default=True``; the repository's
    ``set_default`` enforces that invariant transactionally rather than via a
    DB partial-unique index (SQLite/Postgres behave differently there).
    """

    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("mode IN ('live','mock')", name="ck_accounts_mode"),
        Index("ix_accounts_is_default", "is_default"),
        Index("ix_accounts_enabled", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # acct-<hex12>
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # ``live`` = real QMT trading terminal; ``mock`` = simulated portfolio.
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="live")
    # --- QMT proxy connection (serves both market data and trading) ---
    base_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    token: Mapped[str | None] = mapped_column(Text, nullable=True)  # plaintext API key
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=30.0)
    # Which QMT terminal (client) on a multi-terminal qmt-proxy this account
    # routes to. Sent as the ``X-QMT-Terminal`` header; ``None`` → proxy's
    # configured default terminal (single-terminal deployments leave this null).
    qmt_terminal_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # --- trading identity (the old ``QmtSettings.account_id``) ---
    qmt_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # refreshed on connect
    # --- mock portfolio (mode=="mock") ---
    mock_cash: Mapped[float] = mapped_column(Float, nullable=False, default=100_000.0)
    mock_equity: Mapped[float] = mapped_column(Float, nullable=False, default=100_000.0)
    # [{"symbol": str, "quantity": number, "cost_price": number}, ...]
    mock_positions: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    # --- flags ---
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true(),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class WatchlistRecord(Base):
    """A single watchlist entry: one collected symbol with tags / note / order.

    The watchlist is a single pool (not multiple named groups); the ``tags``
    JSON array is the categorization mechanism. ``symbol`` is canonical
    ``CODE.EXCHANGE`` and is unique across the table (a symbol is watched at
    most once). IDs are ``wl-<hex12>``. Tags use a JSON array column rather
    than an m2m table, following the repository convention (see
    ``AccountRecord.mock_positions``). No-backcompat: created fresh, no shim.
    """

    __tablename__ = "watchlist_entries"
    __table_args__ = (
        UniqueConstraint("symbol", name="uq_watchlist_entries_symbol"),
        Index("ix_watchlist_entries_sort_order", "sort_order"),
        Index("ix_watchlist_entries_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # wl-<hex12>
    symbol: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # JSON server_default behaves inconsistently across SQLite/Postgres, so the
    # ORM fills [] via default=list; the column itself is NOT NULL.
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class MonitorRuleRecord(Base):
    """A standalone realtime monitoring rule (盯盘规则).

    Unlike ``TaskTrigger`` (which is Task-owned and only fires while the parent
    task is ``running``), a monitor rule is a first-class, stock-scoped entity
    evaluated tick-by-tick by the ``MonitorDaemon`` against the realtime quote
    stream. It binds three things:

    - scope: which symbols to watch — ``scope_kind`` ('watchlist_tag' | 'symbols')
      + ``scope_json`` ({"tag": ...} or {"symbols": [...]}).
    - condition: a declarative AND/OR ``condition_json`` tree whose leaves are
      preset detectors (limit_up / limit_up_seal_shrink / ...) or field-threshold
      predicates. Validated by ``doyoutrade.monitoring.conditions``; this layer
      never imports the validator (dumb persistence).
    - delivery: ``delivery_json`` reuses the EXACT shape of
      ``task_triggers.delivery_json`` ({mode, target:{kind, channel_id, chat_id,
      session_id}}) so the trigger delivery target-resolution applies unchanged.

    ``cooldown_seconds`` is the per-rule minimum gap between two alerts for the
    same (rule, symbol, condition); the daemon also gates on a rising-edge
    transition so a sealed limit-up board does not re-alert every tick.
    """

    __tablename__ = "monitor_rules"
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('watchlist_tag', 'symbols')",
            name="ck_monitor_rules_scope_kind",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'error')",
            name="ck_monitor_rules_status",
        ),
        Index("ix_monitor_rules_active", "enabled", "status"),
        Index("ix_monitor_rules_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # mon-<hex12>
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")

    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # JSON server_default behaves inconsistently across SQLite/Postgres, so the
    # ORM fills {} via default=dict; the columns themselves are NOT NULL.
    scope_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    condition_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # delivery axis (None = no push) — same shape as task_triggers.delivery_json.
    delivery_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class MonitorAlertRecord(Base):
    """One fired monitor alert (盯盘命中历史) — also the durable dedup/cooldown source.

    Appended every time a rule's condition tree triggers for a symbol AND passes
    the daemon's rising-edge + cooldown gates. ``run_id`` is the per-fire run id
    threaded into the ``debug_sessions`` (session_type='monitor') / span chain so
    a fire is reachable by run_id exactly like a cycle run. ``transition_key``
    distinguishes board state transitions within a day (e.g. an open→reseal→open
    sequence fires twice with different keys); the daemon owns its value. High
    write volume → integer autoincrement PK (this row is never addressed by a
    prefixed id externally).
    """

    __tablename__ = "monitor_alerts"
    __table_args__ = (
        Index("ix_monitor_alerts_rule_symbol", "monitor_rule_id", "symbol"),
        Index(
            "ix_monitor_alerts_dedup",
            "monitor_rule_id",
            "symbol",
            "condition_name",
            "triggered_at",
        ),
        Index("ix_monitor_alerts_triggered_at", "triggered_at"),
        Index("ix_monitor_alerts_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    monitor_rule_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("monitor_rules.id", ondelete="CASCADE"),
        nullable=False,
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    condition_name: Mapped[str] = mapped_column(String(64), nullable=False)
    transition_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    triggered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    diagnostics_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    delivery_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class CronJobRunRecord(Base):
    """Per-firing record for a cron job, capturing pre-action and agent run details."""

    __tablename__ = "cron_job_runs"
    __table_args__ = (
        Index("ix_cron_job_runs_job_id_fired_at", "job_id", "fired_at"),
        Index("ix_cron_job_runs_pre_run_id", "pre_run_id"),
        Index("ix_cron_job_runs_trace_id", "trace_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cron_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    fired_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    # OpenTelemetry trace_id of the ``cron.job.fire`` span (32-char lowercase
    # hex), captured at fire time so an operator with only a trace_id can
    # reverse-resolve which cron firing produced it. Null for legacy rows and
    # for fires where tracing was a no-op (all-zero / invalid trace id).
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pre_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pre_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pre_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pre_debug_session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pre_result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    pre_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # New (Task-3 pipeline): which JobTaskExecutor kind handled the fire,
    # and what happened to the user-facing push. Null for legacy rows that
    # ran through the pre_action-only path.
    cron_task_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class CachedBarRecord(Base):
    """Persistent OHLCV bar cache, keyed by (provider, symbol, interval, adjust, timestamp).

    Provider + adjust are part of the PK so that bars fetched with different 复权 modes
    can co-exist without silently overwriting each other — 复权断崖 can misleadingly
    affect technical indicators like SMA crossovers if the wrong mode is served.
    """

    __tablename__ = "cached_bars"
    __table_args__ = (
        Index("ix_cached_bars_range_lookup", "provider", "symbol", "interval", "adjust", "bar_timestamp"),
    )

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval: Mapped[str] = mapped_column(String(16), primary_key=True)
    adjust: Mapped[str] = mapped_column(
        String(16), primary_key=True, default=DEFAULT_BAR_ADJUST
    )
    bar_timestamp: Mapped[str] = mapped_column(String(32), primary_key=True)
    open_price: Mapped[float] = mapped_column(Float, nullable=False)
    high_price: Mapped[float] = mapped_column(Float, nullable=False)
    low_price: Mapped[float] = mapped_column(Float, nullable=False)
    close_price: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class CachedBarRangeRecord(Base):
    """Coverage records: which (start, end) windows the upstream has already been asked for.

    Decoupled from :class:`CachedBarRecord` because an empty range
    (weekend / holiday block) is itself a meaningful cache hit — a bar
    cache that only stores bars would re-hit upstream every time a
    strategy queries a non-trading-day window. ``CachedBarsRepository``
    merges overlapping / adjacent ranges into the smallest set that
    covers the same calendar interval.

    The ``adjust`` column distinguishes different 复权 modes (none/qfq/hfq)
    so that cached data for one mode does not serve queries for another mode.
    """

    __tablename__ = "cached_bar_ranges"
    __table_args__ = (
        Index(
            "ix_cached_bar_ranges_lookup",
            "provider",
            "symbol",
            "interval",
            "adjust",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    interval: Mapped[str] = mapped_column(String(16), nullable=False)
    adjust: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DEFAULT_BAR_ADJUST
    )
    range_start: Mapped[str] = mapped_column(String(32), nullable=False)
    range_end: Mapped[str] = mapped_column(String(32), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class CachedBarSuspensionRecord(Base):
    """Per-symbol trading-day **suspensions** captured alongside the bar cache.

    A suspended day (baostock ``tradestatus==0`` / blank volume) has no
    tradeable bar, so :class:`CachedBarRecord` deliberately stores none for
    it — but the *fact that the symbol was halted* (rather than the upstream
    simply lacking the row) is the only signal that lets the backtest mark
    overlay tell a genuine halt apart from a data gap. Without it,
    ``merge_simulated_bar_marks_into_market`` cannot decide whether a missing
    current-day bar should block a buy (halt) or carry forward the last close
    (gap) — and on a warm cache the transient
    ``BaostockDataProvider.last_suspended_days`` attribute is gone (the fetch
    never re-ran), so the signal must be persisted next to the bars it
    explains.

    Keyed exactly like :class:`CachedBarRecord` (``provider`` + ``adjust`` in
    the PK) so halts recorded under one source / 复权 mode never masquerade as
    another's.
    """

    __tablename__ = "cached_bar_suspensions"
    __table_args__ = (
        Index(
            "ix_cached_bar_suspensions_lookup",
            "provider",
            "symbol",
            "interval",
            "adjust",
            "suspended_day",
        ),
    )

    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval: Mapped[str] = mapped_column(String(16), primary_key=True)
    adjust: Mapped[str] = mapped_column(
        String(16), primary_key=True, default=DEFAULT_BAR_ADJUST
    )
    suspended_day: Mapped[str] = mapped_column(String(16), primary_key=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class MarketBarRecord(Base):
    """Local market-warehouse OHLCV bars (SQLite or TimescaleDB) keyed by source, adjust policy, interval, and time."""

    __tablename__ = "market_bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval: Mapped[str] = mapped_column(String(16), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    adjust: Mapped[str] = mapped_column(String(16), primary_key=True)
    bar_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open_price: Mapped[float] = mapped_column(Float, nullable=False)
    high_price: Mapped[float] = mapped_column(Float, nullable=False)
    low_price: Mapped[float] = mapped_column(Float, nullable=False)
    close_price: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


Index(
    "ix_market_bars_symbol_interval_adjust_time",
    MarketBarRecord.symbol,
    MarketBarRecord.interval,
    MarketBarRecord.adjust,
    MarketBarRecord.bar_time.desc(),
)
Index(
    "ix_market_bars_provider_symbol_interval_adjust_time",
    MarketBarRecord.provider,
    MarketBarRecord.symbol,
    MarketBarRecord.interval,
    MarketBarRecord.adjust,
    MarketBarRecord.bar_time.desc(),
)


class MarketBarSyncStateRecord(Base):
    __tablename__ = "market_bar_sync_state"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval: Mapped[str] = mapped_column(String(16), primary_key=True)
    provider: Mapped[str] = mapped_column(String(16), primary_key=True)
    adjust: Mapped[str] = mapped_column(String(16), primary_key=True)
    target_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    target_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    covered_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    covered_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")


class AssistantMessageRecord(Base):
    __tablename__ = "assistant_messages"
    __table_args__ = (
        Index("ix_assistant_messages_session_created", "session_id", "created_at"),
    )

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    linked_attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AssistantEventRecord(Base):
    __tablename__ = "assistant_events"
    __table_args__ = (
        Index("ix_assistant_events_session_id", "session_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class AssistantLoadedSkillRecord(Base):
    """SKILL.md content loaded by an assistant session via load_skill.

    Persisted so that context compaction (which folds old tool_results into
    a summary boundary) does not drop the skill content. After a compaction
    the assistant service rebuilds a <system-reminder> message from these
    rows and re-injects it before the next model invocation, so the agent
    can keep using the skill without re-invoking load_skill.

    Composite PK on (session_id, skill_name) gives natural upsert semantics
    when the same skill is re-loaded inside one session. FK CASCADE to
    assistant_sessions ensures rows disappear with the session.
    """

    __tablename__ = "assistant_loaded_skills"
    __table_args__ = (
        Index("ix_assistant_loaded_skills_session_id", "session_id"),
    )

    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("assistant_sessions.session_id", ondelete="CASCADE"),
        primary_key=True,
    )
    skill_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    skill_path: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class AssistantJobWatchRecord(Base):
    """A registered wake-up: when a long-running job (backtest) reaches a
    terminal status, the JobWatchService composes a reply and delivers it
    into the originating assistant session.

    Created by the in-process ``watch_job`` tool; consumed by
    ``doyoutrade/assistant/job_watcher.py``. ``status`` lifecycle:
    ``pending`` → ``fired`` (delivered) / ``failed`` (lookup or delivery
    broke; ``last_error`` carries why) / ``cancelled``.
    """

    __tablename__ = "assistant_job_watches"
    __table_args__ = (
        Index("ix_assistant_job_watches_status", "status"),
        Index("ix_assistant_job_watches_session_id", "session_id"),
        Index("ix_assistant_job_watches_job_id", "job_id"),
    )

    watch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("assistant_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="backtest")
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
    fired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SwarmRunRecord(Base):
    """一次 Swarm preset 执行的聚合根。

    每个 run 对应一张 preset 团队的多智能体协作；任务存于 SwarmTaskRecord，
    实时事件存于 SwarmEventRecord（供 SSE 流式推送）。
    """

    __tablename__ = "swarm_runs"
    __table_args__ = (
        Index("ix_swarm_runs_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    preset_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    user_vars: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    final_report: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SwarmTaskRecord(Base):
    """Swarm DAG 中的一个任务节点（绑定到某个 agent role）。"""

    __tablename__ = "swarm_tasks"
    __table_args__ = (
        Index("ix_swarm_tasks_run_id", "run_id"),
        UniqueConstraint("run_id", "task_id", name="uq_swarm_tasks_run_task"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("swarm_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    depends_on: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    input_from: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SwarmEventRecord(Base):
    """Swarm 运行事件日志，对标 AssistantEventRecord，供 SSE 按 after_id 分页。"""

    __tablename__ = "swarm_events"
    __table_args__ = (
        Index("ix_swarm_events_run_id", "run_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("swarm_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class DecisionSignalRecord(Base):
    """One persisted decision signal (决策信号) — the signal-lifecycle aggregate root.

    Sources (``source`` column):

    - ``strategy`` — live strategy runner output (reserved; no producer yet).
    - ``backtest`` — extracted from a finished backtest run's executed fills
      (:meth:`doyoutrade.platform.service.TradingPlatformService._persist_decision_signals_from_run`).
    - ``assistant`` — the ``record_decision_signal`` assistant tool.

    Attribution columns are soft references (String + Index, no DB FK) matching
    the repo-wide convention for ``cycle_runs``-adjacent tables (see
    ``TradeFillRecord.cycle_run_id``): ``run_id`` (backtest job / cycle run id),
    ``cycle_run_id`` (the specific ``cycle_runs.run_id`` that produced the
    signal), ``trace_id`` (OpenTelemetry), ``session_id`` (assistant session).

    Price columns are decimal Strings — same convention as
    ``trade_fills.price`` — never floats, so money survives round-trips exactly.

    ``dedupe_key`` is repository-generated from
    ``(run_id or trace_id or session_id, symbol, action, horizon)`` and unique,
    making ``create_if_absent`` idempotent across retries / re-finalization.
    """

    __tablename__ = "decision_signals"
    __table_args__ = (
        CheckConstraint(
            "source IN ('strategy', 'backtest', 'assistant')",
            name="ck_decision_signals_source",
        ),
        CheckConstraint(
            "action IN ('buy', 'sell', 'hold', 'add', 'reduce', 'watch', "
            "'take_profit', 'stop_loss')",
            name="ck_decision_signals_action",
        ),
        CheckConstraint(
            "status IN ('active', 'expired', 'invalidated', 'evaluated')",
            name="ck_decision_signals_status",
        ),
        UniqueConstraint("dedupe_key", name="uq_decision_signals_dedupe_key"),
        Index("ix_decision_signals_run_id", "run_id"),
        Index("ix_decision_signals_task_id", "task_id"),
        Index("ix_decision_signals_symbol_created", "symbol", "created_at"),
        Index("ix_decision_signals_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # dsig-<hex12>
    # Attribution (all soft references; nullable because sources differ in
    # which identifiers exist at record time).
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    cycle_run_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    horizon: Mapped[str] = mapped_column(String(16), nullable=False, default="5d")
    # Decimal strings (same as trade_fills.price/amount) — display + eval only,
    # never fed to the execution path as floats.
    entry_low: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_high: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_loss: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_price: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


class DecisionSignalOutcomeRecord(Base):
    """Backtest-verification outcome for one (signal, horizon, engine_version).

    Written by :mod:`doyoutrade.backtest.decision_signal_eval` consumers.
    Real FK to the parent signal (child→parent FK is allowed by convention;
    only ``cycle_runs`` references stay soft). ``UniqueConstraint`` makes
    ``upsert_outcome`` idempotent so re-evaluation replaces rather than
    duplicates.
    """

    __tablename__ = "decision_signal_outcomes"
    __table_args__ = (
        CheckConstraint(
            "outcome IN ('hit', 'miss', 'neutral')",
            name="ck_decision_signal_outcomes_outcome",
        ),
        UniqueConstraint(
            "signal_id",
            "horizon",
            "engine_version",
            name="uq_decision_signal_outcomes_key",
        ),
        Index("ix_decision_signal_outcomes_signal_id", "signal_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("decision_signals.id", ondelete="CASCADE"),
        nullable=False,
    )
    horizon: Mapped[str] = mapped_column(String(16), nullable=False)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False, default="v1")
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    direction_expected: Mapped[str] = mapped_column(String(8), nullable=False)
    direction_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    anchor_date: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    eval_window_days: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_gain_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
