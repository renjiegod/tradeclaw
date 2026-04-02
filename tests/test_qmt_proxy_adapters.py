import unittest

from tradeclaw.data.qmt_proxy import (
    QmtLiveDataProvider,
    QmtProxyHistoricalProvider,
    QmtProxyPortfolioProvider,
)


class _FakeQmtClient:
    async def fetch_history(self, symbol, start_time, end_time):
        return [
            {
                "symbol": symbol,
                "ts": "2026-01-01T09:31:00",
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "volume": 1000,
            }
        ]

    async def fetch_account(self):
        return {"cash": 90000.0, "equity": 100000.0}

    async def fetch_positions(self):
        return [{"symbol": "600000.SH", "quantity": 100, "cost_price": 9.8}]

    async def fetch_latest_quotes(self, symbols):
        return [{"symbol": symbols[0], "price": 10.2}]


class QmtProxyAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_provider_maps_raw_bar(self):
        provider = QmtProxyHistoricalProvider(client=_FakeQmtClient())

        bars = await provider.get_bars("600000.SH", "2026-01-01T09:30:00", "2026-01-01T10:00:00")

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "600000.SH")
        self.assertEqual(bars[0].close, 10.2)

    async def test_portfolio_provider_maps_account_and_positions(self):
        provider = QmtProxyPortfolioProvider(client=_FakeQmtClient())

        account = await provider.get_account_snapshot()
        positions = await provider.get_positions()

        self.assertEqual(account.equity, 100000.0)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "600000.SH")

    async def test_live_provider_maps_latest_quotes_into_market_context(self):
        provider = QmtLiveDataProvider(client=_FakeQmtClient(), symbols=["600000.SH"])

        market_context = await provider.get_market_context()

        self.assertEqual(market_context.symbol_to_price["600000.SH"], 10.2)


if __name__ == "__main__":
    unittest.main()
