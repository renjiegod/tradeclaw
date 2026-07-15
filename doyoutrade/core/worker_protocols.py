"""Structural types for :class:`~doyoutrade.core.worker.TradingWorker` dependencies.

These :class:`typing.Protocol` definitions describe only what ``run_cycle`` and
helpers call, so wrappers (debug patches, test fakes) stay substitutable without
inheritance from concrete classes.

The signal generation contract lives in
:mod:`doyoutrade.core.signal_generator_protocol`.
"""

from __future__ import annotations

from typing import Any, Awaitable, Protocol

from doyoutrade.core.cycle_state import CycleRunState
from doyoutrade.core.models import (
    AccountSnapshot,
    MarketContext,
    OrderIntent,
    PositionSnapshot,
    RiskDecision,
    ValidationResult,
)
from doyoutrade.execution.approval import ApprovalResult


class IntentValidatorProtocol(Protocol):
    def validate(self, intent: OrderIntent) -> ValidationResult:
        ...


class RiskEngineProtocol(Protocol):
    def evaluate(
        self,
        intents: list[OrderIntent],
        account_snapshot: AccountSnapshot,
        positions: list[PositionSnapshot],
        *,
        cycle_state: CycleRunState | None = None,
        settlement_mode: str = "t0",
    ) -> list[RiskDecision]:
        ...


class ApprovalGateProtocol(Protocol):
    def request(
        self,
        intent: OrderIntent,
        account_snapshot: AccountSnapshot | None = None,
        market_context: MarketContext | None = None,
        mode: str = "paper",
        *,
        cycle_state: CycleRunState | None = None,
        min_notional_for_approval: float | None = None,
        timeout_seconds: int | None = None,
        account_id: str | None = None,
    ) -> ApprovalResult | Awaitable[ApprovalResult]:
        ...


class ExecutionAdapterProtocol(Protocol):
    def submit_intent(
        self,
        intent: OrderIntent,
        *,
        cycle_state: CycleRunState | None = None,
        market_context: MarketContext | None = None,
    ) -> Any | Awaitable[Any]:
        ...
