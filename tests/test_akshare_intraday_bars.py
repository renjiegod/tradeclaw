"""akshare intraday intervals must route to the *minute* endpoints.

Regression guard: intraday intervals (1m/5m/15m/30m/60m) used to be mapped into
``_INTERVAL_PERIOD_MAP`` and sent to ``stock_zh_a_hist``, which only accepts
daily/weekly/monthly and raised ``KeyError('60')`` — silently swallowed to zero
bars. They must go to ``stock_zh_a_hist_min_em`` (ETFs to
``fund_etf_hist_min_em``) with the numeric period and a datetime window.
"""

import asyncio
import unittest
from unittest.mock import patch

import pandas as pd

from doyoutrade.data.akshare_provider import AkshareHistoricalProvider


def _min_df() -> pd.DataFrame:
    # eastmoney minute endpoints key the timestamp as 时间 (not 日期).
    return pd.DataFrame(
        [
            {
                "时间": "2026-07-23 10:30:00",
                "开盘": 10.0,
                "最高": 10.2,
                "最低": 9.9,
                "收盘": 10.1,
                "成交量": 12345,
                "成交额": 67890.0,
            },
            {
                "时间": "2026-07-23 11:30:00",
                "开盘": 10.1,
                "最高": 10.3,
                "最低": 10.0,
                "收盘": 10.2,
                "成交量": 23456,
                "成交额": 78901.0,
            },
        ]
    )


class AkshareIntradayRoutingTests(unittest.TestCase):
    def test_60m_stock_uses_min_em_endpoint(self) -> None:
        provider = AkshareHistoricalProvider()
        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_hist_min_em",
            return_value=_min_df(),
        ) as min_api, patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_hist",
        ) as daily_api:
            bars = asyncio.run(
                provider.get_bars("600000.SH", "2026-07-22", "2026-07-23", interval="60m")
            )
        min_api.assert_called_once()
        daily_api.assert_not_called()
        kwargs = min_api.call_args.kwargs
        self.assertEqual(kwargs["symbol"], "600000")
        self.assertEqual(kwargs["period"], "60")
        # date-only bounds widened to the full session as "YYYY-MM-DD HH:MM:SS"
        self.assertEqual(kwargs["start_date"], "2026-07-22 09:30:00")
        self.assertEqual(kwargs["end_date"], "2026-07-23 15:00:00")
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].symbol, "600000.SH")
        # 时间 column parsed into an intraday timestamp (with a time component)
        self.assertEqual(bars[0].timestamp, "2026-07-23T10:30:00")
        self.assertEqual(bars[0].close, 10.1)

    def test_60m_etf_uses_fund_etf_min_em_endpoint(self) -> None:
        provider = AkshareHistoricalProvider()
        with patch(
            "doyoutrade.data.akshare_provider.ak.fund_etf_hist_min_em",
            return_value=_min_df(),
        ) as etf_min_api, patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_hist_min_em",
        ) as stock_min_api:
            bars = asyncio.run(
                provider.get_bars("510300.SH", "2026-07-22", "2026-07-23", interval="60m")
            )
        etf_min_api.assert_called_once()
        stock_min_api.assert_not_called()
        self.assertEqual(etf_min_api.call_args.kwargs["symbol"], "510300")
        self.assertEqual(etf_min_api.call_args.kwargs["period"], "60")
        self.assertEqual(len(bars), 2)

    def test_1m_forces_empty_adjust(self) -> None:
        # akshare's 1-minute feed rejects a non-empty adjust; the adapter must
        # blank it even when the caller asks for qfq.
        provider = AkshareHistoricalProvider()
        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_hist_min_em",
            return_value=_min_df(),
        ) as min_api:
            asyncio.run(
                provider.get_bars(
                    "600000.SH", "2026-07-23", "2026-07-23", interval="1m", adjust="qfq"
                )
            )
        self.assertEqual(min_api.call_args.kwargs["period"], "1")
        self.assertEqual(min_api.call_args.kwargs["adjust"], "")

    def test_5m_preserves_adjust(self) -> None:
        provider = AkshareHistoricalProvider()
        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_hist_min_em",
            return_value=_min_df(),
        ) as min_api:
            asyncio.run(
                provider.get_bars(
                    "600000.SH", "2026-07-23", "2026-07-23", interval="5m", adjust="qfq"
                )
            )
        self.assertEqual(min_api.call_args.kwargs["period"], "5")
        self.assertEqual(min_api.call_args.kwargs["adjust"], "qfq")

    def test_daily_still_uses_stock_zh_a_hist(self) -> None:
        # Regression: daily must NOT be diverted to the minute endpoint.
        provider = AkshareHistoricalProvider()
        daily_df = pd.DataFrame(
            [
                {
                    "日期": "2026-07-23",
                    "开盘": 10.0,
                    "最高": 10.2,
                    "最低": 9.9,
                    "收盘": 10.1,
                    "成交量": 12345,
                    "成交额": 67890.0,
                }
            ]
        )
        with patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_hist",
            return_value=daily_df,
        ) as daily_api, patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_hist_min_em",
        ) as min_api:
            bars = asyncio.run(
                provider.get_bars("600000.SH", "2026-07-01", "2026-07-23", interval="1d")
            )
        daily_api.assert_called_once()
        min_api.assert_not_called()
        self.assertEqual(daily_api.call_args.kwargs["period"], "daily")
        self.assertEqual(bars[0].timestamp, "2026-07-23")


if __name__ == "__main__":
    unittest.main()
