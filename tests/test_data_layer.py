import unittest

from tradeclaw.data.providers import (
    InMemoryHistoricalDataProvider,
    MarketDataNormalizer,
    RealtimeMarketFeed,
)
from tradeclaw.domain.models import Bar


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


if __name__ == "__main__":
    unittest.main()
