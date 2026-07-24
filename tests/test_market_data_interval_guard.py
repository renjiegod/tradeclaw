"""Regression coverage: an explicit (non-``auto``) data source must reject an
unsupported interval/instrument-type combination *before* the upstream call.

Background: ``FallbackHistoricalDataProvider`` (the ``data_source=auto``
chain) already skips a provider whose capabilities don't cover the requested
(interval, symbol) pair. But requesting an explicit single provider (e.g.
``--data-source baostock``) bypassed that chain entirely and went straight
to ``provider.get_bars()`` — for an index + minute-interval request on
baostock, that surfaced as a bare ``ValueError: not enough values to
unpack`` leaking out of baostock's own response parser, with zero context
for the caller. ``MarketDataFetcher._fetch_ohlcv`` now checks
``supports_interval_for_symbol`` up front for the single-provider path and
raises a named, informative error instead.
"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

from doyoutrade.api.operations.market_data import (
    MarketDataFetcher,
    _IntervalNotSupportedForSymbol,
)
from doyoutrade.core.models import Bar
from doyoutrade.data.protocols import ProviderCapabilities


class _StubSingleProvider:
    def __init__(self, capabilities: ProviderCapabilities, bars=None):
        self.capabilities = capabilities
        self._bars = list(bars or [])
        self.get_bars_calls: list[tuple] = []

    async def get_bars(self, symbol, start, end, *, interval="1d", adjust="qfq"):
        self.get_bars_calls.append((symbol, start, end, interval))
        return list(self._bars)


class FetchOhlcvIntervalGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._account_patch = patch(
            "doyoutrade.data.account_resolution.resolve_default_market_account",
            new=AsyncMock(return_value=None),
        )
        self._account_patch.start()
        self.addAsyncCleanup(self._account_patch.stop)

    async def test_explicit_baostock_rejects_index_minute_interval_before_network_call(self):
        stub = _StubSingleProvider(
            ProviderCapabilities(
                name="baostock",
                supported_intervals=frozenset({"1d", "60m"}),
                unsupported_index_intervals=frozenset({"60m"}),
            ),
            bars=[Bar(symbol="000001.SH", timestamp="2026-01-02", open=1, high=1, low=1, close=1, volume=1)],
        )
        with patch(
            "doyoutrade.data.factory.build_trading_data_stack",
            return_value=(stub, None, None),
        ):
            fetcher = MarketDataFetcher()
            with self.assertRaises(_IntervalNotSupportedForSymbol) as ctx:
                await fetcher._fetch_ohlcv(
                    "000001.SH",
                    start_dt=date(2026, 1, 1),
                    end_dt=date(2026, 1, 5),
                    period_label="test",
                    interval="60m",
                    data_source="baostock",
                )
        self.assertIn("000001.SH", str(ctx.exception))
        self.assertIn("60m", str(ctx.exception))
        # The whole point: reject before the upstream call, not after it fails.
        self.assertEqual(stub.get_bars_calls, [])

    async def test_explicit_baostock_still_serves_stock_minute_interval(self):
        stub = _StubSingleProvider(
            ProviderCapabilities(
                name="baostock",
                supported_intervals=frozenset({"1d", "60m"}),
                unsupported_index_intervals=frozenset({"60m"}),
            ),
            bars=[Bar(symbol="600519.SH", timestamp="2026-01-02", open=1, high=1, low=1, close=1, volume=1)],
        )
        with patch(
            "doyoutrade.data.factory.build_trading_data_stack",
            return_value=(stub, None, None),
        ):
            fetcher = MarketDataFetcher()
            await fetcher._fetch_ohlcv(
                "600519.SH",
                start_dt=date(2026, 1, 1),
                end_dt=date(2026, 1, 5),
                period_label="test",
                interval="60m",
                data_source="baostock",
            )
        self.assertEqual(len(stub.get_bars_calls), 1)


if __name__ == "__main__":
    unittest.main()
