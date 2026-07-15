import unittest

from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.data.providers import (
    InMemoryHistoricalDataProvider,
    MarketDataNormalizer,
    RealtimeMarketFeed,
)
from doyoutrade.core.models import Bar


class DataLayerTests(unittest.TestCase):
    def test_historical_provider_respects_as_of_time(self):
        provider = InMemoryHistoricalDataProvider(
            {
                "600000.SH": [
                    Bar(symbol="600000.SH", timestamp="2026-01-01T09:31:00", open=10, high=11, low=9.8, close=10.5, volume=1000),
                    Bar(symbol="600000.SH", timestamp="2026-01-01T09:32:00", open=10.5, high=10.8, low=10.4, close=10.6, volume=900),
                ]
            }
        )

        bars = provider.get_bars("600000.SH", start_time="2026-01-01T09:30:00", end_time="2026-01-01T09:35:00", as_of_time="2026-01-01T09:31:30")

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].timestamp, "2026-01-01T09:31:00")

    def test_normalizer_standardizes_symbol(self):
        normalizer = MarketDataNormalizer(default_market="SH")

        symbol = normalizer.normalize_symbol("600000")

        self.assertEqual(symbol, "600000.SH")

    def test_realtime_feed_dispatches_to_subscribers(self):
        feed = RealtimeMarketFeed()
        events = []

        feed.subscribe("600000.SH", events.append)
        feed.publish_quote("600000.SH", 10.2, "2026-01-01T09:31:00")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].symbol, "600000.SH")
        self.assertEqual(events[0].price, 10.2)

    def test_mock_trading_data_provider_get_bars_empty_by_default(self):
        import asyncio

        async def _run():
            dp = MockTradingDataProvider()
            return await dp.get_bars("600000.SH", "2026-01-01", "2026-01-10")

        self.assertEqual(asyncio.run(_run()), [])

    def test_mock_trading_data_provider_get_bars_normalizes_timestamps(self):
        import asyncio

        bars_in = [
            Bar(
                symbol="600000.SH",
                timestamp="20260102",
                open=1.0,
                high=1.1,
                low=0.9,
                close=1.05,
                volume=100.0,
            )
        ]

        async def _run():
            dp = MockTradingDataProvider(bars_by_symbol={"600000.SH": bars_in})
            # InMemoryHistoricalDataProvider compares timestamps lexicographically; use compact
            # YYYYMMDD bounds when storing compact bar keys.
            return await dp.get_bars("600000.SH", "20260101", "20260103")

        out = asyncio.run(_run())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].timestamp, "2026-01-02")


if __name__ == "__main__":
    unittest.main()
