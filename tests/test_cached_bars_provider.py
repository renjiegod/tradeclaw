from __future__ import annotations

import unittest
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

from doyoutrade.data.cached_bars import (
    BACKTEST_BARS_CACHE_EXPANSION_DAYS,
    CachedBarsDataProvider,
    build_backtest_cached_data_provider,
    expanded_backtest_bar_range,
    install_cached_data_provider,
)
from doyoutrade.data.bars_cache_store import (
    InMemoryBarsCacheStore,
    RepositoryBarsCacheStore,
)
from doyoutrade.core.models import Bar, MarketContext
from doyoutrade.observability import initialize_observability, reset_observability
from doyoutrade.observability.debug_span_export import (
    debug_span_export_for_session,
    register_span_persist_sink,
)


def _bar(symbol: str, day: str, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=day,
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        volume=1000.0,
        amount=close * 1000.0,
    )


class FakeDataProvider:
    def __init__(self, bars_by_symbol: dict[str, list[Bar]] | None = None) -> None:
        self.bars_by_symbol = bars_by_symbol or {}
        self.calls: list[tuple[str, str, str, str]] = []
        self.market_context_calls = 0
        self.closed = False

    async def get_market_context(self) -> MarketContext:
        self.market_context_calls += 1
        return MarketContext(symbol_to_price={"600000.SH": 12.0})

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = "qfq",
    ) -> list[Bar]:
        self.calls.append((symbol, start_time, end_time, interval, adjust))
        start = start_time[:10]
        end = end_time[:10]
        return [
            bar
            for bar in self.bars_by_symbol.get(symbol, [])
            if start <= bar.timestamp[:10] <= end
        ]

    async def is_trading_day(self, value: str) -> bool:
        return value != "2026-01-04"

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        return [start, end]

    async def aclose(self) -> None:
        self.closed = True


class CachedBarsDataProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_preloaded_subrange_hits_memory_without_inner_call(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider(
            {
                symbol: [
                    _bar(symbol, "2026-01-01", 10.0),
                    _bar(symbol, "2026-01-02", 11.0),
                    _bar(symbol, "2026-01-03", 12.0),
                    _bar(symbol, "2026-01-04", 13.0),
                ]
            }
        )
        provider = CachedBarsDataProvider(inner, scope="backtest", run_id="run-1")

        await provider.preload_bars([symbol], "2026-01-01", "2026-01-04", interval="1d")
        got = await provider.get_bars(symbol, "2026-01-02", "2026-01-03", interval="1d")

        self.assertEqual([bar.timestamp for bar in got], ["2026-01-02", "2026-01-03"])
        self.assertEqual(inner.calls, [(symbol, "2026-01-01", "2026-01-04", "1d", "qfq")])

    async def test_out_of_range_request_lazy_loads_and_merges(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider(
            {
                symbol: [
                    _bar(symbol, "2026-01-01", 10.0),
                    _bar(symbol, "2026-01-02", 11.0),
                    _bar(symbol, "2026-01-03", 12.0),
                ]
            }
        )
        provider = CachedBarsDataProvider(inner, scope="backtest", run_id="run-1")

        await provider.preload_bars([symbol], "2026-01-01", "2026-01-01", interval="1d")
        got = await provider.get_bars(symbol, "2026-01-02", "2026-01-03", interval="1d")
        got_again = await provider.get_bars(symbol, "2026-01-02", "2026-01-03", interval="1d")

        self.assertEqual([bar.timestamp for bar in got], ["2026-01-02", "2026-01-03"])
        self.assertEqual([bar.timestamp for bar in got_again], ["2026-01-02", "2026-01-03"])
        self.assertEqual(
            inner.calls,
            [
                (symbol, "2026-01-01", "2026-01-01", "1d", "qfq"),
                (symbol, "2026-01-02", "2026-01-03", "1d", "qfq"),
            ],
        )

    async def test_live_request_ending_today_bypasses_cache(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider({symbol: [_bar(symbol, "2026-01-05", 15.0)]})
        provider = CachedBarsDataProvider(
            inner,
            scope="live",
            today_fn=lambda: date(2026, 1, 5),
        )

        first = await provider.get_bars(symbol, "2026-01-05", "2026-01-05", interval="1d")
        second = await provider.get_bars(symbol, "2026-01-05", "2026-01-05", interval="1d")

        self.assertEqual([bar.close for bar in first], [15.0])
        self.assertEqual([bar.close for bar in second], [15.0])
        self.assertEqual(
            inner.calls,
            [
                (symbol, "2026-01-05", "2026-01-05", "1d", "qfq"),
                (symbol, "2026-01-05", "2026-01-05", "1d", "qfq"),
            ],
        )

    async def test_live_historical_request_can_hit_cache(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider({symbol: [_bar(symbol, "2026-01-04", 14.0)]})
        provider = CachedBarsDataProvider(
            inner,
            scope="live",
            today_fn=lambda: date(2026, 1, 5),
        )

        first = await provider.get_bars(symbol, "2026-01-04", "2026-01-04", interval="1d")
        second = await provider.get_bars(symbol, "2026-01-04", "2026-01-04", interval="1d")

        self.assertEqual([bar.close for bar in first], [14.0])
        self.assertEqual([bar.close for bar in second], [14.0])
        self.assertEqual(inner.calls, [(symbol, "2026-01-04", "2026-01-04", "1d", "qfq")])

    async def test_live_cross_today_splits_history_cache_and_realtime_fetch(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider(
            {
                symbol: [
                    _bar(symbol, "2026-01-04", 14.0),
                    _bar(symbol, "2026-01-05", 15.0),
                ]
            }
        )
        provider = CachedBarsDataProvider(
            inner,
            scope="live",
            today_fn=lambda: date(2026, 1, 5),
        )

        first = await provider.get_bars(symbol, "2026-01-04", "2026-01-05", interval="1d")
        second = await provider.get_bars(symbol, "2026-01-04", "2026-01-05", interval="1d")

        self.assertEqual([bar.timestamp for bar in first], ["2026-01-04", "2026-01-05"])
        self.assertEqual([bar.timestamp for bar in second], ["2026-01-04", "2026-01-05"])
        # Realtime fetch starts at YESTERDAY (adjust-drift anchor overlap),
        # not today — the extra cached day is overwritten on merge.
        self.assertEqual(
            inner.calls,
            [
                (symbol, "2026-01-04", "2026-01-04", "1d", "qfq"),
                (symbol, "2026-01-04", "2026-01-05", "1d", "qfq"),
                (symbol, "2026-01-04", "2026-01-05", "1d", "qfq"),
            ],
        )

    async def test_live_cross_today_empty_realtime_falls_back_to_history(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider({symbol: [_bar(symbol, "2026-01-04", 14.0)]})
        provider = CachedBarsDataProvider(
            inner,
            scope="live",
            today_fn=lambda: date(2026, 1, 5),
        )
        # First call serves the history miss; second (realtime, starting
        # yesterday for the drift anchor) returns nothing at all.
        provider._inner.get_bars = AsyncMock(
            side_effect=[[_bar(symbol, "2026-01-04", 14.0)], []]
        )

        got = await provider.get_bars(symbol, "2026-01-04", "2026-01-05", interval="1d")

        self.assertEqual([bar.timestamp for bar in got], ["2026-01-04"])
        self.assertEqual(provider._inner.get_bars.await_count, 2)
        realtime_call = provider._inner.get_bars.await_args_list[1]
        self.assertEqual(realtime_call.args, (symbol, "2026-01-04", "2026-01-05"))

    async def test_live_cross_today_realtime_error_falls_back_to_history(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider({symbol: [_bar(symbol, "2026-01-04", 14.0)]})
        provider = CachedBarsDataProvider(
            inner,
            scope="live",
            today_fn=lambda: date(2026, 1, 5),
        )
        provider._inner.get_bars = AsyncMock(
            side_effect=[
                [_bar(symbol, "2026-01-04", 14.0)],
                RuntimeError("qmt down"),
            ]
        )

        got = await provider.get_bars(symbol, "2026-01-04", "2026-01-05", interval="1d")

        self.assertEqual([bar.timestamp for bar in got], ["2026-01-04"])
        self.assertEqual(provider._inner.get_bars.await_count, 2)

    async def test_delegates_non_bar_methods(self) -> None:
        inner = FakeDataProvider()
        provider = CachedBarsDataProvider(inner, scope="backtest", run_id="run-1")

        is_trading = await provider.is_trading_day("2026-01-05")
        dates = await provider.get_trading_dates("2026-01-01", "2026-01-02")
        await provider.aclose()

        self.assertTrue(is_trading)
        self.assertEqual(dates, ["2026-01-01", "2026-01-02"])
        self.assertTrue(inner.closed)

    async def test_get_market_context_backtest_skips_inner_realtime_call(self) -> None:
        inner = FakeDataProvider()
        provider = CachedBarsDataProvider(inner, scope="backtest", run_id="run-1")

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            market = await provider.get_market_context()

        self.assertEqual(market.symbol_to_price, {})
        self.assertEqual(market.symbol_to_tick, {})
        self.assertEqual(inner.market_context_calls, 0)
        self.assertEqual(emit.await_count, 1)
        event_name, payload = emit.await_args_list[0].args
        self.assertEqual(event_name, "cached_bars_market_context_backtest_skip")
        self.assertEqual(payload["scope"], "backtest")
        self.assertEqual(payload["run_id"], "run-1")
        self.assertEqual(payload["reason"], "overlay_owns_quotes_in_backtest")
        self.assertIn("hint", payload)

    async def test_get_market_context_live_delegates_to_inner(self) -> None:
        inner = FakeDataProvider()
        provider = CachedBarsDataProvider(inner, scope="live", run_id="run-2")

        market = await provider.get_market_context()

        self.assertEqual(market.symbol_to_price["600000.SH"], 12.0)
        self.assertEqual(inner.market_context_calls, 1)

    def test_expanded_backtest_bar_range_adds_21_calendar_days(self) -> None:
        start, end = expanded_backtest_bar_range(date(2026, 1, 22), date(2026, 2, 1))

        self.assertEqual(start, "2026-01-01")
        self.assertEqual(end, "2026-02-22")

    def test_expanded_range_falls_back_to_base_when_no_startup(self) -> None:
        # No startup_history → legacy 21-day expansion on both sides.
        start, end = expanded_backtest_bar_range(
            date(2026, 1, 22), date(2026, 2, 1), startup_history=None
        )
        self.assertEqual(start, "2026-01-01")
        self.assertEqual(end, "2026-02-22")

        # Zero / negative are also treated as "unknown" and fall back.
        start_zero, _ = expanded_backtest_bar_range(
            date(2026, 1, 22), date(2026, 2, 1), startup_history=0
        )
        self.assertEqual(start_zero, "2026-01-01")

    def test_expanded_range_uses_startup_history_when_provided(self) -> None:
        # startup_history=50 → left expansion = ceil(50 * 1.7) + 5 = 90
        # calendar days; right stays at the legacy 21-day window.
        # 2026-04-23 - 90 days = 2026-01-23.
        start, end = expanded_backtest_bar_range(
            date(2026, 4, 23),
            date(2026, 5, 23),
            startup_history=50,
        )
        self.assertEqual(start, "2026-01-23")
        # Right stays at +21 days.
        self.assertEqual(end, "2026-06-13")

    def test_expanded_range_small_startup_history_keeps_base_floor(self) -> None:
        # startup_history=5 → ceil(5*1.7)+5 = 9+5 = 14 < 21 floor → 21.
        start, end = expanded_backtest_bar_range(
            date(2026, 4, 23),
            date(2026, 5, 23),
            startup_history=5,
        )
        # 2026-04-23 - 21 days = 2026-04-02.
        self.assertEqual(start, "2026-04-02")
        self.assertEqual(end, "2026-06-13")

    async def test_build_backtest_cached_data_provider_preloads_expanded_range(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider({symbol: [_bar(symbol, "2026-01-10", 10.0)]})

        provider = await build_backtest_cached_data_provider(
            inner,
            run_id="run-1",
            symbols=[symbol, symbol, ""],
            range_start=date(2026, 1, 22),
            range_end=date(2026, 2, 1),
            interval="1d",
        )
        got = await provider.get_bars(symbol, "2026-01-10", "2026-01-10", interval="1d")

        self.assertIsInstance(provider, CachedBarsDataProvider)
        self.assertEqual([bar.timestamp for bar in got], ["2026-01-10"])
        self.assertEqual(inner.calls, [(symbol, "2026-01-01", "2026-02-22", "1d", "qfq")])

    async def test_build_backtest_cached_data_provider_honours_startup_history(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider({symbol: [_bar(symbol, "2026-01-25", 10.0)]})

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            provider = await build_backtest_cached_data_provider(
                inner,
                run_id="run-warmup-1",
                symbols=[symbol],
                range_start=date(2026, 4, 23),
                range_end=date(2026, 5, 23),
                interval="1d",
                startup_history=50,
            )

        self.assertIsInstance(provider, CachedBarsDataProvider)
        # Inner should have been called with the dynamically-expanded
        # window (left = 2026-01-23, right = 2026-06-13).
        preload_call = inner.calls[0]
        self.assertEqual(preload_call, (symbol, "2026-01-23", "2026-06-13", "1d", "qfq"))

        # The new debug event records the breakdown so an operator can
        # see exactly which formula decided the preload boundary.
        warmup_events = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "backtest_cache_preload_with_warmup"
        ]
        self.assertEqual(len(warmup_events), 1)
        payload = warmup_events[0]
        self.assertEqual(payload["run_id"], "run-warmup-1")
        self.assertEqual(payload["startup_history"], 50)
        self.assertEqual(payload["base_expansion_days"], BACKTEST_BARS_CACHE_EXPANSION_DAYS)
        self.assertEqual(payload["computed_left_days"], 90)
        self.assertEqual(payload["right_expansion_days"], BACKTEST_BARS_CACHE_EXPANSION_DAYS)
        self.assertTrue(payload["warmup_applied"])
        self.assertEqual(payload["preload_start"], "2026-01-23")
        self.assertEqual(payload["preload_end"], "2026-06-13")

    async def test_build_backtest_cached_data_provider_no_startup_history_event(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider({symbol: [_bar(symbol, "2026-01-10", 10.0)]})

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            await build_backtest_cached_data_provider(
                inner,
                run_id="run-no-warmup",
                symbols=[symbol],
                range_start=date(2026, 1, 22),
                range_end=date(2026, 2, 1),
                interval="1d",
            )

        warmup_events = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "backtest_cache_preload_with_warmup"
        ]
        self.assertEqual(len(warmup_events), 1)
        payload = warmup_events[0]
        self.assertIsNone(payload["startup_history"])
        self.assertEqual(payload["computed_left_days"], BACKTEST_BARS_CACHE_EXPANSION_DAYS)
        self.assertFalse(payload["warmup_applied"])

    def test_install_cached_data_provider_rebinds_worker_and_signal_bearing_component(self) -> None:
        """Trading strategy exposes data_provider on signal_component; flat strategy holds it on self."""

        class SignalComponent:
            def __init__(self, data_provider: Any) -> None:
                self.data_provider = data_provider

        class CompositeStrategy:
            def __init__(self, data_provider: Any) -> None:
                self.signal_component = SignalComponent(data_provider)

        class FlatStrategy:
            def __init__(self, data_provider: Any) -> None:
                self.data_provider = data_provider

        inner = FakeDataProvider()
        cached = CachedBarsDataProvider(inner, scope="backtest", run_id="run-1")

        composite_worker: Any = type("W", (), {})()
        composite_worker.data_provider = inner
        composite_worker.strategy = CompositeStrategy(inner)
        install_cached_data_provider(composite_worker, cached, previous=inner)
        self.assertIs(composite_worker.data_provider, cached)
        self.assertIs(composite_worker.strategy.signal_component.data_provider, cached)

        flat_worker: Any = type("W", (), {})()
        flat_worker.data_provider = inner
        flat_worker.strategy = FlatStrategy(inner)
        install_cached_data_provider(flat_worker, cached, previous=inner)
        self.assertIs(flat_worker.data_provider, cached)
        self.assertIs(flat_worker.strategy.data_provider, cached)

    def test_install_cached_data_provider_rebinds_signal_generator(self) -> None:
        """Backtest workers carry the live signal generator on
        ``worker.signal_generator`` (an InstanceSignalGenerator), NOT on
        ``worker.strategy``. The signal generator holds its own
        ``data_provider`` reference that feeds the per-bar BarsHistoryFetcher;
        if it is not rebound to the cache, every bar's history fetch bypasses
        the cache and re-hits the live source (the QMT-per-bar regression).
        """

        class FakeSignalGenerator:
            def __init__(self, data_provider: Any) -> None:
                self.data_provider = data_provider

        inner = FakeDataProvider()
        cached = CachedBarsDataProvider(inner, scope="backtest", run_id="run-1")

        # Worker with no .strategy attribute at all — only .signal_generator,
        # exactly like the backtest TradingWorker build.
        worker: Any = type("W", (), {})()
        worker.data_provider = inner
        worker.signal_generator = FakeSignalGenerator(inner)

        install_cached_data_provider(worker, cached, previous=inner)

        self.assertIs(worker.data_provider, cached)
        self.assertIs(
            worker.signal_generator.data_provider,
            cached,
            "signal_generator.data_provider must be rebound to the cache so "
            "per-bar history fetches hit the preload instead of the live source",
        )

    def test_install_cached_data_provider_signal_generator_mismatch_is_logged(self) -> None:
        """If the signal generator points at some OTHER provider (identity
        mismatch), the bypass is logged, not silently skipped."""

        class FakeSignalGenerator:
            def __init__(self, data_provider: Any) -> None:
                self.data_provider = data_provider

        inner = FakeDataProvider()
        other = FakeDataProvider()
        cached = CachedBarsDataProvider(inner, scope="backtest", run_id="run-1")

        worker: Any = type("W", (), {})()
        worker.data_provider = inner
        worker.signal_generator = FakeSignalGenerator(other)

        with self.assertLogs("doyoutrade.data.cached_bars", level="WARNING") as cm:
            install_cached_data_provider(worker, cached, previous=inner)

        # Mismatch left untouched (not force-overwritten) and surfaced.
        self.assertIs(worker.signal_generator.data_provider, other)
        self.assertTrue(
            any("did not match the expected previous provider" in line for line in cm.output)
        )

    async def test_emits_cache_events_for_preload_hit_miss_and_bypass(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider(
            {
                symbol: [
                    _bar(symbol, "2026-01-01", 10.0),
                    _bar(symbol, "2026-01-02", 11.0),
                    _bar(symbol, "2026-01-05", 15.0),
                ]
            }
        )
        provider = CachedBarsDataProvider(inner, scope="backtest", run_id="run-1")

        with patch("doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock) as emit:
            await provider.preload_bars([symbol], "2026-01-01", "2026-01-01", interval="1d")
            await provider.get_bars(symbol, "2026-01-01", "2026-01-01", interval="1d")
            await provider.get_bars(symbol, "2026-01-02", "2026-01-02", interval="1d")
            live = CachedBarsDataProvider(
                inner,
                scope="live",
                today_fn=lambda: date(2026, 1, 5),
            )
            await live.get_bars(symbol, "2026-01-04", "2026-01-05", interval="1d")

        payloads = [call.args[1] for call in emit.await_args_list]
        operations = [payload["operation"] for payload in payloads]
        self.assertIn("preload", operations)
        self.assertIn("hit", operations)
        self.assertIn("miss", operations)
        self.assertIn("bypass", operations)
        self.assertIn("split", operations)
        self.assertIn("split_result", operations)
        for payload in payloads:
            self.assertIn("symbol", payload)
            self.assertIn("interval", payload)
            self.assertIn("requested_start", payload)
            self.assertIn("requested_end", payload)
            self.assertIn("returned_count", payload)

    async def test_preload_event_is_recorded_on_exported_cache_span(self) -> None:
        symbol = "600000.SH"
        inner = FakeDataProvider({symbol: [_bar(symbol, "2026-01-01", 10.0)]})
        provider = CachedBarsDataProvider(inner, scope="backtest", run_id="run-1")
        rows: list[dict[str, Any]] = []

        initialize_observability(service_name="doyoutrade-cache-test")
        register_span_persist_sink(lambda row: rows.append(dict(row)))
        try:
            with debug_span_export_for_session("sess-1", "backtest"):
                await provider.preload_bars([symbol], "2026-01-01", "2026-01-01", interval="1d")
        finally:
            register_span_persist_sink(None)
            reset_observability()

        preload_rows = [row for row in rows if row["name"] == "data.cache.preload_bars"]
        self.assertEqual(len(preload_rows), 1)
        self.assertEqual(preload_rows[0]["session_id"], "sess-1")
        self.assertEqual(preload_rows[0]["span_source"], "backtest")
        events = preload_rows[0]["attributes"].get("_events", [])
        preload_events = [
            event
            for event in events
            if event["event_type"] == "data_cache.get_bars"
            and event["payload"].get("operation") == "preload"
        ]
        self.assertEqual(len(preload_events), 1)


class BarsCacheStoreInvalidateTests(unittest.IsolatedAsyncioTestCase):
    async def test_inmemory_invalidate_drops_entry_and_returns_count(self) -> None:
        symbol = "000636.SZ"
        store = InMemoryBarsCacheStore()
        await store.record_fetch(
            provider="qmt",
            symbol=symbol,
            interval="1d",
            start="2026-01-01",
            end="2026-01-03",
            bars=[_bar(symbol, "2026-01-01", 130.0), _bar(symbol, "2026-01-02", 131.0)],
        )

        removed = await store.invalidate(provider="qmt", symbol=symbol, interval="1d")

        self.assertEqual(removed, 2)
        self.assertEqual(
            await store.covered_ranges(provider="qmt", symbol=symbol, interval="1d"), []
        )
        self.assertEqual(
            await store.bars_in_range(
                provider="qmt", symbol=symbol, interval="1d",
                start="2026-01-01", end="2026-01-03",
            ),
            [],
        )
        # Idempotent: a second invalidate finds nothing.
        self.assertEqual(
            await store.invalidate(provider="qmt", symbol=symbol, interval="1d"), 0
        )

    async def test_inmemory_invalidate_only_targets_matching_key(self) -> None:
        symbol = "000636.SZ"
        store = InMemoryBarsCacheStore()
        await store.record_fetch(
            provider="qmt", symbol=symbol, interval="1d",
            start="2026-01-01", end="2026-01-01",
            bars=[_bar(symbol, "2026-01-01", 130.0)],
        )
        await store.record_fetch(
            provider="qmt", symbol=symbol, interval="1d",
            start="2026-01-01", end="2026-01-01",
            bars=[_bar(symbol, "2026-01-01", 99.0)],
            adjust="none",
        )

        removed = await store.invalidate(
            provider="qmt", symbol=symbol, interval="1d", adjust="qfq"
        )

        self.assertEqual(removed, 1)
        none_bars = await store.bars_in_range(
            provider="qmt", symbol=symbol, interval="1d",
            start="2026-01-01", end="2026-01-01", adjust="none",
        )
        self.assertEqual([bar.close for bar in none_bars], [99.0])

    async def test_repository_store_invalidate_delegates_to_repo(self) -> None:
        class FakeRepo:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str, str]] = []

            async def invalidate_symbol_cache(
                self, *, provider: str, symbol: str, interval: str, adjust: str
            ) -> int:
                self.calls.append((provider, symbol, interval, adjust))
                return 7

        repo = FakeRepo()
        store = RepositoryBarsCacheStore(repo)

        removed = await store.invalidate(provider="qmt", symbol="000636.SZ", interval="1d")

        self.assertEqual(removed, 7)
        self.assertEqual(repo.calls, [("qmt", "000636.SZ", "1d", "qfq")])


class AdjustDriftSelfHealTests(unittest.IsolatedAsyncioTestCase):
    """Self-healing of qfq adjust-factor drift (e.g. 000636.SZ 2025-06-11 ~130 → ~13)."""

    SYMBOL = "000636.SZ"

    def _old_bars(self, days: list[str]) -> list[Bar]:
        return [_bar(self.SYMBOL, day, 130.0 + i) for i, day in enumerate(days)]

    def _new_bars(self, days: list[str]) -> list[Bar]:
        return [_bar(self.SYMBOL, day, 13.0 + i * 0.1) for i, day in enumerate(days)]

    async def _seed_store(self, store: InMemoryBarsCacheStore, days: list[str]) -> None:
        seed = CachedBarsDataProvider(
            FakeDataProvider({self.SYMBOL: self._old_bars(days)}),
            scope="backtest",
            run_id="run-seed",
            store=store,
        )
        await seed.preload_bars([self.SYMBOL], days[0], days[-1], interval="1d")

    async def test_miss_path_drift_invalidates_emits_event_and_returns_fresh(self) -> None:
        store = InMemoryBarsCacheStore()
        await self._seed_store(store, ["2026-01-01", "2026-01-02"])
        new_days = ["2026-01-01", "2026-01-02", "2026-01-03"]
        provider = CachedBarsDataProvider(
            FakeDataProvider({self.SYMBOL: self._new_bars(new_days)}),
            scope="backtest",
            run_id="run-new",
            store=store,
        )

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit, self.assertLogs("doyoutrade.data.cached_bars", level="WARNING") as cm:
            got = await provider.get_bars(self.SYMBOL, "2026-01-01", "2026-01-03", interval="1d")

        self.assertEqual([round(bar.close, 1) for bar in got], [13.0, 13.1, 13.2])
        payloads = [call.args[1] for call in emit.await_args_list]
        drift_events = [p for p in payloads if p["operation"] == "adjust_drift_invalidated"]
        self.assertEqual(len(drift_events), 1)
        event = drift_events[0]
        self.assertTrue(event["drifted"])
        self.assertEqual(event["reason"], "adjust_factor_changed")
        self.assertEqual(event["trigger"], "history_miss")
        self.assertGreater(event["max_rel_deviation"], 0.5)
        self.assertIn("hint", event)
        self.assertTrue(any("adjust-factor drift detected" in line for line in cm.output))
        # The store holds only the rescaled bars — no old-factor remnants.
        cached = await store.bars_in_range(
            provider="unknown", symbol=self.SYMBOL, interval="1d",
            start="2026-01-01", end="2026-01-03",
        )
        self.assertEqual([round(bar.close, 1) for bar in cached], [13.0, 13.1, 13.2])

    async def test_miss_path_without_drift_keeps_existing_behavior(self) -> None:
        store = InMemoryBarsCacheStore()
        days = ["2026-01-01", "2026-01-02"]
        bars = self._new_bars(days + ["2026-01-03"])
        inner = FakeDataProvider({self.SYMBOL: bars})
        provider = CachedBarsDataProvider(inner, scope="backtest", run_id="run-1", store=store)
        await provider.preload_bars([self.SYMBOL], "2026-01-01", "2026-01-02", interval="1d")

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            got = await provider.get_bars(self.SYMBOL, "2026-01-01", "2026-01-03", interval="1d")

        self.assertEqual([bar.timestamp for bar in got], ["2026-01-01", "2026-01-02", "2026-01-03"])
        operations = [call.args[1]["operation"] for call in emit.await_args_list]
        self.assertNotIn("adjust_drift_invalidated", operations)
        self.assertNotIn("adjust_drift_invalidate_failed", operations)

    async def test_preload_revalidation_detects_drift_and_refetches(self) -> None:
        days = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
        store = InMemoryBarsCacheStore()
        await self._seed_store(store, days)
        provider = CachedBarsDataProvider(
            FakeDataProvider({self.SYMBOL: self._new_bars(days)}),
            scope="backtest",
            run_id="run-reval",
            store=store,
        )

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            await provider.preload_bars([self.SYMBOL], days[0], days[-1], interval="1d")
            got = await provider.get_bars(self.SYMBOL, "2026-01-02", "2026-01-04", interval="1d")

        # The fully-covered cache window did NOT silently serve old-factor
        # bars: the anchor revalidation invalidated and refetched.
        self.assertEqual([round(bar.close, 1) for bar in got], [13.1, 13.2, 13.3])
        payloads = [call.args[1] for call in emit.await_args_list]
        drift_events = [p for p in payloads if p["operation"] == "adjust_drift_invalidated"]
        self.assertEqual(len(drift_events), 1)
        self.assertEqual(drift_events[0]["trigger"], "preload_revalidate")
        preload_events = [p for p in payloads if p["operation"] == "preload"]
        self.assertEqual(len(preload_events), 1)
        self.assertTrue(preload_events[0]["adjust_revalidated"])

    async def test_preload_revalidation_anchor_empty_emits_event_and_serves_cache(self) -> None:
        days = ["2026-01-01", "2026-01-02", "2026-01-03"]
        store = InMemoryBarsCacheStore()
        await self._seed_store(store, days)
        # Upstream has no data at all → anchor window comes back empty.
        provider = CachedBarsDataProvider(
            FakeDataProvider({}), scope="backtest", run_id="run-anchor-empty", store=store
        )

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit, self.assertLogs("doyoutrade.data.cached_bars", level="WARNING") as cm:
            await provider.preload_bars([self.SYMBOL], days[0], days[-1], interval="1d")
            got = await provider.get_bars(self.SYMBOL, days[0], days[-1], interval="1d")

        # Conservative pass: cached (old-factor) bars still served.
        self.assertEqual([bar.close for bar in got], [130.0, 131.0, 132.0])
        payloads = [call.args[1] for call in emit.await_args_list]
        anchor_events = [
            p for p in payloads if p["operation"] == "adjust_revalidate_anchor_unavailable"
        ]
        self.assertEqual(len(anchor_events), 1)
        self.assertEqual(anchor_events[0]["reason"], "anchor_fetch_empty")
        self.assertIn("hint", anchor_events[0])
        preload_events = [p for p in payloads if p["operation"] == "preload"]
        self.assertFalse(preload_events[0]["adjust_revalidated"])
        self.assertTrue(any("anchor fetch returned 0 bars" in line for line in cm.output))

    async def test_preload_revalidation_anchor_error_emits_event_and_serves_cache(self) -> None:
        days = ["2026-01-01", "2026-01-02", "2026-01-03"]
        store = InMemoryBarsCacheStore()
        await self._seed_store(store, days)
        inner = FakeDataProvider({})
        provider = CachedBarsDataProvider(
            inner, scope="backtest", run_id="run-anchor-error", store=store
        )
        provider._inner.get_bars = AsyncMock(side_effect=RuntimeError("qmt down"))

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit, self.assertLogs("doyoutrade.data.cached_bars", level="WARNING") as cm:
            await provider.preload_bars([self.SYMBOL], days[0], days[-1], interval="1d")
            got = await provider.get_bars(self.SYMBOL, days[0], days[-1], interval="1d")

        self.assertEqual([bar.close for bar in got], [130.0, 131.0, 132.0])
        payloads = [call.args[1] for call in emit.await_args_list]
        anchor_events = [
            p for p in payloads if p["operation"] == "adjust_revalidate_anchor_unavailable"
        ]
        self.assertEqual(len(anchor_events), 1)
        self.assertEqual(anchor_events[0]["reason"], "anchor_fetch_error")
        self.assertEqual(anchor_events[0]["error_type"], "RuntimeError")
        self.assertTrue(any("anchor fetch failed" in line for line in cm.output))

    async def test_preload_revalidation_clean_cache_flags_revalidated(self) -> None:
        days = ["2026-01-01", "2026-01-02", "2026-01-03"]
        store = InMemoryBarsCacheStore()
        bars = self._new_bars(days)
        seed = CachedBarsDataProvider(
            FakeDataProvider({self.SYMBOL: bars}),
            scope="backtest", run_id="run-seed", store=store,
        )
        await seed.preload_bars([self.SYMBOL], days[0], days[-1], interval="1d")
        provider = CachedBarsDataProvider(
            FakeDataProvider({self.SYMBOL: bars}),
            scope="backtest", run_id="run-clean", store=store,
        )

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            await provider.preload_bars([self.SYMBOL], days[0], days[-1], interval="1d")

        payloads = [call.args[1] for call in emit.await_args_list]
        operations = [p["operation"] for p in payloads]
        self.assertNotIn("adjust_drift_invalidated", operations)
        preload_events = [p for p in payloads if p["operation"] == "preload"]
        self.assertTrue(preload_events[0]["adjust_revalidated"])

    async def test_live_split_yesterday_anchor_drift_refetches_history(self) -> None:
        history_days = ["2026-01-02", "2026-01-03", "2026-01-04"]
        store = InMemoryBarsCacheStore()
        await self._seed_store(store, history_days)
        new_days = ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
        live = CachedBarsDataProvider(
            FakeDataProvider({self.SYMBOL: self._new_bars(new_days)}),
            scope="live",
            today_fn=lambda: date(2026, 1, 5),
            store=store,
        )

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit, self.assertLogs("doyoutrade.data.cached_bars", level="WARNING") as cm:
            got = await live.get_bars(self.SYMBOL, "2026-01-02", "2026-01-05", interval="1d")

        # All four bars carry the NEW adjust factor — the cached old-factor
        # history was detected via the yesterday anchor and refetched.
        self.assertEqual([bar.timestamp for bar in got], new_days)
        self.assertEqual([round(bar.close, 1) for bar in got], [13.0, 13.1, 13.2, 13.3])
        payloads = [call.args[1] for call in emit.await_args_list]
        drift_events = [p for p in payloads if p["operation"] == "adjust_drift_invalidated"]
        self.assertEqual(len(drift_events), 1)
        self.assertEqual(drift_events[0]["trigger"], "live_split_anchor")
        self.assertEqual(drift_events[0]["reason"], "adjust_factor_changed")
        self.assertTrue(any("adjust-factor drift detected" in line for line in cm.output))
        # The store now holds the new-factor history.
        cached = await store.bars_in_range(
            provider="unknown", symbol=self.SYMBOL, interval="1d",
            start="2026-01-02", end="2026-01-04",
        )
        self.assertEqual([round(bar.close, 1) for bar in cached], [13.0, 13.1, 13.2])

    async def test_live_split_yesterday_anchor_without_drift_keeps_history(self) -> None:
        history_days = ["2026-01-02", "2026-01-03", "2026-01-04"]
        new_days = ["2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]
        store = InMemoryBarsCacheStore()
        bars = self._new_bars(new_days)
        seed = CachedBarsDataProvider(
            FakeDataProvider({self.SYMBOL: bars}),
            scope="backtest", run_id="run-seed", store=store,
        )
        await seed.preload_bars([self.SYMBOL], history_days[0], history_days[-1], interval="1d")
        live = CachedBarsDataProvider(
            FakeDataProvider({self.SYMBOL: bars}),
            scope="live",
            today_fn=lambda: date(2026, 1, 5),
            store=store,
        )

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            got = await live.get_bars(self.SYMBOL, "2026-01-02", "2026-01-05", interval="1d")

        self.assertEqual([bar.timestamp for bar in got], new_days)
        operations = [call.args[1]["operation"] for call in emit.await_args_list]
        self.assertNotIn("adjust_drift_invalidated", operations)

    async def test_invalidate_failure_is_logged_evented_and_reraised(self) -> None:
        store = InMemoryBarsCacheStore()
        await self._seed_store(store, ["2026-01-01", "2026-01-02"])
        provider = CachedBarsDataProvider(
            FakeDataProvider({self.SYMBOL: self._new_bars(["2026-01-01", "2026-01-02", "2026-01-03"])}),
            scope="backtest",
            run_id="run-fail",
            store=store,
        )
        store.invalidate = AsyncMock(side_effect=RuntimeError("db gone"))  # type: ignore[method-assign]

        with patch(
            "doyoutrade.data.cached_bars.emit_debug_event", new_callable=AsyncMock
        ) as emit, self.assertLogs("doyoutrade.data.cached_bars", level="ERROR") as cm:
            with self.assertRaises(RuntimeError):
                await provider.get_bars(self.SYMBOL, "2026-01-01", "2026-01-03", interval="1d")

        payloads = [call.args[1] for call in emit.await_args_list]
        failed_events = [
            p for p in payloads if p["operation"] == "adjust_drift_invalidate_failed"
        ]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["reason"], "store_invalidate_error")
        self.assertEqual(failed_events[0]["error_type"], "RuntimeError")
        self.assertTrue(any("cache invalidate failed" in line for line in cm.output))


if __name__ == "__main__":
    unittest.main()
