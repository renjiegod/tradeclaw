import unittest

from qmt_proxy_sdk.models.data import TradingCalendarResponse

from doyoutrade.account import QmtAccountReader, StoreBackedAccountReader
from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.data.qmt_proxy import QmtLiveDataProvider, QmtProxyHistoricalProvider


class _FakeQmtClient:
    async def fetch_bars(self, symbol, start_time, end_time, interval="1m", *, adjust="qfq"):
        return [
            {
                "symbol": symbol,
                "ts": "2026-01-01T09:31:00",
                "open": 10.0,
                "high": 10.5,
                "low": 9.9,
                "close": 10.2,
                "volume": 1000,
                "amount": 1_020_000.0,
            }
        ]

    async def fetch_account(self):
        return {"cash": 90000.0, "equity": 100000.0}

    async def fetch_positions(self):
        return [
            {
                "symbol": "600000.SH",
                "quantity": 100,
                "cost_price": 9.8,
                "market_price": 10.0,
                "market_value": 1000.0,
                "available": 50.0,
                "frozen": 0.0,
                "name": "浦发银行",
            }
        ]

    async def fetch_latest_quotes(self, symbols):
        return [{"symbol": symbols[0], "price": 10.2}]


class QmtProxyAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_provider_maps_raw_bar(self):
        provider = QmtProxyHistoricalProvider(client=_FakeQmtClient())

        bars = await provider.get_bars("600000.SH", "2026-01-01T09:30:00", "2026-01-01T10:00:00")

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "600000.SH")
        self.assertEqual(bars[0].close, 10.2)
        self.assertEqual(bars[0].timestamp, "2026-01-01T09:31:00")
        self.assertEqual(bars[0].amount, 1_020_000.0)

    async def test_historical_provider_normalizes_compact_daily_timestamp(self):
        class _DailyClient(_FakeQmtClient):
            async def fetch_bars(self, symbol, start_time, end_time, interval="1m", *, adjust="qfq"):
                return [
                    {
                        "symbol": symbol,
                        "ts": "20260102",
                        "open": 1.0,
                        "high": 1.1,
                        "low": 0.9,
                        "close": 1.05,
                        "volume": 500,
                        "amount": 525.0,
                    }
                ]

        provider = QmtProxyHistoricalProvider(client=_DailyClient())
        bars = await provider.get_bars("600000.SH", "2026-01-01", "2026-01-03", interval="1d")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].timestamp, "2026-01-02")
        self.assertEqual(bars[0].amount, 525.0)

    async def test_live_provider_get_bars_delegates_to_history(self):
        provider = QmtLiveDataProvider(client=_FakeQmtClient(), symbols=["600000.SH"])
        bars = await provider.get_bars("600000.SH", "2026-01-01", "2026-01-02")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].timestamp, "2026-01-01T09:31:00")

    async def test_live_provider_trading_day_methods_use_client_calendar(self):
        class _CalClient(_FakeQmtClient):
            async def get_trading_calendar(self, year: int) -> TradingCalendarResponse:
                return TradingCalendarResponse(
                    trading_dates=["2026-04-06", "2026-04-07"],
                    holidays=[],
                    year=year,
                )

        provider = QmtLiveDataProvider(client=_CalClient(), symbols=["600000.SH"])
        self.assertTrue(await provider.is_trading_day("2026-04-06"))
        self.assertFalse(await provider.is_trading_day("2026-04-05"))
        dates = await provider.get_trading_dates("2026-04-05", "2026-04-08")
        self.assertEqual(dates, ["2026-04-06", "2026-04-07"])

    async def test_live_provider_compact_calendar_dates_work_with_iso_range(self):
        class _CalClient(_FakeQmtClient):
            async def get_trading_calendar(self, year: int) -> TradingCalendarResponse:
                return TradingCalendarResponse(
                    trading_dates=["20260401", "20260402", "20260403"],
                    holidays=[],
                    year=year,
                )

        provider = QmtLiveDataProvider(client=_CalClient(), symbols=["600000.SH"])
        self.assertTrue(await provider.is_trading_day("2026-04-03"))
        dates = await provider.get_trading_dates("2026-02-05", "2026-06-05")
        self.assertEqual(dates, ["2026-04-01", "2026-04-02", "2026-04-03"])

    async def test_live_provider_normalizes_calendar_datetime_strings(self):
        class _CalClient(_FakeQmtClient):
            async def get_trading_calendar(self, year: int) -> TradingCalendarResponse:
                return TradingCalendarResponse(
                    trading_dates=["2026-04-06T00:00:00", "2026-04-07"],
                    holidays=[],
                    year=year,
                )

        provider = QmtLiveDataProvider(client=_CalClient(), symbols=["600000.SH"])
        self.assertTrue(await provider.is_trading_day("2026-04-06"))
        dates = await provider.get_trading_dates("2026-04-05", "2026-04-08")
        self.assertEqual(dates, ["2026-04-06", "2026-04-07"])

    async def test_account_reader_maps_account_and_positions(self):
        reader = QmtAccountReader(client=_FakeQmtClient())

        account = await reader.get_account_snapshot()
        positions = await reader.get_positions()

        self.assertEqual(account.equity, 100000.0)
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, "600000.SH")
        self.assertEqual(positions[0].available, 50.0)
        self.assertEqual(positions[0].market_price, 10.0)
        self.assertEqual(positions[0].market_value, 1000.0)
        self.assertEqual(positions[0].name, "浦发银行")

    async def test_live_provider_maps_latest_quotes_into_market_context(self):
        provider = QmtLiveDataProvider(client=_FakeQmtClient(), symbols=["600000.SH"])

        market_context = await provider.get_market_context()

        self.assertEqual(market_context.symbol_to_price["600000.SH"], 10.2)
        self.assertEqual(market_context.symbol_to_tick, {})

    async def test_live_provider_maps_tick_payload_when_present(self):
        class _ClientWithTick(_FakeQmtClient):
            async def fetch_latest_quotes(self, symbols):
                return [
                    {
                        "symbol": symbols[0],
                        "price": 10.2,
                        "last": 10.2,
                        "ts": "2026-01-01T09:31:00",
                        "tick": {
                            "time": "2026-01-01T09:31:00",
                            "last_price": 10.2,
                            "bid_price": [10.19],
                            "ask_price": [10.21],
                        },
                    }
                ]

        provider = QmtLiveDataProvider(client=_ClientWithTick(), symbols=["600000.SH"])
        market_context = await provider.get_market_context()

        self.assertEqual(market_context.symbol_to_tick["600000.SH"]["bid_price"], [10.19])

    async def test_mock_store_reader_uses_local_portfolio_not_proxy(self):
        class _NoAccountClient(_FakeQmtClient):
            async def fetch_account(self):
                raise AssertionError("fetch_account must not be called for store-backed reader")

            async def fetch_positions(self):
                raise AssertionError("fetch_positions must not be called for store-backed reader")

        mock_pf = MockTradingDataProvider(cash=77_777.0, equity=88_888.0)
        reader = StoreBackedAccountReader(mock_pf)
        provider = QmtLiveDataProvider(client=_NoAccountClient(), symbols=["600000.SH"])

        market_context = await provider.get_market_context()
        self.assertEqual(market_context.symbol_to_price["600000.SH"], 10.2)
        self.assertEqual(market_context.symbol_to_tick, {})

        acct = await reader.get_account_snapshot()
        self.assertEqual(acct.cash, 77_777.0)
        # Equity is always MTM from cash + positions (default zero position → cash only).
        self.assertEqual(acct.equity, 77_777.0)


if __name__ == "__main__":
    unittest.main()
