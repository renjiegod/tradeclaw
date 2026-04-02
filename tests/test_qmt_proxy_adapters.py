import unittest

from tradeclaw.data.qmt_proxy import QmtProxyHistoricalProvider, QmtProxyPortfolioProvider


class _FakeQmtClient:
    def fetch_history(self, symbol, start_time, end_time):
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

    def fetch_account(self):
        return {"cash": 90000.0, "equity": 100000.0}

    def fetch_positions(self):
        return [{"symbol": "600000.SH", "quantity": 100, "cost_price": 9.8}]


class QmtProxyAdapterTests(unittest.TestCase):
    def test_historical_provider_maps_raw_bar(self):
        provider = QmtProxyHistoricalProvider(client=_FakeQmtClient())

        bars = provider.get_bars("600000.SH", "2026-01-01T09:30:00", "2026-01-01T10:00:00")

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "600000.SH")
        self.assertEqual(bars[0].close, 10.2)

    def test_portfolio_provider_maps_account_and_positions(self):
        provider = QmtProxyPortfolioProvider(client=_FakeQmtClient())

        account = provider.get_account_snapshot()
        positions = provider.get_positions()

        self.assertEqual(account.equity, 100000.0)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "600000.SH")


if __name__ == "__main__":
    unittest.main()
