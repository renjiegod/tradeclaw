from __future__ import annotations

from typing import List, Protocol, runtime_checkable

from doyoutrade.core.models import AccountSnapshot, PositionSnapshot


@runtime_checkable
class AccountReader(Protocol):
    """Async source for account cash/equity and positions (not market data)."""

    async def get_account_snapshot(self) -> AccountSnapshot: ...

    async def get_positions(self) -> List[PositionSnapshot]: ...
