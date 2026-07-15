"""StrategyContext — the ``ctx`` argument passed to every Strategy hook.

This is the strategy's view of its world for one cycle. It bundles:

- **Basic info**: ``ctx.symbol`` (current evaluation target), ``ctx.now``
  (logical time), ``ctx.run_id`` / ``ctx.trace_id`` (vertical IDs that
  flow into trade_fills / debug events / model invocations).
- **Account / position views**: ``ctx.position`` / ``ctx.account`` —
  read-only projections of the worker's snapshots scoped to the current
  symbol.
- **Parameters**: ``ctx.params`` — free-form parameter overrides resolved
  by the runner. Tunable parameters declared as ``IntParameter`` etc. on
  the strategy class are bound separately via ``self.<param>.value``.
- **Data access**: ``ctx.dp`` — the only sanctioned way to fetch market
  data inside a strategy. Every method on ``ctx.dp`` is auto-instrumented
  with an OTel span + debug event + cycle cache + typed error.
- **Universe**: ``ctx.universe`` — the full universe of the cycle, in case
  the strategy needs to know its siblings for ranking / pairs logic.

The context is constructed once per (cycle × symbol) by
:class:`doyoutrade.strategy_sdk.runner.StrategyRunner` before invoking
``populate_indicators`` and ``on_bar``. It is immutable from the strategy's
perspective — strategies must not mutate ``ctx.dp.cache`` or any other
internal field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Mapping

from doyoutrade.core.models import AccountSnapshot, PositionSnapshot

if TYPE_CHECKING:
    from doyoutrade.strategy_sdk.data_provider import DataProvider


@dataclass(frozen=True)
class PositionView:
    """Read-only projection of the strategy's current position for ``ctx.symbol``.

    ``quantity == 0`` → flat. ``is_long`` is the canonical predicate for
    "do I have an open position?" — strategies should branch on this rather
    than poking at quantity directly.

    ``current_profit`` is the unrealized return as a fraction (0.05 = +5%).
    Computed from market_price vs cost_price; falls back to 0.0 when either
    is unknown.
    """

    symbol: str
    quantity: float = 0.0
    cost_price: Decimal = field(default_factory=lambda: Decimal("0"))
    market_price: float | None = None
    market_value: float | None = None

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def current_profit(self) -> float:
        if self.market_price is None or self.cost_price == 0:
            return 0.0
        cost = float(self.cost_price)
        if cost <= 0:
            return 0.0
        return (self.market_price - cost) / cost

    @classmethod
    def from_snapshot(
        cls, symbol: str, snapshot: PositionSnapshot | None
    ) -> "PositionView":
        if snapshot is None:
            return cls(symbol=symbol)
        return cls(
            symbol=snapshot.symbol,
            quantity=float(snapshot.quantity),
            cost_price=snapshot.cost_price,
            market_price=snapshot.market_price,
            market_value=snapshot.market_value,
        )


@dataclass(frozen=True)
class AccountView:
    """Read-only projection of the account snapshot for the strategy."""

    cash: Decimal
    equity: Decimal

    @classmethod
    def from_snapshot(cls, snapshot: AccountSnapshot) -> "AccountView":
        return cls(cash=snapshot.cash, equity=snapshot.equity)


@dataclass(frozen=True)
class StrategyContext:
    """The ``ctx`` argument passed to every Strategy hook.

    All fields are immutable (frozen dataclass). The data provider
    (``self.dp``) holds its own cycle-scoped mutable cache but exposes only
    methods, never raw cache access, to the strategy.
    """

    symbol: str
    now: datetime
    run_id: str
    trace_id: str
    universe: tuple[str, ...]
    position: PositionView
    account: AccountView
    params: Mapping[str, Any]
    dp: "DataProvider"

    @property
    def is_backtest(self) -> bool:
        """True when running inside a backtest (live/dry methods will raise).

        Set by the runner based on the worker's execution mode. Strategy
        code can use this to gate optional logic that only makes sense
        live (e.g. reading orderbook depth), though preferring the
        ``ctx.dp.ticker()`` / ``orderbook()`` raises with ``live_only_method``
        is more explicit.
        """
        return getattr(self.dp, "is_backtest", False)


__all__ = ["AccountView", "PositionView", "StrategyContext"]
