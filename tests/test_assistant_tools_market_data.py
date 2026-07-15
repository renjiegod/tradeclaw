"""Helper-class tests for :mod:`doyoutrade.api.operations.market_data`.

The chat-facing ``get_market_data`` tool and ``data ohlcv`` CLI were
removed in favour of the multi-symbol ``data run`` entry point; what
remains is :class:`MarketDataFetcher`, an internal helper used by
``data_run`` to walk the data-factory provider chain. These tests pin
the helper's window-resolution and fetch-side contracts.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from doyoutrade.api.operations.market_data import (
    ARTIFACTS_ROOT,
    MarketDataFetcher,
    _InvalidPeriod,
)


class MarketDataFetcherTests(unittest.IsolatedAsyncioTestCase):
    """Window-resolution and provider-chain behaviour for the helper."""

    def test_artifacts_root_uses_home(self) -> None:
        home = Path.home()
        self.assertTrue(str(ARTIFACTS_ROOT).startswith(str(home)))
        self.assertIn(".doyoutrade", str(ARTIFACTS_ROOT))

    def test_period_to_start_date_supports_common_units(self) -> None:
        fetcher = MarketDataFetcher()
        end = date(2026, 5, 16)
        cases = {
            "1y": 365,
            "6m": 180,
            "1m": 30,
            "1mo": 30,
            "3mo": 90,
            "20d": 20,
            "3w": 21,
            " 1MO ": 30,
        }
        for period, days in cases.items():
            with self.subTest(period=period):
                self.assertEqual(
                    fetcher._period_to_start_date(period, end),
                    end - timedelta(days=days),
                )

    def test_period_to_start_date_rejects_unknown(self) -> None:
        fetcher = MarketDataFetcher()
        for bad in ("", "abc", "1mo2", "0d", "-1d", "month"):
            with self.subTest(period=bad):
                with self.assertRaises(_InvalidPeriod):
                    fetcher._period_to_start_date(bad, date(2026, 5, 16))

    def test_fetch_ohlcv_records_last_used_source_for_single_provider(self) -> None:
        """``_last_used_source`` mirrors the provider that actually answered."""
        import pandas as pd
        from unittest.mock import AsyncMock, MagicMock, patch
        from doyoutrade.core.models import Bar

        dates = pd.date_range("2026-01-01", periods=10, freq="B")
        fake_bars = [
            Bar(
                symbol="TEST001.SZ",
                timestamp=ts.strftime("%Y-%m-%d"),
                open=10.0,
                high=10.5,
                low=9.5,
                close=10.2,
                volume=1000.0,
                amount=None,
                adjust_type="qfq",
            )
            for ts in dates
        ]

        stub_provider = MagicMock()
        stub_provider.capabilities = MagicMock()
        stub_provider.capabilities.name = "qmt"
        stub_provider.get_bars = AsyncMock(return_value=fake_bars)
        stub_provider.aclose = AsyncMock()

        fetcher = MarketDataFetcher()
        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            original_config = os.environ.get("DOYOUTRADE_CONFIG", "")
            os.environ["HOME"] = tmp_home
            os.environ["DOYOUTRADE_CONFIG"] = str(
                Path(__file__).resolve().parents[1] / "config.yaml"
            )
            try:
                with patch(
                    "doyoutrade.data.factory.build_trading_data_stack",
                    return_value=(stub_provider, MagicMock(), MagicMock()),
                ):
                    df = asyncio.run(
                        fetcher._fetch_ohlcv(
                            "TEST001.SZ",
                            start_dt=date(2026, 1, 1),
                            end_dt=date(2026, 1, 14),
                            period_label="2w",
                            interval="1d",
                            data_source="qmt",
                        )
                    )
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
                if original_config:
                    os.environ["DOYOUTRADE_CONFIG"] = original_config
                else:
                    os.environ.pop("DOYOUTRADE_CONFIG", None)

        # Frame should carry the expected columns and the helper should
        # have recorded which provider actually delivered the bars.
        for column in ("open", "high", "low", "close", "volume"):
            self.assertIn(column, df.columns)
        self.assertEqual(len(df), 10)
        self.assertEqual(fetcher._last_used_source, "qmt")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
