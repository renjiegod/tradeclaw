"""Tests for :class:`doyoutrade.data.fallback_provider.FallbackHistoricalDataProvider`.

Covers the three failure-mode branches plus the
``last_used_provider`` tracking the assistant tool relies on for the
``provider_used`` envelope.
"""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from doyoutrade.core.models import Bar, MarketContext, QuoteSnapshot
from doyoutrade.data.fallback_provider import (
    FallbackHistoricalDataProvider,
    FallbackRealtimeQuoteProvider,
)
from doyoutrade.data.protocols import ProviderCapabilities
from doyoutrade.infra.qmt_proxy_client import QmtRealtimeKlineUnsupportedError


def _bar(day: str, close: float) -> Bar:
    return Bar(
        symbol="600000.SH",
        timestamp=day,
        open=close - 0.5,
        high=close + 0.5,
        low=close - 1.0,
        close=close,
        volume=1000.0,
        amount=close * 1000.0,
    )


class _StubProvider:
    """Minimal provider double exposing capabilities and the four protocol methods."""

    def __init__(
        self,
        name: str,
        *,
        bars=None,
        raise_on_get: Exception | None = None,
        intervals: frozenset[str] | None = None,
        unsupported_index_intervals: frozenset[str] | None = None,
    ):
        self.capabilities = ProviderCapabilities(
            name=name,
            supported_intervals=intervals or frozenset({"1d", "1w"}),
            unsupported_index_intervals=unsupported_index_intervals or frozenset(),
        )
        self._bars = list(bars or [])
        self._raise = raise_on_get
        self.get_bars_calls: list[tuple] = []
        self.market_context_calls = 0

    async def get_bars(self, symbol, start, end, *, interval="1d", adjust="qfq"):
        self.get_bars_calls.append((symbol, start, end, interval))
        if self._raise is not None:
            raise self._raise
        return list(self._bars)

    async def get_market_context(self):
        self.market_context_calls += 1
        return MarketContext()

    async def is_trading_day(self, value):
        return True

    async def get_trading_dates(self, start, end):
        return [start, end]


class FallbackHistoricalDataProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_with_bars_short_circuits(self):
        """When the primary returns bars, secondaries are not touched."""
        primary = _StubProvider("qmt", bars=[_bar("2026-01-02", 12.0)])
        secondary = _StubProvider("akshare")
        wrapper = FallbackHistoricalDataProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            bars = await wrapper.get_bars("600000.SH", "2026-01-01", "2026-01-05")

        self.assertEqual([b.timestamp for b in bars], ["2026-01-02"])
        self.assertEqual(wrapper.last_used_provider, "qmt")
        self.assertEqual(len(primary.get_bars_calls), 1)
        self.assertEqual(secondary.get_bars_calls, [])
        # No skip events for the primary-success path.
        emit.assert_not_awaited()

    async def test_falls_back_on_exception_with_visible_event(self):
        """A primary raising delegates to secondary with a debug event capturing the cause."""
        primary = _StubProvider("qmt", raise_on_get=RuntimeError("proxy down"))
        secondary = _StubProvider("akshare", bars=[_bar("2026-01-02", 13.5)])
        wrapper = FallbackHistoricalDataProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            bars = await wrapper.get_bars("600000.SH", "2026-01-01", "2026-01-05")

        self.assertEqual([b.close for b in bars], [13.5])
        self.assertEqual(wrapper.last_used_provider, "akshare")
        # One skip event for qmt → exception.
        events = [call.args for call in emit.await_args_list]
        self.assertEqual(len(events), 1)
        name, payload = events[0]
        self.assertEqual(name, "market_data_provider_skipped")
        self.assertEqual(payload["provider"], "qmt")
        self.assertEqual(payload["reason"], "exception")
        self.assertEqual(payload["exc_type"], "RuntimeError")
        self.assertIn("proxy down", payload["exc_message"])

    async def test_falls_back_on_empty_with_visible_event(self):
        """An empty result from the primary triggers a skip + next provider attempt."""
        primary = _StubProvider("qmt", bars=[])
        secondary = _StubProvider("akshare", bars=[_bar("2026-01-02", 11.0)])
        wrapper = FallbackHistoricalDataProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            bars = await wrapper.get_bars("600000.SH", "2026-01-01", "2026-01-05")

        self.assertEqual([b.close for b in bars], [11.0])
        self.assertEqual(wrapper.last_used_provider, "akshare")
        events = [call.args for call in emit.await_args_list]
        self.assertEqual(events[0][1]["provider"], "qmt")
        self.assertEqual(events[0][1]["reason"], "empty_result")

    async def test_interval_unsupported_skips_provider(self):
        """Providers whose capabilities don't advertise the interval are skipped without an upstream call."""
        primary = _StubProvider(
            "tushare", intervals=frozenset({"1d"}), bars=[_bar("2026-01-02", 9.0)]
        )
        secondary = _StubProvider(
            "qmt", intervals=frozenset({"1m"}), bars=[_bar("2026-01-02T10:30:00", 10.0)]
        )
        wrapper = FallbackHistoricalDataProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            bars = await wrapper.get_bars(
                "600000.SH", "2026-01-01", "2026-01-05", interval="1m"
            )

        self.assertEqual([b.close for b in bars], [10.0])
        # tushare was skipped before its get_bars was called.
        self.assertEqual(primary.get_bars_calls, [])
        self.assertEqual(len(secondary.get_bars_calls), 1)
        payload = emit.await_args_list[0].args[1]
        self.assertEqual(payload["provider"], "tushare")
        self.assertEqual(payload["reason"], "interval_unsupported")

    async def test_index_symbol_skips_provider_lacking_index_minute_support(self):
        """A baostock-like provider that declares 60m support only for non-index
        symbols must be skipped (not called upstream) when the request is for
        an index — the fallback chain should fall through to a provider that
        actually serves index minute bars, instead of hitting baostock's SDK
        and surfacing its opaque parse failure."""
        baostock_like = _StubProvider(
            "baostock",
            intervals=frozenset({"1d", "60m"}),
            unsupported_index_intervals=frozenset({"60m"}),
            bars=[_bar("2026-01-02", 9.0)],
        )
        akshare_like = _StubProvider(
            "akshare", intervals=frozenset({"60m"}), bars=[_bar("2026-01-02T10:30:00", 10.0)]
        )
        wrapper = FallbackHistoricalDataProvider([baostock_like, akshare_like])

        bars = await wrapper.get_bars(
            "000001.SH", "2026-01-01", "2026-01-05", interval="60m"
        )

        self.assertEqual([b.close for b in bars], [10.0])
        self.assertEqual(baostock_like.get_bars_calls, [])
        self.assertEqual(len(akshare_like.get_bars_calls), 1)

    async def test_exhausted_chain_reraises_last_error(self):
        """When every provider raises, the wrapper surfaces the final exception."""
        primary = _StubProvider("qmt", raise_on_get=RuntimeError("qmt"))
        secondary = _StubProvider("akshare", raise_on_get=RuntimeError("ak"))
        wrapper = FallbackHistoricalDataProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ):
            with self.assertRaises(RuntimeError) as ctx:
                await wrapper.get_bars("600000.SH", "2026-01-01", "2026-01-05")
        self.assertEqual(str(ctx.exception), "ak")
        self.assertIsNone(wrapper.last_used_provider)

    async def test_terminal_error_aborts_fallback_chain(self):
        """Terminal provider errors must surface immediately instead of silently degrading semantics."""
        primary = _StubProvider(
            "qmt",
            raise_on_get=QmtRealtimeKlineUnsupportedError("full_kline unsupported"),
            intervals=frozenset({"5m"}),
        )
        secondary = _StubProvider(
            "akshare",
            bars=[_bar("2026-01-02", 13.5)],
            intervals=frozenset({"5m"}),
        )
        wrapper = FallbackHistoricalDataProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            with self.assertRaises(QmtRealtimeKlineUnsupportedError):
                await wrapper.get_bars(
                    "600000.SH",
                    "2026-06-15T09:30:00",
                    "2026-06-15T10:00:00",
                    interval="5m",
                )

        self.assertEqual(len(primary.get_bars_calls), 1)
        self.assertEqual(secondary.get_bars_calls, [])
        events = [call.args for call in emit.await_args_list]
        self.assertEqual(len(events), 1)
        name, payload = events[0]
        self.assertEqual(name, "market_data_provider_failed_terminal")
        self.assertEqual(payload["provider"], "qmt")
        self.assertEqual(payload["reason"], "terminal_error")

    async def test_exhausted_chain_all_empty_returns_empty_list(self):
        """When all providers return [], the wrapper returns [] (no synthetic error)."""
        primary = _StubProvider("qmt", bars=[])
        secondary = _StubProvider("akshare", bars=[])
        wrapper = FallbackHistoricalDataProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ):
            bars = await wrapper.get_bars("600000.SH", "2026-01-01", "2026-01-05")
        self.assertEqual(bars, [])
        self.assertIsNone(wrapper.last_used_provider)

    async def test_market_context_uses_primary_only(self):
        """Non-bar APIs route to the first provider — no fallback."""
        primary = _StubProvider("qmt")
        secondary = _StubProvider("akshare")
        wrapper = FallbackHistoricalDataProvider([primary, secondary])

        await wrapper.get_market_context()
        self.assertEqual(primary.market_context_calls, 1)
        self.assertEqual(secondary.market_context_calls, 0)

    def test_constructor_requires_at_least_one_provider(self):
        with self.assertRaises(ValueError):
            FallbackHistoricalDataProvider([])


