from __future__ import annotations

from typing import List

from tradeclaw.domain.models import AccountSnapshot, MarketContext, PositionSnapshot


class MockTradingDataProvider:
    """Deterministic in-process quotes and portfolio for tests and dry runs."""

    def __init__(
        self,
        symbol_to_price: dict[str, float] | None = None,
        cash: float = 100_000.0,
        equity: float = 100_000.0,
        positions: List[PositionSnapshot] | None = None,
    ):
        self._symbol_to_price = dict(symbol_to_price or {"600000.SH": 10.0, "601318.SH": 50.0})
        self._cash = float(cash)
        self._equity = float(equity)
        self._positions = list(
            positions
            if positions is not None
            else [PositionSnapshot(symbol="600000.SH", quantity=0, cost_price=0.0)]
        )

    async def get_market_context(self) -> MarketContext:
        return MarketContext(symbol_to_price=dict(self._symbol_to_price))

    async def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(cash=self._cash, equity=self._equity)

    async def get_positions(self) -> List[PositionSnapshot]:
        return list(self._positions)


class StaticUniverseProvider:
    """Universe is exactly the configured symbol list (same idea as QmtUniverseProvider)."""

    def __init__(self, symbols: List[str]):
        self._symbols = list(symbols)

    async def build_universe(self, *_):
        return list(self._symbols)
