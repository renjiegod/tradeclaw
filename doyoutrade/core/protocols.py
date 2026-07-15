from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from doyoutrade.core.cycle_state import CycleRunState
from doyoutrade.core.models import AccountSnapshot, Bar, MarketContext, PositionSnapshot
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST


@runtime_checkable
class TradingDataProvider(Protocol):
    """Async source for market quotes, historical bars, and trading calendar.

    Account cash/equity and positions are provided by :class:`~doyoutrade.account.protocol.AccountReader`.
    """

    async def get_market_context(self) -> MarketContext: ...

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> List[Bar]:
        """Historical OHLCV for *symbol* between *start_time* and *end_time* (inclusive).

        ``interval`` follows qmt-proxy period names (e.g. ``1d``, ``1m``). Returns ``[]``
        when no rows exist. ``Bar.timestamp`` values are normalized (see
        :func:`doyoutrade.data.bar_timestamp.normalize_bar_timestamp`).

        ``adjust`` controls 复权 mode: ``"none"`` (不复权), ``"qfq"`` (前复权), or ``"hfq"`` (后复权).
        Default is ``"qfq"`` so strategy/backtest calculations match the default chart view.
        """
        ...

    async def is_trading_day(self, date: str) -> bool:
        """Return True if *date* (YYYY-MM-DD) is a trading day, False otherwise."""
        ...

    async def get_trading_dates(self, start: str, end: str) -> List[str]:
        """Return all trading dates in [start, end] (YYYY-MM-DD, inclusive)."""
        ...


@runtime_checkable
class UniverseProvider(Protocol):
    """Builds the tradable symbol list for a cycle."""

    async def build_universe(
        self,
        market_context: MarketContext,
        account_snapshot: AccountSnapshot,
        positions: List[PositionSnapshot],
        *,
        cycle_state: CycleRunState | None = None,
    ) -> List[str]: ...