class _StubQuoteProvider:
    """Minimal RealtimeQuoteProvider double: canned answers or a raised exception."""

    def __init__(self, name: str, *, answers: dict[str, QuoteSnapshot] | None = None, raise_on_fetch: Exception | None = None):
        self.name = name
        self._answers = dict(answers or {})
        self._raise = raise_on_fetch
        self.fetch_calls: list[list[str]] = []

    async def fetch_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        self.fetch_calls.append(list(symbols))
        if self._raise is not None:
            raise self._raise
        return {
            s: self._answers.get(s, QuoteSnapshot(symbol=s, status="no_data"))
            for s in symbols
        }


class FallbackRealtimeQuoteProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_answers_everything_secondary_not_called(self):
        primary = _StubQuoteProvider(
            "mootdx",
            answers={"600000.SH": QuoteSnapshot(symbol="600000.SH", price=10.0, status="ok")},
        )
        secondary = _StubQuoteProvider("akshare")
        wrapper = FallbackRealtimeQuoteProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ):
            quotes = await wrapper.fetch_quotes(["600000.SH"])

        self.assertEqual(quotes["600000.SH"].status, "ok")
        self.assertAlmostEqual(quotes["600000.SH"].price, 10.0)
        self.assertEqual(secondary.fetch_calls, [])

    async def test_secondary_answers_symbols_primary_missed(self):
        primary = _StubQuoteProvider(
            "mootdx",
            answers={"600000.SH": QuoteSnapshot(symbol="600000.SH", price=10.0, status="ok")},
        )
        secondary = _StubQuoteProvider(
            "akshare",
            answers={"430047.BJ": QuoteSnapshot(symbol="430047.BJ", price=5.0, status="ok")},
        )
        wrapper = FallbackRealtimeQuoteProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ) as emit_mock:
            quotes = await wrapper.fetch_quotes(["600000.SH", "430047.BJ"])

        self.assertEqual(quotes["600000.SH"].status, "ok")
        self.assertEqual(quotes["430047.BJ"].status, "ok")
        self.assertAlmostEqual(quotes["430047.BJ"].price, 5.0)
        # secondary must only have been asked about the symbol primary missed
        self.assertEqual(secondary.fetch_calls, [["430047.BJ"]])
        emit_mock.assert_awaited()

    async def test_primary_raises_falls_through_to_secondary(self):
        primary = _StubQuoteProvider("mootdx", raise_on_fetch=RuntimeError("mootdx not installed"))
        secondary = _StubQuoteProvider(
            "akshare",
            answers={"600000.SH": QuoteSnapshot(symbol="600000.SH", price=11.0, status="ok")},
        )
        wrapper = FallbackRealtimeQuoteProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ):
            quotes = await wrapper.fetch_quotes(["600000.SH"])

        self.assertEqual(quotes["600000.SH"].status, "ok")
        self.assertAlmostEqual(quotes["600000.SH"].price, 11.0)

    async def test_all_providers_exhausted_symbol_is_no_data_not_dropped(self):
        primary = _StubQuoteProvider("mootdx")
        secondary = _StubQuoteProvider("akshare")
        wrapper = FallbackRealtimeQuoteProvider([primary, secondary])

        with patch(
            "doyoutrade.data.fallback_provider.emit_debug_event", new_callable=AsyncMock
        ):
            quotes = await wrapper.fetch_quotes(["999999.SH"])

        self.assertIn("999999.SH", quotes)
        self.assertEqual(quotes["999999.SH"].status, "no_data")

    def test_constructor_requires_at_least_one_provider_for_quotes(self):
        with self.assertRaises(ValueError):
            FallbackRealtimeQuoteProvider([])


if __name__ == "__main__":
    unittest.main()
