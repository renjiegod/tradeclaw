from __future__ import annotations

from typing import List

from doyoutrade.core.models import AccountSnapshot, PositionSnapshot


class ZeroAccountReader:
    """Fixed empty account (e.g. public market data stacks without a broker)."""

    portfolio_source: str = "ledger"

    async def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(cash=0, equity=0)

    async def get_positions(self) -> List[PositionSnapshot]:
        return []
