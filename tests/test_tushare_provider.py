"""Tests for :class:`doyoutrade.data.tushare_provider.TushareDataProvider`.

Tushare is an optional dep; these tests substitute a stub module in
``sys.modules`` before the provider's lazy import runs so the test
suite doesn't require the ``tushare`` package to be installed.
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from typing import Any
from unittest.mock import MagicMock


def _install_stub_tushare(pro_bar_return: Any = None, pro_bar_side_effect: Exception | None = None):
    """Install a minimal ``tushare`` stub into ``sys.modules``.

    Returns the stub so the test can assert on calls (set_token / pro_bar).
    The caller is responsible for restoring the previous module via
    :func:`_restore_tushare` so other tests aren't polluted.
    """
    stub = types.ModuleType("tushare")
    stub.set_token = MagicMock()
    stub.pro_api = MagicMock(return_value=MagicMock())
    if pro_bar_side_effect is not None:
        stub.pro_bar = MagicMock(side_effect=pro_bar_side_effect)
    else:
        stub.pro_bar = MagicMock(return_value=pro_bar_return)
    sys.modules["tushare"] = stub
    return stub


def _restore_tushare(previous: types.ModuleType | None):
    if previous is None:
        sys.modules.pop("tushare", None)
    else:
        sys.modules["tushare"] = previous


class _FakeProApi:
    """A plain object (unlike MagicMock, ``hasattr`` reflects real attribute
    presence) standing in for the handle ``tushare.pro_api()`` returns."""


class _FakeDataFrame:
    """Minimal DataFrame-like object replicating the subset Tushare returns."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self.empty = not rows

    def iterrows(self):
        for i, row in enumerate(self._rows):
            yield i, row


