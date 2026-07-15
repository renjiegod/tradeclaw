from __future__ import annotations

from typing import Any, List

from doyoutrade.core.models import AccountSnapshot, PositionSnapshot


class StoreBackedAccountReader:
    """Delegates to an object that implements async account/position accessors.

    Used with :class:`~doyoutrade.data.mock_provider.MockTradingDataProvider` so the same
    in-memory store backs mock market ticks and mock account state.
    """

    portfolio_source: str = "ledger"

    def __init__(self, store: Any):
        self._store = store

    async def get_account_snapshot(self) -> AccountSnapshot:
        return await self._store.get_account_snapshot()

    async def get_positions(self) -> List[PositionSnapshot]:
        return await self._store.get_positions()
