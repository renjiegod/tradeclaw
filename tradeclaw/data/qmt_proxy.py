from __future__ import annotations

from typing import List

from tradeclaw.domain.models import AccountSnapshot, Bar, MarketContext, PositionSnapshot


class QmtProxyHistoricalProvider:
    """Adapter that maps qmt-proxy historical payloads into internal Bar models."""

    def __init__(self, client):
        self.client = client

    async def get_bars(self, symbol: str, start_time: str, end_time: str) -> List[Bar]:
        rows = await self.client.fetch_history(
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
        )
        bars: List[Bar] = []
        for row in rows:
            bars.append(
                Bar(
                    symbol=row["symbol"],
                    timestamp=row["ts"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
        return bars


class QmtProxyPortfolioProvider:
    """Adapter that maps qmt-proxy account/positions into internal snapshots."""

    def __init__(self, client):
        self.client = client

    async def get_account_snapshot(self) -> AccountSnapshot:
        account = await self.client.fetch_account()
        return AccountSnapshot(cash=float(account["cash"]), equity=float(account["equity"]))

    async def get_positions(self) -> List[PositionSnapshot]:
        rows = await self.client.fetch_positions()
        positions: List[PositionSnapshot] = []
        for row in rows:
            positions.append(
                PositionSnapshot(
                    symbol=row["symbol"],
                    quantity=float(row["quantity"]),
                    cost_price=float(row["cost_price"]),
                )
            )
        return positions


class QmtLiveDataProvider:
    """Live data provider that exposes worker-friendly market/account interfaces."""

    def __init__(self, client, symbols):
        self.client = client
        self.symbols = list(symbols)
        self.portfolio_provider = QmtProxyPortfolioProvider(client=client)

    async def get_market_context(self) -> MarketContext:
        quotes = await self.client.fetch_latest_quotes(self.symbols)
        symbol_to_price = {}
        for quote in quotes:
            symbol = quote["symbol"]
            price = quote.get("price")
            if price is None:
                price = quote.get("last")
            symbol_to_price[symbol] = float(price)
        return MarketContext(symbol_to_price=symbol_to_price)

    async def get_account_snapshot(self) -> AccountSnapshot:
        return await self.portfolio_provider.get_account_snapshot()

    async def get_positions(self) -> List[PositionSnapshot]:
        return await self.portfolio_provider.get_positions()

    async def aclose(self):
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()
