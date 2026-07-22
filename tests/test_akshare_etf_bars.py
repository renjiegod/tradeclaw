"""akshare historical provider routes ETFs to fund_etf_hist_em, stocks to stock_zh_a_hist."""

import asyncio
import unittest
from unittest.mock import patch

import pandas as pd

from doyoutrade.data.akshare_provider import AkshareHistoricalProvider


def _hist_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "日期": "2024-06-03",
                "开盘": 1.0,
                "最高": 1.1,
                "最低": 0.9,
                "收盘": 1.05,
                "成交量": 12345,
                "成交额": 67890.0,
            }
        ]
    )


class AkshareEtfRoutingTests(unittest.TestCase):
    def test_etf_symbol_uses_fund_etf_hist_em(self) -> None:
        provider = AkshareHistoricalProvider()
        with patch(
            "doyoutrade.data.akshare_provider.ak.fund_etf_hist_em",
            return_value=_hist_df(),
        ) as etf_api, patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_hist",
            return_value=_hist_df(),
        ) as stock_api:
            bars = asyncio.run(
                provider.get_bars("510300.SH", "2024-06-01", "2024-06-30", interval="1d")
            )
        etf_api.assert_called_once()
        stock_api.assert_not_called()
        # akshare hist endpoints take the bare 6-digit code.
        self.assertEqual(etf_api.call_args.kwargs["symbol"], "510300")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].symbol, "510300.SH")
        self.assertEqual(bars[0].close, 1.05)

    def test_stock_symbol_uses_stock_zh_a_hist(self) -> None:
        provider = AkshareHistoricalProvider()
        with patch(
            "doyoutrade.data.akshare_provider.ak.fund_etf_hist_em",
            return_value=_hist_df(),
        ) as etf_api, patch(
            "doyoutrade.data.akshare_provider.ak.stock_zh_a_hist",
            return_value=_hist_df(),
        ) as stock_api:
            bars = asyncio.run(
                provider.get_bars("600000.SH", "2024-06-01", "2024-06-30", interval="1d")
            )
        stock_api.assert_called_once()
        etf_api.assert_not_called()
        self.assertEqual(stock_api.call_args.kwargs["symbol"], "600000")
        self.assertEqual(len(bars), 1)


if __name__ == "__main__":
    unittest.main()
