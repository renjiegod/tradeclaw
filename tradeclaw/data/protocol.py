from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from tradeclaw.domain.models import AccountSnapshot, MarketContext, PositionSnapshot


@runtime_checkable
class TradingDataProvider(Protocol):
    """Async source for market quotes, account cash/equity, and positions."""

    async def get_market_context(self) -> MarketContext: ...

    async def get_account_snapshot(self) -> AccountSnapshot: ...

    async def get_positions(self) -> List[PositionSnapshot]: ...


@runtime_checkable
class UniverseProvider(Protocol):
    """Builds the tradable symbol list for a cycle."""

    async def build_universe(self, market_context, account_snapshot, positions) -> List[str]: ...
