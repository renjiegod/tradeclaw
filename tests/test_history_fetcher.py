from __future__ import annotations

import unittest
from datetime import datetime

from doyoutrade.core.models import Bar
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.strategy_sdk.history_fetcher import BarsHistoryFetcher


def _bar(sym: str, ts: str, close: float) -> Bar:
    return Bar(
        symbol=sym,
        timestamp=normalize_bar_timestamp(ts),
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=1_000.0,
    )


class BarsHistoryFetcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_5m_respects_intraday_as_of_timestamp(self) -> None:
        sym = "600000.SH"
        provider = MockTradingDataProvider(
            bars_by_symbol={
                sym: [
                    _bar(sym, "2026-01-05T09:35:00", 10.0),
                    _bar(sym, "2026-01-05T09:40:00", 10.2),
                    _bar(sym, "2026-01-05T09:45:00", 10.4),
                ]
            }
        )
        fetcher = BarsHistoryFetcher(data_provider=provider)

        df = await fetcher.fetch(
            sym,
            as_of=datetime(2026, 1, 5, 9, 40, 0),
            lookback=2,
            freq="5m",
        )

        self.assertEqual(len(df), 2)
        self.assertEqual(
            [idx.isoformat() for idx in df.index.to_pydatetime()],
            ["2026-01-05T09:35:00", "2026-01-05T09:40:00"],
        )


if __name__ == "__main__":
    unittest.main()
