"""Protocol the worker uses to talk to the signal+sizing layer.

A :class:`SignalGeneratorProtocol` implementation owns end-to-end signal
generation **and** sizing: it returns already-sized
:class:`~doyoutrade.core.models.OrderIntent` rows so the worker doesn't need
to know whether they came from a :class:`Strategy` + :class:`PositionManager`
pipeline or a custom runner.

The canonical implementation is
:class:`doyoutrade.strategy_sdk.runner.StrategyRunner`. New code should
target this protocol directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Dict, List, Protocol, runtime_checkable

from doyoutrade.core.cycle_state import CycleRunState
from doyoutrade.core.models import (
    AccountSnapshot,
    MarketContext,
    OrderIntent,
    PositionSnapshot,
    TaskBudgetSnapshot,
)


@dataclass
class SignalGenerationContext:
    """Per-cycle input bundle to a :class:`SignalGeneratorProtocol`.

    Mirrors the read-only inputs the worker has assembled by the start of the
    strategy phase. ``cycle_state`` carries ``run_id`` / ``trace_id`` /
    ``cycle_time`` for the runner to forward into ``SignalContext`` and
    persistence.
    """

    market_context: MarketContext
    universe: List[str]
    account_snapshot: AccountSnapshot
    positions: List[PositionSnapshot]
    task_budget_snapshot: TaskBudgetSnapshot | None = None
    cycle_state: CycleRunState | None = None

    # --- output channel (populated by the generator, read by the worker) ---
    # Per-symbol decision factors the strategy produced this cycle, keyed by
    # symbol → ``Signal.to_dict()`` (direction / tag / rationale / diagnostics).
    # The worker persists this into ``cycle_runs.details.signal_diagnostics`` so
    # downstream consumers (e.g. the strategy_signal_alert cron executor's
    # ``no_signal_mode='full'`` push) can explain *why* a cycle produced no
    # actionable order without re-querying debug spans. ``None`` until the
    # generator runs; an empty dict means "ran, but no per-symbol signals".
    signal_diagnostics: Dict[str, Any] | None = None


@runtime_checkable
class SignalGeneratorProtocol(Protocol):
    """Single-method contract for signal generation + position sizing.

    Implementations return :class:`OrderIntent` rows whose ``quantity`` /
    ``amount`` are final — the worker does not adjust them downstream.
    Returning ``[]`` is a valid "no orders this cycle" outcome.
    """

    def generate_intents(
        self, ctx: SignalGenerationContext
    ) -> List[OrderIntent] | Awaitable[List[OrderIntent]]:
        ...


__all__ = ["SignalGenerationContext", "SignalGeneratorProtocol"]
