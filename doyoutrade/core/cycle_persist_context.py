"""Context for correlating a worker cycle with tick/debug sessions (run_kind, session id).

Used when :meth:`~doyoutrade.runtime.scheduler.RuntimeScheduler.tick_once` runs multiple
instances without per-invocation arguments to :meth:`~doyoutrade.core.worker.TradingWorker.run_cycle`.
"""

from __future__ import annotations

from contextvars import ContextVar

# "scheduled" | "manual" — set by TradingPlatformService.tick_once around scheduler tick.
current_tick_run_kind: ContextVar[str] = ContextVar("current_tick_run_kind", default="scheduled")

# session id for scheduled/manual tick span export (optional on cycle_runs.session_id).
current_tick_session_id: ContextVar[str | None] = ContextVar("current_tick_session_id", default=None)

# task_triggers.id when the current cycle was fired by a Trigger (run_kind="trigger");
# written to cycle_runs.trigger_id so a Trigger-fired cycle is attributable on the run row
# (trigger_id -> run_id <-> debug_sessions <-> spans <-> model_invocations <-> trade_fills).
current_trigger_id: ContextVar[str | None] = ContextVar("current_trigger_id", default=None)
