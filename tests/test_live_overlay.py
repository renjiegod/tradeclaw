"""``strategy_sdk.live_overlay`` — splice a live forming bar onto warehouse history.

Covers ``build_forming_bar_row`` (field fallbacks + status/price guards),
``splice_forming_bar`` (append vs same-day replace, tz alignment), and the
``LiveBarOverlayHistoryFetcher`` decorator (daily-only overlay, pass-through for
non-daily freq / missing / non-ok quotes, ``tail(lookback)`` bound).
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from doyoutrade.core.models import QuoteSnapshot
from doyoutrade.strategy_sdk.live_overlay import (
    LiveBarOverlayHistoryFetcher,
    build_forming_bar_row,
    splice_forming_bar,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _history(n: int = 6, last_day: str = "2026-06-13") -> pd.DataFrame:
    idx = pd.date_range(end=last_day, periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1000.0] * n,
        },
        index=idx,
    )


class _FakeFetcher:
    def __init__(self, df: pd.DataFrame, *, record: dict | None = None) -> None:
        self._df = df
        self._record = record

    async def fetch(self, symbol, *, as_of, lookback, freq="1d") -> pd.DataFrame:
        if self._record is not None:
            self._record["freq"] = freq
            self._record["lookback"] = lookback
        return self._df.copy()


class BuildFormingBarRowTests(unittest.TestCase):
    def test_ok_quote_builds_row(self):
        snap = QuoteSnapshot(
            symbol="X", price=90.0, open=99.0, high=99.5, low=88.0,
            volume=5000.0, status="ok",
        )
        row = build_forming_bar_row(snap)
        self.assertEqual(row["close"], 90.0)
        self.assertEqual(row["open"], 99.0)
        self.assertEqual(row["volume"], 5000.0)

    def test_non_ok_status_returns_none(self):
        snap = QuoteSnapshot(symbol="X", price=90.0, status="qmt_disconnected")
        self.assertIsNone(build_forming_bar_row(snap))

    def test_missing_price_returns_none(self):
        snap = QuoteSnapshot(symbol="X", price=None, status="ok")
        self.assertIsNone(build_forming_bar_row(snap))

    def test_missing_ohl_fall_back_to_price(self):
        snap = QuoteSnapshot(symbol="X", price=50.0, status="ok")
        row = build_forming_bar_row(snap)
        self.assertEqual(row["open"], 50.0)
        self.assertEqual(row["high"], 50.0)
        self.assertEqual(row["low"], 50.0)
        self.assertEqual(row["volume"], 0.0)

    def test_high_low_kept_consistent_with_close(self):
        # last print printed above the reported high during a fast move.
        snap = QuoteSnapshot(
            symbol="X", price=110.0, open=100.0, high=105.0, low=98.0, status="ok",
        )
        row = build_forming_bar_row(snap)
        self.assertGreaterEqual(row["high"], row["close"])
        self.assertLessEqual(row["low"], row["close"])


class SpliceFormingBarTests(unittest.TestCase):
    def test_append_when_new_day(self):
        df = _history(last_day="2026-06-13")
        as_of = datetime(2026, 6, 17, 6, 50, tzinfo=timezone.utc)
        row = {"open": 99.0, "high": 99.0, "low": 88.0, "close": 90.0, "volume": 5000.0}
        out = splice_forming_bar(df, as_of, row)
        self.assertEqual(len(out), len(df) + 1)
        self.assertEqual(float(out["close"].iloc[-1]), 90.0)

    def test_replace_when_same_day(self):
        df = _history(last_day="2026-06-17")
        as_of = datetime(2026, 6, 17, 6, 50, tzinfo=timezone.utc)
        row = {"open": 99.0, "high": 99.0, "low": 88.0, "close": 90.0, "volume": 5000.0}
        out = splice_forming_bar(df, as_of, row)
        self.assertEqual(len(out), len(df))  # replaced, not appended
        self.assertEqual(float(out["close"].iloc[-1]), 90.0)


class LiveBarOverlayFetcherTests(unittest.TestCase):
    def setUp(self):
        self.as_of = datetime(2026, 6, 17, 6, 50, tzinfo=timezone.utc)
        self.snap = QuoteSnapshot(
            symbol="X", price=90.0, open=99.0, high=99.5, low=88.0,
            volume=5000.0, status="ok",
        )

    def test_daily_appends_forming_bar(self):
        base = _FakeFetcher(_history())
        ov = LiveBarOverlayHistoryFetcher(inner=base, quotes={"X": self.snap})
        df = _run(ov.fetch("X", as_of=self.as_of, lookback=30, freq="1d"))
        self.assertEqual(float(df["close"].iloc[-1]), 90.0)

    def test_non_daily_freq_passes_through(self):
        base = _FakeFetcher(_history())
        ov = LiveBarOverlayHistoryFetcher(inner=base, quotes={"X": self.snap})
        df = _run(ov.fetch("X", as_of=self.as_of, lookback=30, freq="5m"))
        # No forming bar spliced — last close stays the historical 100.0.
        self.assertEqual(float(df["close"].iloc[-1]), 100.0)

    def test_missing_quote_passes_through(self):
        base = _FakeFetcher(_history())
        ov = LiveBarOverlayHistoryFetcher(inner=base, quotes={})
        df = _run(ov.fetch("X", as_of=self.as_of, lookback=30, freq="1d"))
        self.assertEqual(float(df["close"].iloc[-1]), 100.0)

    def test_non_ok_quote_passes_through(self):
        base = _FakeFetcher(_history())
        bad = QuoteSnapshot(symbol="X", price=90.0, status="no_data")
        ov = LiveBarOverlayHistoryFetcher(inner=base, quotes={"X": bad})
        df = _run(ov.fetch("X", as_of=self.as_of, lookback=30, freq="1d"))
        self.assertEqual(float(df["close"].iloc[-1]), 100.0)

    def test_tail_bounds_window(self):
        base = _FakeFetcher(_history(n=10))
        ov = LiveBarOverlayHistoryFetcher(inner=base, quotes={"X": self.snap})
        df = _run(ov.fetch("X", as_of=self.as_of, lookback=5, freq="1d"))
        self.assertEqual(len(df), 5)
        self.assertEqual(float(df["close"].iloc[-1]), 90.0)

    def test_data_provider_property_exposes_inner(self):
        class _WithProvider:
            data_provider = object()

            async def fetch(self, *a, **k):
                return _history()

        inner = _WithProvider()
        ov = LiveBarOverlayHistoryFetcher(inner=inner, quotes={})
        self.assertIs(ov.data_provider, inner.data_provider)


if __name__ == "__main__":
    unittest.main()