class TushareDataProviderTests(unittest.TestCase):
    def setUp(self):
        self._prev_tushare = sys.modules.get("tushare")

    def tearDown(self):
        _restore_tushare(self._prev_tushare)

    def test_init_rejects_empty_token(self):
        from doyoutrade.data.tushare_provider import TushareDataProvider

        with self.assertRaises(ValueError) as ctx:
            TushareDataProvider(symbols=["600000.SH"], token="")
        self.assertIn("non-empty token", str(ctx.exception))

    def test_capabilities_includes_minute_intervals(self):
        from doyoutrade.data.tushare_provider import TushareDataProvider

        provider = TushareDataProvider(symbols=["600000.SH"], token="t")
        caps = provider.capabilities
        self.assertEqual(caps.name, "tushare")
        self.assertIn("1d", caps.supported_intervals)
        self.assertIn("1mo", caps.supported_intervals)
        self.assertIn("1m", caps.supported_intervals)
        self.assertIn("5m", caps.supported_intervals)
        self.assertIn("60m", caps.supported_intervals)
        self.assertTrue(caps.requires_auth)
        self.assertFalse(caps.is_realtime_capable)

    def test_get_bars_minute_uses_min_freq_datetime_and_trade_time(self):
        """Minute requests use ``<n>min`` freq, datetime bounds, and ``trade_time``."""
        from doyoutrade.data.tushare_provider import TushareDataProvider

        # Tushare returns minute bars newest-first with a ``trade_time`` column.
        rows = [
            {
                "trade_time": "2024-12-02 09:40:00",
                "open": 10.2, "high": 10.4, "low": 10.1, "close": 10.3,
                "vol": 500.0, "amount": 5150.0,
            },
            {
                "trade_time": "2024-12-02 09:35:00",
                "open": 10.0, "high": 10.3, "low": 9.9, "close": 10.2,
                "vol": 600.0, "amount": 6120.0,
            },
        ]
        stub = _install_stub_tushare(pro_bar_return=_FakeDataFrame(rows))
        provider = TushareDataProvider(symbols=["600000.SH"], token="t")
        bars = asyncio.run(
            provider.get_bars("600000.SH", "2024-12-02", "2024-12-02", interval="5m")
        )

        self.assertEqual(
            [b.timestamp for b in bars],
            ["2024-12-02T09:35:00", "2024-12-02T09:40:00"],
        )
        self.assertEqual([b.close for b in bars], [10.2, 10.3])
        kwargs = stub.pro_bar.call_args.kwargs
        self.assertEqual(kwargs["freq"], "5min")
        self.assertEqual(kwargs["start_date"], "2024-12-02 09:00:00")
        self.assertEqual(kwargs["end_date"], "2024-12-02 15:00:00")

    def test_get_bars_unsupported_interval_raises(self):
        from doyoutrade.data.tushare_provider import TushareDataProvider

        _install_stub_tushare(pro_bar_return=_FakeDataFrame([]))
        provider = TushareDataProvider(symbols=["600000.SH"], token="t")
        with self.assertRaises(ValueError):
            asyncio.run(
                provider.get_bars("600000.SH", "2026-01-01", "2026-01-05", interval="3h")
            )

    def test_get_bars_translates_pro_bar_rows_chronologically(self):
        """``pro_bar`` returns newest-first; provider must flip to ascending order."""
        from doyoutrade.data.tushare_provider import TushareDataProvider

        # Tushare returns newest-first; two daily rows.
        rows = [
            {
                "trade_date": "20260103",
                "open": 11.0,
                "high": 11.5,
                "low": 10.8,
                "close": 11.2,
                "vol": 2000.0,
                "amount": 22400.0,
            },
            {
                "trade_date": "20260102",
                "open": 10.5,
                "high": 11.0,
                "low": 10.3,
                "close": 10.9,
                "vol": 1800.0,
                "amount": 19620.0,
            },
        ]
        stub = _install_stub_tushare(pro_bar_return=_FakeDataFrame(rows))

        provider = TushareDataProvider(symbols=["600000.SH"], token="t")
        bars = asyncio.run(
            provider.get_bars(
                "600000.SH", "2026-01-01", "2026-01-05", interval="1d"
            )
        )

        self.assertEqual([b.timestamp for b in bars], ["2026-01-02", "2026-01-03"])
        self.assertEqual([b.close for b in bars], [10.9, 11.2])
        self.assertEqual(bars[0].adjust_type, "qfq")
        # Token + pro_api initialised once.
        stub.set_token.assert_called_with("t")
        stub.pro_api.assert_called()
        # Call to pro_bar used the YYYYMMDD-flattened dates and adj=qfq.
        kwargs = stub.pro_bar.call_args.kwargs
        self.assertEqual(kwargs["ts_code"], "600000.SH")
        self.assertEqual(kwargs["start_date"], "20260101")
        self.assertEqual(kwargs["end_date"], "20260105")
        self.assertEqual(kwargs["adj"], "qfq")
        self.assertEqual(kwargs["freq"], "D")

    def test_get_bars_empty_response_returns_empty(self):
        from doyoutrade.data.tushare_provider import TushareDataProvider

        _install_stub_tushare(pro_bar_return=_FakeDataFrame([]))
        provider = TushareDataProvider(symbols=["600000.SH"], token="t")
        bars = asyncio.run(
            provider.get_bars("600000.SH", "2026-01-01", "2026-01-05")
        )
        self.assertEqual(bars, [])

    def test_get_bars_pro_bar_exception_surfaces_as_runtime_error(self):
        """Upstream errors (bad token / rate limit / missing minute credit) must
        be visible, not swallowed into a fake empty result."""
        from doyoutrade.data.tushare_provider import TushareDataProvider

        _install_stub_tushare(pro_bar_side_effect=RuntimeError("rate limit"))
        provider = TushareDataProvider(symbols=["600000.SH"], token="t")
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(
                provider.get_bars("600000.SH", "2026-01-01", "2026-01-05")
            )
        self.assertIn("rate limit", str(ctx.exception))
        self.assertIn("pro_bar failed", str(ctx.exception))

    def test_ensure_pro_api_defaults_to_official_gateway_when_url_unset(self):
        """No custom url configured -> the handle's http_url is left untouched."""
        from doyoutrade.data.tushare_provider import TushareDataProvider

        stub = _install_stub_tushare()
        stub.pro_api.return_value = _FakeProApi()
        provider = TushareDataProvider(symbols=["600000.SH"], token="t")
        provider._ensure_pro_api()

        pro = stub.pro_api.return_value
        self.assertFalse(hasattr(pro, "_DataApi__http_url"))

    def test_ensure_pro_api_overrides_http_url_when_configured(self):
        """A configured ``url`` overrides the Tushare handle's private gateway attr."""
        from doyoutrade.data.tushare_provider import TushareDataProvider

        stub = _install_stub_tushare()
        stub.pro_api.return_value = _FakeProApi()
        provider = TushareDataProvider(
            symbols=["600000.SH"], token="t", url="http://proxy.example.com"
        )
        provider._ensure_pro_api()

        pro = stub.pro_api.return_value
        self.assertEqual(pro._DataApi__token, "t")
        self.assertEqual(pro._DataApi__http_url, "http://proxy.example.com")

    def test_get_bars_passes_pro_api_handle_to_pro_bar(self):
        """``pro_bar`` must receive our configured handle, else it silently
        builds its own default-gateway handle and a custom url is ignored."""
        from doyoutrade.data.tushare_provider import TushareDataProvider

        stub = _install_stub_tushare(pro_bar_return=_FakeDataFrame([]))
        provider = TushareDataProvider(
            symbols=["600000.SH"], token="t", url="http://proxy.example.com"
        )
        asyncio.run(provider.get_bars("600000.SH", "2026-01-01", "2026-01-05"))

        kwargs = stub.pro_bar.call_args.kwargs
        self.assertIs(kwargs["api"], stub.pro_api.return_value)

    def test_ensure_pro_api_raises_when_tushare_missing(self):
        """A missing ``tushare`` install surfaces a clear RuntimeError, not ImportError."""
        from doyoutrade.data.tushare_provider import TushareDataProvider

        # Force ``import tushare`` to fail regardless of the host environment.
        sys.modules["tushare"] = None  # type: ignore[assignment]
        provider = TushareDataProvider(symbols=["600000.SH"], token="t")
        with self.assertRaises(RuntimeError) as ctx:
            provider._ensure_pro_api()
        self.assertIn("not installed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
