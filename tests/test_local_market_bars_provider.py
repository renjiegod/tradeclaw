from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

from doyoutrade.core.models import Bar, MarketContext
from doyoutrade.data.local_market_bars import LocalHistoricalBarsDataProvider


def _bar(symbol: str, timestamp: str, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        volume=1000.0,
        amount=close * 1000.0,
        adjust_type="qfq",
    )


def _row(symbol: str, timestamp: str, close: float) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "volume": 1000.0,
        "amount": close * 1000.0,
        "adjust_type": "qfq",
    }


class FakeRepository:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        read_error: Exception | None = None,
        upsert_error: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.read_error = read_error
        self.upsert_error = upsert_error
        self.read_calls: list[dict[str, Any]] = []
        self.upsert_calls: list[dict[str, Any]] = []

    async def bars_in_range(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.read_calls.append(kwargs)
        if self.read_error is not None:
            raise self.read_error
        return list(self.rows)

    async def upsert_bars(self, **kwargs: Any) -> int:
        self.upsert_calls.append(kwargs)
        if self.upsert_error is not None:
            raise self.upsert_error
        return len(kwargs["bars"])


class FakeUpstream:
    capabilities = object()

    def __init__(
        self,
        bars: list[Bar] | None = None,
        *,
        fetch_error: Exception | None = None,
    ) -> None:
        self.bars = bars or []
        self.fetch_error = fetch_error
        self.calls: list[tuple[str, str, str, str, str]] = []
        self.closed = False

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
        if self.fetch_error is not None:
            raise self.fetch_error
        return list(self.bars)

    async def get_market_context(self) -> MarketContext:
        return MarketContext(symbol_to_price={"600000.SH": 12.0})

    async def is_trading_day(self, value: str) -> bool:
        return value != "2026-01-04"

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        return [start, end]

    async def aclose(self) -> None:
        self.closed = True


class LocalHistoricalBarsDataProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_hit_does_not_call_upstream(self) -> None:
        repo = FakeRepository(rows=[_row("600000.SH", "2026-01-02", 11.0)])
        upstream = FakeUpstream()
        provider = LocalHistoricalBarsDataProvider(
            repo,
            upstream,
            provider="qmt",
            adjust="qfq",
        )

        with patch(
            "doyoutrade.data.local_market_bars.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            got = await provider.get_bars(
                "600000.SH",
                "2026-01-01",
                "2026-01-03",
                interval="1d",
                adjust="qfq",
            )

        self.assertEqual([bar.timestamp for bar in got], ["2026-01-02"])
        self.assertEqual(got[0].close, 11.0)
        self.assertEqual(upstream.calls, [])
        self.assertEqual(len(repo.read_calls), 1)
        read_call = repo.read_calls[0]
        self.assertEqual(read_call["provider"], "qmt")
        self.assertEqual(read_call["adjust"], "qfq")
        self.assertEqual(read_call["symbol"], "600000.SH")
        self.assertEqual(read_call["interval"], "1d")
        self.assertEqual(read_call["start"], datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(read_call["end"], datetime(2026, 1, 3, 23, 59, 59, 999999, timezone.utc))
        event_names = [call.args[0] for call in emit.await_args_list]
        self.assertIn("market_data.get_bars.hit", event_names)

    async def test_missing_range_calls_upstream_and_upserts(self) -> None:
        bar = _bar("600000.SH", "2026-01-02", 11.0)
        repo = FakeRepository()
        upstream = FakeUpstream([bar])
        provider = LocalHistoricalBarsDataProvider(repo, upstream, provider="qmt")

        with patch(
            "doyoutrade.data.local_market_bars.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            got = await provider.get_bars(
                "600000.SH",
                "2026-01-01",
                "2026-01-03",
                interval="1d",
            )

        self.assertEqual(got, [bar])
        self.assertEqual(
            upstream.calls,
            [("600000.SH", "2026-01-01", "2026-01-03", "1d", "qfq")],
        )
        self.assertEqual(len(repo.upsert_calls), 1)
        upsert = repo.upsert_calls[0]
        self.assertEqual(upsert["provider"], "qmt")
        self.assertEqual(upsert["adjust"], "qfq")
        self.assertEqual(upsert["interval"], "1d")
        self.assertEqual(upsert["bars"][0]["timestamp"], "2026-01-02")
        self.assertEqual(upsert["bars"][0]["adjust_type"], "qfq")
        events = [(call.args[0], call.args[1]) for call in emit.await_args_list]
        self.assertIn("market_data.get_bars.miss", [event for event, _ in events])
        gap_payloads = [
            payload for event, payload in events if event == "market_data.get_bars.gap_fetch"
        ]
        self.assertEqual(len(gap_payloads), 1)
        self.assertEqual(gap_payloads[0]["returned_count"], 1)
        self.assertEqual(gap_payloads[0]["upserted_count"], 1)
        self.assertEqual(
            gap_payloads[0]["missing_ranges"],
            [{"start": "2026-01-01", "end": "2026-01-03", "reason": "local_empty"}],
        )

    async def test_five_minute_date_bounds_read_local_store(self) -> None:
        repo = FakeRepository(rows=[_row("600000.SH", "2026-01-02T09:35:00", 11.0)])
        upstream = FakeUpstream()
        provider = LocalHistoricalBarsDataProvider(
            repo,
            upstream,
            provider="qmt",
            adjust="qfq",
        )

        got = await provider.get_bars(
            "600000.SH",
            "2026-01-02",
            "2026-01-03",
            interval="5m",
        )

        self.assertEqual([bar.timestamp for bar in got], ["2026-01-02T09:35:00"])
        self.assertEqual(upstream.calls, [])
        read_call = repo.read_calls[0]
        self.assertEqual(read_call["interval"], "5m")
        self.assertEqual(read_call["start"], datetime(2026, 1, 2, tzinfo=timezone.utc))
        self.assertEqual(
            read_call["end"],
            datetime(2026, 1, 3, 23, 59, 59, 999999, timezone.utc),
        )

    async def test_five_minute_gap_fetch_upserts_timezone_aware_timestamp(self) -> None:
        bar = _bar("600000.SH", "2026-01-02T09:35:00", 11.0)
        repo = FakeRepository()
        upstream = FakeUpstream([bar])
        provider = LocalHistoricalBarsDataProvider(repo, upstream, provider="qmt")

        got = await provider.get_bars(
            "600000.SH",
            "2026-01-02",
            "2026-01-03",
            interval="5m",
        )

        self.assertEqual(got, [bar])
        self.assertEqual(
            upstream.calls,
            [("600000.SH", "2026-01-02", "2026-01-03", "5m", "qfq")],
        )
        upsert = repo.upsert_calls[0]
        self.assertEqual(upsert["interval"], "5m")
        self.assertEqual(upsert["bars"][0]["timestamp"], "2026-01-02T09:35:00+00:00")

    async def test_five_minute_gap_fetch_rejects_date_only_bar_timestamp(self) -> None:
        repo = FakeRepository()
        upstream = FakeUpstream([_bar("600000.SH", "2026-01-02", 11.0)])
        provider = LocalHistoricalBarsDataProvider(repo, upstream, provider="qmt")

        with patch(
            "doyoutrade.data.local_market_bars.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            with self.assertRaisesRegex(ValueError, "market_data_bar_timestamp_invalid"):
                await provider.get_bars(
                    "600000.SH",
                    "2026-01-02",
                    "2026-01-03",
                    interval="5m",
                )

        self.assertEqual(repo.upsert_calls, [])
        failed_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.get_bars.failed"
        ][0]
        self.assertEqual(failed_payload["error_code"], "market_data_upsert_failed")
        self.assertEqual(failed_payload["error_type"], "ValueError")
        self.assertIn("market_data_bar_timestamp_invalid", failed_payload["error"])

    async def test_unsupported_interval_raises_and_emits_event(self) -> None:
        repo = FakeRepository()
        upstream = FakeUpstream()
        provider = LocalHistoricalBarsDataProvider(repo, upstream, provider="qmt")

        with patch(
            "doyoutrade.data.local_market_bars.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            with self.assertRaisesRegex(ValueError, "market_data_interval_unsupported"):
                await provider.get_bars(
                    "600000.SH",
                    "2026-01-01",
                    "2026-01-03",
                    interval="15m",
                )

        self.assertEqual(repo.read_calls, [])
        self.assertEqual(upstream.calls, [])
        failed_payloads = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.get_bars.failed"
        ]
        self.assertEqual(len(failed_payloads), 1)
        self.assertEqual(
            failed_payloads[0]["error_code"],
            "market_data_interval_unsupported",
        )
        self.assertEqual(failed_payloads[0]["error"], "unsupported interval: 15m")

    async def test_local_db_failure_raises_without_upstream_fallback(self) -> None:
        repo = FakeRepository(read_error=RuntimeError("db down"))
        upstream = FakeUpstream()
        provider = LocalHistoricalBarsDataProvider(repo, upstream, provider="qmt")

        with patch(
            "doyoutrade.data.local_market_bars.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            with self.assertRaisesRegex(RuntimeError, "db down"):
                await provider.get_bars(
                    "600000.SH",
                    "2026-01-01",
                    "2026-01-03",
                    interval="1d",
                )

        self.assertEqual(upstream.calls, [])
        failed_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.get_bars.failed"
        ][0]
        self.assertEqual(failed_payload["error_code"], "market_data_local_read_failed")
        self.assertEqual(failed_payload["error_type"], "RuntimeError")
        self.assertEqual(failed_payload["error"], "db down")

    async def test_upsert_failure_emits_structured_event_and_raises(self) -> None:
        repo = FakeRepository(upsert_error=RuntimeError("write down"))
        upstream = FakeUpstream([_bar("600000.SH", "2026-01-02", 11.0)])
        provider = LocalHistoricalBarsDataProvider(repo, upstream, provider="qmt")

        with patch(
            "doyoutrade.data.local_market_bars.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            with self.assertRaisesRegex(RuntimeError, "write down"):
                await provider.get_bars(
                    "600000.SH",
                    "2026-01-01",
                    "2026-01-03",
                    interval="1d",
                )

        self.assertEqual(len(upstream.calls), 1)
        failed_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.get_bars.failed"
        ][0]
        self.assertEqual(failed_payload["error_code"], "market_data_upsert_failed")
        self.assertEqual(failed_payload["error_type"], "RuntimeError")
        self.assertEqual(failed_payload["error"], "write down")
        self.assertEqual(failed_payload["returned_count"], 1)
        self.assertIn("missing_ranges", failed_payload)

    async def test_upstream_failure_emits_structured_event_and_raises(self) -> None:
        repo = FakeRepository()
        upstream = FakeUpstream(fetch_error=RuntimeError("source down"))
        provider = LocalHistoricalBarsDataProvider(repo, upstream, provider="qmt")

        with patch(
            "doyoutrade.data.local_market_bars.emit_debug_event",
            new_callable=AsyncMock,
        ) as emit:
            with self.assertRaisesRegex(RuntimeError, "source down"):
                await provider.get_bars(
                    "600000.SH",
                    "2026-01-01",
                    "2026-01-03",
                    interval="1d",
                )

        self.assertEqual(repo.upsert_calls, [])
        failed_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.get_bars.failed"
        ][0]
        self.assertEqual(
            failed_payload["error_code"],
            "market_data_upstream_fetch_failed",
        )
        self.assertEqual(failed_payload["error_type"], "RuntimeError")
        self.assertEqual(failed_payload["error"], "source down")
        self.assertIn("missing_ranges", failed_payload)

    async def test_non_bar_methods_delegate_to_upstream(self) -> None:
        upstream = FakeUpstream()
        provider = LocalHistoricalBarsDataProvider(
            FakeRepository(),
            upstream,
            provider="qmt",
        )

        market = await provider.get_market_context()
        is_trading = await provider.is_trading_day("2026-01-05")
        dates = await provider.get_trading_dates("2026-01-01", "2026-01-02")
        await provider.aclose()

        self.assertIs(provider.capabilities, upstream.capabilities)
        self.assertEqual(market.symbol_to_price["600000.SH"], 12.0)
        self.assertTrue(is_trading)
        self.assertEqual(dates, ["2026-01-01", "2026-01-02"])
        self.assertTrue(upstream.closed)

    async def test_aclose_is_noop_when_upstream_has_no_close(self) -> None:
        class UpstreamWithoutClose:
            async def get_market_context(self) -> MarketContext:
                return MarketContext(symbol_to_price={})

            async def is_trading_day(self, value: str) -> bool:
                return True

            async def get_trading_dates(self, start: str, end: str) -> list[str]:
                return []

        provider = LocalHistoricalBarsDataProvider(
            FakeRepository(),
            UpstreamWithoutClose(),
            provider="qmt",
        )

        await provider.aclose()


if __name__ == "__main__":
    unittest.main()
