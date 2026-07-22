from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

from doyoutrade.core.models import Bar
from doyoutrade.data.cloud_profile import (
    CloudPlan,
    CloudProfile,
    CloudQuota,
    CloudRecommendations,
)
from doyoutrade.data.market_sync import (
    MarketDataSyncService,
    _split_into_coverage_segments,
)


def _bar(symbol: str, timestamp: str = "2026-06-01") -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=timestamp,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=100.0,
        amount=150.0,
        adjust_type="qfq",
    )


def _weekly_timestamps(start: str, end: str, *, interval: str) -> list[str]:
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    values: list[str] = []
    day = start_day
    while day <= end_day:
        raw = day.isoformat()
        if interval == "1d":
            values.append(raw)
        else:
            values.extend(_intraday_timestamps(raw))
        day = date.fromordinal(day.toordinal() + 7)
    end_raw = end_day.isoformat()
    end_values = [end_raw] if interval == "1d" else _intraday_timestamps(end_raw)
    if values[-1] != end_values[-1]:
        values.extend(end_values)
    return values


def _weekly_close_only_timestamps(start: str, end: str) -> list[str]:
    start_day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    values: list[str] = []
    day = start_day
    while day <= end_day:
        values.append(f"{day.isoformat()}T15:00:00")
        day = date.fromordinal(day.toordinal() + 7)
    end_value = f"{end_day.isoformat()}T15:00:00"
    if values[-1] != end_value:
        values.append(end_value)
    return values


def _daily_timestamps_with_gap(
    *,
    start: str,
    gap_after: str,
    gap_before: str,
    end: str,
    step_days: int = 7,
) -> list[str]:
    """Build daily timestamps with a calendar hole between two trading segments."""
    gap_after_day = date.fromisoformat(gap_after)
    gap_before_day = date.fromisoformat(gap_before)
    values: list[str] = []
    day = date.fromisoformat(start)
    end_day = date.fromisoformat(end)
    while day <= gap_after_day:
        values.append(day.isoformat())
        day = date.fromordinal(day.toordinal() + step_days)
    if values[-1] != gap_after:
        values.append(gap_after)
    day = gap_before_day
    while day <= end_day:
        values.append(day.isoformat())
        day = date.fromordinal(day.toordinal() + step_days)
    if values[-1] != end:
        values.append(end)
    return values


def _intraday_timestamps(day: str) -> list[str]:
    morning = datetime.fromisoformat(f"{day}T09:35:00")
    afternoon = datetime.fromisoformat(f"{day}T13:05:00")
    values = [
        (morning + timedelta(minutes=5 * index)).strftime("%Y-%m-%dT%H:%M:%S")
        for index in range(24)
    ]
    values.extend(
        (afternoon + timedelta(minutes=5 * index)).strftime("%Y-%m-%dT%H:%M:%S")
        for index in range(24)
    )
    return values


class FakeCatalogRepository:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def list_page(
        self,
        *,
        q: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls.append({"q": q, "limit": limit, "offset": offset})
        return self.rows[offset : offset + limit], len(self.rows)


class FakeMarketRepository:
    def __init__(
        self,
        states: dict[tuple[str, str], dict[str, Any]] | None = None,
        *,
        state_error: Exception | None = None,
        upsert_error: Exception | None = None,
        success_error: Exception | None = None,
        failure_error: Exception | None = None,
        events: list[str] | None = None,
        anchor_bars: list[dict[str, Any]] | None = None,
        anchor_error: Exception | None = None,
    ) -> None:
        self.states = states or {}
        self.state_error = state_error
        self.upsert_error = upsert_error
        self.success_error = success_error
        self.failure_error = failure_error
        self.events = events
        self.anchor_bars = anchor_bars or []
        self.anchor_error = anchor_error
        self.state_calls: list[dict[str, Any]] = []
        self.upserts: list[dict[str, Any]] = []
        self.successes: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.anchor_calls: list[dict[str, Any]] = []

    async def get_sync_state(self, **kwargs: Any) -> dict[str, Any] | None:
        self.state_calls.append(kwargs)
        if self.state_error is not None:
            raise self.state_error
        return self.states.get((kwargs["symbol"], kwargs["interval"]))

    async def bars_in_range(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.anchor_calls.append(kwargs)
        if self.anchor_error is not None:
            raise self.anchor_error
        return list(self.anchor_bars)

    async def upsert_bars(self, **kwargs: Any) -> int:
        if self.upsert_error is not None:
            raise self.upsert_error
        self.upserts.append(kwargs)
        return len(kwargs["bars"])

    async def mark_sync_success(self, **kwargs: Any) -> None:
        if self.success_error is not None:
            raise self.success_error
        self.successes.append(kwargs)

    async def mark_sync_failure(self, **kwargs: Any) -> None:
        if self.events is not None:
            self.events.append("mark_sync_failure")
        if self.failure_error is not None:
            raise self.failure_error
        self.failures.append(kwargs)


class FakeProvider:
    def __init__(
        self,
        *,
        failure: tuple[str, str] | None = None,
        intraday_date_only: tuple[str, str] | None = None,
        empty_result: tuple[str, str] | None = None,
        last_used_provider: str | None = None,
        timestamp_override: dict[tuple[str, str], str | list[str]] | None = None,
        fail_on_call_index: int | None = None,
    ) -> None:
        self.failure = failure
        self.intraday_date_only = intraday_date_only
        self.empty_result = empty_result
        self.last_used_provider = last_used_provider
        self.timestamp_override = timestamp_override or {}
        self.fail_on_call_index = fail_on_call_index
        self.calls: list[tuple[str, str, str, str]] = []
        self.closed = False

    async def get_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        interval: str = "1d",
    ) -> list[Bar]:
        self.calls.append((symbol, start, end, interval))
        if self.fail_on_call_index is not None and len(self.calls) == self.fail_on_call_index:
            raise RuntimeError("refresh fetch down")
        if self.failure == (symbol, interval):
            raise RuntimeError("rate limited")
        if self.empty_result == (symbol, interval):
            return []
        if interval == "5m" and self.intraday_date_only == (symbol, interval):
            return [_bar(symbol, timestamp=start)]
        timestamp = self.timestamp_override.get((symbol, interval))
        if timestamp is None:
            return [
                _bar(symbol, timestamp=item)
                for item in _weekly_timestamps(start, end, interval=interval)
            ]
        if isinstance(timestamp, list):
            return [_bar(symbol, timestamp=item) for item in timestamp]
        return [_bar(symbol, timestamp=timestamp)]

    async def aclose(self) -> None:
        self.closed = True

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        return _weekly_timestamps(start, end, interval="1d")


class CoverageSegmentTests(unittest.TestCase):
    def test_split_into_coverage_segments_splits_long_suspension_gap(self) -> None:
        days = [
            date(2017, 6, 7),
            date(2017, 6, 14),
            date(2017, 6, 21),
            date(2017, 9, 28),
            date(2017, 10, 5),
        ]
        segments = _split_into_coverage_segments(days)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0][-1], date(2017, 6, 21))
        self.assertEqual(segments[1][0], date(2017, 9, 28))


class MarketDataSyncServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_builds_all_a_share_interval_jobs_and_records_failures(self) -> None:
        catalog = FakeCatalogRepository(
            [
                {"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True},
                {"symbol": "000001.SZ", "instrument_type": "stock", "is_tradable": True},
                {"symbol": "430001.BJ", "instrument_type": "stock", "is_tradable": True},
                {"symbol": "110000.SH", "instrument_type": "bond", "is_tradable": True},
                {"symbol": "HK0001.HK", "instrument_type": "stock", "is_tradable": True},
                {"symbol": "300001.SZ", "instrument_type": "stock", "is_tradable": False},
            ]
        )
        repo = FakeMarketRepository()
        provider = FakeProvider(failure=("000001.SZ", "5m"))
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=catalog,
            provider_factory=lambda: provider,
            intervals=("1d", "5m"),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=2,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 6, "succeeded": 5, "failed": 1, "skipped": 0})
        self.assertEqual(len(repo.upserts), 5)
        self.assertEqual(len(repo.successes), 5)
        self.assertEqual(len(repo.failures), 1)
        self.assertEqual(repo.failures[0]["error_code"], "market_data_sync_fetch_failed")
        self.assertEqual(repo.failures[0]["error_type"], "RuntimeError")
        self.assertEqual(repo.failures[0]["error_message"], "rate limited")
        self.assertEqual(
            sorted(provider.calls),
            [
                ("000001.SZ", "2016-06-01", "2026-06-01", "1d"),
                ("000001.SZ", "2016-06-01", "2026-06-01", "5m"),
                ("430001.BJ", "2016-06-01", "2026-06-01", "1d"),
                ("430001.BJ", "2016-06-01", "2026-06-01", "5m"),
                ("600000.SH", "2016-06-01", "2026-06-01", "1d"),
                ("600000.SH", "2016-06-01", "2026-06-01", "5m"),
            ],
        )
        five_minute_upsert = [
            call for call in repo.upserts if call["interval"] == "5m"
        ][0]
        five_minute_timestamps = {
            bar["timestamp"] for bar in five_minute_upsert["bars"]
        }
        self.assertIn("2026-06-01T15:00:00+00:00", five_minute_timestamps)
        event_names = [call.args[0] for call in emit.await_args_list]
        self.assertIn("market_data.sync.started", event_names)
        self.assertIn("market_data.sync.symbol_interval_completed", event_names)
        self.assertIn("market_data.sync.symbol_interval_failed", event_names)
        self.assertIn("market_data.sync.finished", event_names)
        failed_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.sync.symbol_interval_failed"
        ][0]
        self.assertEqual(failed_payload["error"], "rate limited")
        self.assertIn("hint", failed_payload)
        self.assertTrue(provider.closed)

    async def test_list_symbols_scoped_to_watchlist_by_default(self) -> None:
        # Watchlist repo present + sync_full_market default False ⇒ scope to
        # watchlisted symbols only (existing behaviour, unchanged).
        catalog = FakeCatalogRepository(
            [
                {"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True},
                {"symbol": "000001.SZ", "instrument_type": "stock", "is_tradable": True},
            ]
        )

        class _Watchlist:
            async def list_symbols(self) -> list[str]:
                return ["600000.SH"]

        service = MarketDataSyncService(
            market_repository=FakeMarketRepository(),
            instrument_catalog_repository=catalog,
            provider_factory=lambda: FakeProvider(),
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=2,
            rate_limit_per_second=1000,
            watchlist_repository=_Watchlist(),
            today_fn=lambda: date(2026, 6, 1),
        )
        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            symbols = await service._list_symbols()
        self.assertEqual([s for s, _ in symbols], ["600000.SH"])
        scope_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_sync_scoped_to_watchlist"
        ][0]
        self.assertEqual(scope_payload["reason"], "watchlist_scope")

    async def test_sync_full_market_overrides_watchlist_scope(self) -> None:
        # sync_full_market True ⇒ ignore the watchlist scope and sync the whole
        # A-share catalog so the local warehouse can serve full-market screens.
        catalog = FakeCatalogRepository(
            [
                {"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True},
                {"symbol": "000001.SZ", "instrument_type": "stock", "is_tradable": True},
                {"symbol": "110000.SH", "instrument_type": "bond", "is_tradable": True},
                # ETF is tradable and must enter the K-line sync scope.
                {"symbol": "510300.SH", "instrument_type": "etf", "is_tradable": True},
                # An index is non-tradable and stays excluded.
                {"symbol": "000300.SH", "instrument_type": "index", "is_tradable": False},
            ]
        )

        class _Watchlist:
            async def list_symbols(self) -> list[str]:
                return ["600000.SH"]

        service = MarketDataSyncService(
            market_repository=FakeMarketRepository(),
            instrument_catalog_repository=catalog,
            provider_factory=lambda: FakeProvider(),
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=2,
            rate_limit_per_second=1000,
            watchlist_repository=_Watchlist(),
            sync_full_market=True,
            today_fn=lambda: date(2026, 6, 1),
        )
        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            symbols = await service._list_symbols()
        # Full A-share catalog: stocks + ETF are synced; the bond and the
        # non-tradable index are filtered by _is_syncable_a_share. Not the
        # watchlist subset.
        self.assertEqual(
            sorted(s for s, _ in symbols),
            ["000001.SZ", "510300.SH", "600000.SH"],
        )
        scope_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_sync_scoped_to_watchlist"
        ][0]
        self.assertEqual(scope_payload["reason"], "sync_full_market_override")
        self.assertTrue(scope_payload["sync_full_market"])

    async def test_sync_skips_when_state_already_covers_target_window(self) -> None:
        provider = FakeProvider()
        repo = FakeMarketRepository(
            {
                (
                    "600000.SH",
                    "1d",
                ): {
                    "status": "ok",
                    "covered_start": datetime(2016, 6, 1, tzinfo=timezone.utc),
                    "covered_end": datetime(
                        2026,
                        6,
                        1,
                        23,
                        59,
                        59,
                        999999,
                        timezone.utc,
                    ),
                }
            }
        )
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 0, "skipped": 1})
        self.assertEqual(provider.calls, [])
        self.assertEqual(repo.upserts, [])
        self.assertEqual(repo.successes, [])
        self.assertEqual(repo.failures, [])

    async def test_five_minute_sync_covers_whole_returned_trading_day(self) -> None:
        provider = FakeProvider()
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("5m",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual(
            repo.successes[0]["covered_end"],
            datetime(2026, 6, 1, 23, 59, 59, 999999, timezone.utc),
        )

    async def test_five_minute_close_only_response_is_failure(self) -> None:
        provider = FakeProvider(
            timestamp_override={
                ("600000.SH", "5m"): _weekly_close_only_timestamps(
                    "2016-06-01",
                    "2026-06-01",
                )
            }
        )
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("5m",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(repo.successes, [])
        self.assertEqual(
            repo.failures[0]["error_code"],
            "market_data_sync_insufficient_coverage",
        )

    async def test_sync_only_fetches_right_side_incremental_gap(self) -> None:
        provider = FakeProvider()
        repo = FakeMarketRepository(
            {
                (
                    "600000.SH",
                    "1d",
                ): {
                    "status": "ok",
                    "covered_start": datetime(2016, 6, 1, tzinfo=timezone.utc),
                    "covered_end": datetime(
                        2026,
                        5,
                        31,
                        23,
                        59,
                        59,
                        999999,
                        timezone.utc,
                    ),
                }
            }
        )
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        # Incremental window 2026-06-01..2026-06-01 widened backwards by the
        # 10-calendar-day anchor overlap for adjust-drift detection.
        self.assertEqual(provider.calls, [("600000.SH", "2026-05-22", "2026-06-01", "1d")])
        self.assertEqual(
            repo.successes[0]["covered_start"],
            datetime(2016, 6, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            repo.successes[0]["covered_end"],
            datetime(2026, 6, 1, 23, 59, 59, 999999, timezone.utc),
        )

    async def test_sync_uses_listing_date_as_target_start(self) -> None:
        provider = FakeProvider()
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [
                    {
                        "symbol": "688001.SH",
                        "instrument_type": "stock",
                        "is_tradable": True,
                        "raw": {"list_date": "20250102"},
                    }
                ]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual(provider.calls, [("688001.SH", "2025-01-02", "2026-06-01", "1d")])
        self.assertEqual(
            repo.successes[0]["target_start"],
            datetime(2025, 1, 2, tzinfo=timezone.utc),
        )

    async def test_sync_empty_result_is_failure_not_success(self) -> None:
        provider = FakeProvider(empty_result=("600000.SH", "1d"))
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(repo.upserts, [])
        self.assertEqual(repo.successes, [])
        self.assertEqual(repo.failures[0]["error_code"], "market_data_sync_empty_result")

    async def test_sync_partial_response_records_partial_coverage(self) -> None:
        provider = FakeProvider(
            timestamp_override={
                ("600000.SH", "1d"): _weekly_timestamps(
                    "2016-06-01",
                    "2026-05-31",
                    interval="1d",
                )
            }
        )
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual(
            repo.successes[0]["covered_end"],
            datetime(2026, 5, 31, 23, 59, 59, 999999, timezone.utc),
        )
        self.assertLess(repo.successes[0]["covered_end"], repo.successes[0]["target_end"])

    async def test_sync_accepts_leading_intraday_history_gap_from_upstream(self) -> None:
        provider = FakeProvider(
            last_used_provider="baostock",
            timestamp_override={
                ("000636.SZ", "5m"): _weekly_timestamps(
                    "2020-01-02",
                    "2026-06-01",
                    interval="5m",
                )
            },
        )
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "000636.SZ", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("5m",),
            lookback_years=10,
            provider="auto",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual(repo.failures, [])
        self.assertEqual(len(repo.successes), 1)
        self.assertEqual(
            repo.successes[0]["target_start"],
            datetime(2016, 6, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            repo.successes[0]["covered_start"],
            datetime(2020, 1, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(
            repo.successes[0]["covered_end"],
            datetime(2026, 6, 1, 23, 59, 59, 999999, timezone.utc),
        )
        leading_gap = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.sync.leading_gap_accepted"
        ]
        self.assertEqual(len(leading_gap), 1)
        self.assertEqual(leading_gap[0]["served_provider"], "baostock")
        self.assertEqual(leading_gap[0]["requested_start"], "2016-06-01")
        self.assertEqual(leading_gap[0]["returned_start"], "2020-01-02T00:00:00+00:00")
        completed = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.sync.symbol_interval_completed"
        ]
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0]["leading_gap_accepted"])

    async def test_ok_state_with_accepted_leading_gap_skips_full_refetch(self) -> None:
        provider = FakeProvider()
        repo = FakeMarketRepository(
            {
                ("000636.SZ", "5m"): {
                    "status": "ok",
                    "target_start": datetime(2016, 6, 1, tzinfo=timezone.utc),
                    "target_end": datetime(
                        2026,
                        6,
                        1,
                        23,
                        59,
                        59,
                        999999,
                        tzinfo=timezone.utc,
                    ),
                    "covered_start": datetime(2020, 1, 2, tzinfo=timezone.utc),
                    "covered_end": datetime(
                        2026,
                        6,
                        1,
                        23,
                        59,
                        59,
                        999999,
                        tzinfo=timezone.utc,
                    ),
                }
            }
        )
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "000636.SZ", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("5m",),
            lookback_years=10,
            provider="auto",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 0, "skipped": 1})
        self.assertEqual(provider.calls, [])
        self.assertEqual(repo.successes, [])
        self.assertEqual(repo.failures, [])

    async def test_sync_allows_missing_suspended_daily_bar(self) -> None:
        timestamps = _weekly_timestamps("2016-06-01", "2026-06-01", interval="1d")
        omitted = timestamps[1]
        timestamps = [item for item in timestamps if item != omitted]
        provider = FakeProvider(timestamp_override={("600000.SH", "1d"): timestamps})
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual(len(repo.successes), 1)

    async def test_sync_duplicate_only_response_is_failure(self) -> None:
        provider = FakeProvider(
            timestamp_override={("600000.SH", "1d"): "2016-05-31"}
        )
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(repo.successes, [])
        self.assertEqual(
            repo.failures[0]["error_code"],
            "market_data_sync_insufficient_coverage",
        )

    async def test_sync_allows_long_suspension_gap_with_dense_segments(self) -> None:
        timestamps = _daily_timestamps_with_gap(
            start="2016-06-01",
            gap_after="2017-06-21",
            gap_before="2017-09-22",
            end="2026-06-01",
        )
        provider = FakeProvider(timestamp_override={("000403.SZ", "1d"): timestamps})
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "000403.SZ", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="auto",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual(len(repo.upserts), 1)
        event_names = [call.args[0] for call in emit.await_args_list]
        self.assertIn("market_data.sync.suspension_segments", event_names)

    async def test_sync_first_and_last_only_response_is_failure(self) -> None:
        provider = FakeProvider(
            timestamp_override={
                ("600000.SH", "1d"): ["2016-06-01", "2026-06-01"]
            }
        )
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(repo.successes, [])
        self.assertEqual(
            repo.failures[0]["error_code"],
            "market_data_sync_insufficient_coverage",
        )

    async def test_sync_rejects_date_only_intraday_bar_timestamp(self) -> None:
        provider = FakeProvider(intraday_date_only=("600000.SH", "5m"))
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("5m",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(repo.upserts, [])
        self.assertEqual(len(repo.failures), 1)
        self.assertEqual(repo.failures[0]["error_type"], "ValueError")
        self.assertEqual(
            repo.failures[0]["error_code"],
            "market_data_sync_bar_timestamp_invalid",
        )
        self.assertIn("market_data_sync_bar_timestamp_invalid", repo.failures[0]["error_message"])
        failed_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.sync.symbol_interval_failed"
        ][0]
        self.assertEqual(failed_payload["error_code"], "market_data_sync_bar_timestamp_invalid")
        self.assertIn("market_data_sync_bar_timestamp_invalid", failed_payload["error"])

    async def test_sync_malformed_intraday_timestamp_has_timestamp_error_code(self) -> None:
        provider = FakeProvider(
            timestamp_override={
                ("600000.SH", "5m"): ["2016-06-01T15:00:00", "2026-99-01T09:35:00"]
            }
        )
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("5m",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(
            repo.failures[0]["error_code"],
            "market_data_sync_bar_timestamp_invalid",
        )

    async def test_sync_upsert_failure_has_distinct_error_code(self) -> None:
        provider = FakeProvider()
        repo = FakeMarketRepository(upsert_error=RuntimeError("write down"))
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(repo.successes, [])
        self.assertEqual(repo.failures[0]["error_code"], "market_data_sync_upsert_failed")
        failed_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.sync.symbol_interval_failed"
        ][0]
        self.assertEqual(failed_payload["error_code"], "market_data_sync_upsert_failed")
        self.assertEqual(failed_payload["error"], "write down")

    async def test_sync_state_read_failure_has_distinct_error_code(self) -> None:
        provider = FakeProvider()
        repo = FakeMarketRepository(state_error=RuntimeError("state read down"))
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(provider.calls, [])
        self.assertEqual(repo.upserts, [])
        self.assertEqual(repo.failures[0]["error_code"], "market_data_sync_state_read_failed")
        failed_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.sync.symbol_interval_failed"
        ][0]
        self.assertEqual(failed_payload["error_code"], "market_data_sync_state_read_failed")
        self.assertEqual(failed_payload["error"], "state read down")

    async def test_sync_state_success_write_failure_has_distinct_error_code(self) -> None:
        provider = FakeProvider()
        repo = FakeMarketRepository(success_error=RuntimeError("state write down"))
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(len(repo.upserts), 1)
        self.assertEqual(repo.successes, [])
        self.assertEqual(repo.failures[0]["error_code"], "market_data_sync_state_write_failed")
        failed_payload = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.sync.symbol_interval_failed"
        ][0]
        self.assertEqual(failed_payload["error_code"], "market_data_sync_state_write_failed")
        self.assertEqual(failed_payload["error"], "state write down")

    async def test_failure_state_write_failure_does_not_abort_run(self) -> None:
        provider = FakeProvider(failure=("600000.SH", "1d"))
        repo = FakeMarketRepository(failure_error=RuntimeError("state down"))
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        event_names = [call.args[0] for call in emit.await_args_list]
        self.assertIn("market_data.sync.symbol_interval_failed", event_names)
        self.assertIn("market_data.sync.failure_record_failed", event_names)
        self.assertIn("market_data.sync.finished", event_names)

    async def test_failure_event_is_emitted_before_failure_state_write(self) -> None:
        event_order: list[str] = []
        provider = FakeProvider(failure=("600000.SH", "1d"))
        repo = FakeMarketRepository(events=event_order)
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        async def _emit(name: str, payload: dict[str, Any]) -> None:
            if name == "market_data.sync.symbol_interval_failed":
                event_order.append("failed_event")

        with patch("doyoutrade.data.market_sync.emit_debug_event", _emit):
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(event_order[:2], ["failed_event", "mark_sync_failure"])

    async def test_sync_uses_configured_provider_key_for_bar_rows(self) -> None:
        provider = FakeProvider(last_used_provider="akshare")
        repo = FakeMarketRepository()
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="auto",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        await service.run_once()

        self.assertEqual(repo.upserts[0]["provider"], "auto")
        self.assertEqual(repo.successes[0]["provider"], "auto")

    async def test_failed_state_retries_existing_right_side_gap(self) -> None:
        provider = FakeProvider()
        repo = FakeMarketRepository(
            {
                (
                    "600000.SH",
                    "1d",
                ): {
                    "status": "failed",
                    "covered_start": datetime(2016, 6, 1, tzinfo=timezone.utc),
                    "covered_end": datetime(
                        2026,
                        5,
                        31,
                        23,
                        59,
                        59,
                        999999,
                        timezone.utc,
                    ),
                }
            }
        )
        service = MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        # Failed-state retry still widens the fetch window by the anchor overlap.
        self.assertEqual(provider.calls, [("600000.SH", "2026-05-22", "2026-06-01", "1d")])
        self.assertEqual(
            repo.successes[0]["covered_start"],
            datetime(2016, 6, 1, tzinfo=timezone.utc),
        )

    async def test_start_is_idempotent_and_close_cancels_task(self) -> None:
        service = MarketDataSyncService(
            market_repository=FakeMarketRepository(),
            instrument_catalog_repository=FakeCatalogRepository([]),
            provider_factory=FakeProvider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

        await service.start()
        task = service._task
        await service.start()
        self.assertIs(service._task, task)
        await service.aclose()
        self.assertTrue(task.cancelled())


def _incremental_state() -> dict[tuple[str, str], dict[str, Any]]:
    """Sync state covering 2016-06-01..2026-05-31 so 2026-06-01 is the tail gap."""
    return {
        ("600000.SH", "1d"): {
            "status": "ok",
            "covered_start": datetime(2016, 6, 1, tzinfo=timezone.utc),
            "covered_end": datetime(2026, 5, 31, 23, 59, 59, 999999, timezone.utc),
        }
    }


def _anchor_rows(close: float) -> list[dict[str, Any]]:
    """Locally stored anchor-day rows matching FakeProvider's weekly timestamps."""
    return [
        {"timestamp": "2026-05-22", "close": close},
        {"timestamp": "2026-05-29", "close": close},
    ]


class AdjustDriftSyncTests(unittest.IsolatedAsyncioTestCase):
    """Anchor-overlap adjust-factor drift detection in _sync_symbol_interval."""

    def _service(
        self,
        repo: FakeMarketRepository,
        provider: FakeProvider,
    ) -> MarketDataSyncService:
        return MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=FakeCatalogRepository(
                [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
            ),
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=10,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=1000,
            today_fn=lambda: date(2026, 6, 1),
        )

    @staticmethod
    def _payloads(emit: AsyncMock, name: str) -> list[dict[str, Any]]:
        return [call.args[1] for call in emit.await_args_list if call.args[0] == name]

    async def test_incremental_sync_widens_fetch_window_with_anchor_overlap(self) -> None:
        repo = FakeMarketRepository(_incremental_state(), anchor_bars=_anchor_rows(1.5))
        provider = FakeProvider()
        service = self._service(repo, provider)

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        # fetch_start 2026-06-01 widened back 10 calendar days to 2026-05-22.
        self.assertEqual(provider.calls, [("600000.SH", "2026-05-22", "2026-06-01", "1d")])
        self.assertEqual(len(repo.anchor_calls), 1)
        anchor_call = repo.anchor_calls[0]
        self.assertEqual(anchor_call["provider"], "qmt")
        self.assertEqual(anchor_call["adjust"], "qfq")
        self.assertEqual(anchor_call["symbol"], "600000.SH")
        self.assertEqual(anchor_call["interval"], "1d")
        self.assertEqual(anchor_call["start"], datetime(2026, 5, 22, tzinfo=timezone.utc))
        self.assertEqual(
            anchor_call["end"],
            datetime(2026, 5, 31, 23, 59, 59, 999999, timezone.utc),
        )
        completed = self._payloads(emit, "market_data.sync.symbol_interval_completed")[0]
        self.assertEqual(completed["fetch_start"], "2026-06-01")
        self.assertEqual(completed["anchor_start"], "2026-05-22")
        self.assertEqual(completed["requested_start"], "2026-05-22")

    async def test_clean_anchor_does_not_escalate(self) -> None:
        repo = FakeMarketRepository(_incremental_state(), anchor_bars=_anchor_rows(1.5))
        provider = FakeProvider()
        service = self._service(repo, provider)

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(repo.upserts), 1)
        self.assertEqual(len(repo.successes), 1)
        self.assertEqual(
            repo.successes[0]["covered_start"],
            datetime(2016, 6, 1, tzinfo=timezone.utc),
        )
        event_names = [call.args[0] for call in emit.await_args_list]
        self.assertNotIn("market_data.sync.adjust_drift_detected", event_names)
        self.assertNotIn("market_data.sync.adjust_drift_refreshed", event_names)
        self.assertNotIn("market_data.sync.adjust_anchor_unavailable", event_names)

    async def test_anchor_drift_escalates_to_full_range_refresh(self) -> None:
        # Cached anchor closes are 10x the freshly fetched ones — the qfq
        # factor changed, so the entire history must be re-pulled.
        repo = FakeMarketRepository(_incremental_state(), anchor_bars=_anchor_rows(15.0))
        provider = FakeProvider()
        service = self._service(repo, provider)

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual(
            provider.calls,
            [
                ("600000.SH", "2026-05-22", "2026-06-01", "1d"),
                ("600000.SH", "2016-06-01", "2026-06-01", "1d"),
            ],
        )
        # Only the full-range result is written; the incremental slice never
        # lands on its own.
        self.assertEqual(len(repo.upserts), 1)
        upserted_timestamps = [bar["timestamp"] for bar in repo.upserts[0]["bars"]]
        self.assertEqual(min(upserted_timestamps), "2016-06-01")
        self.assertEqual(max(upserted_timestamps), "2026-06-01")
        self.assertEqual(len(repo.successes), 1)
        self.assertEqual(
            repo.successes[0]["covered_start"],
            datetime(2016, 6, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(
            repo.successes[0]["covered_end"],
            datetime(2026, 6, 1, 23, 59, 59, 999999, timezone.utc),
        )

        detected = self._payloads(emit, "market_data.sync.adjust_drift_detected")
        self.assertEqual(len(detected), 1)
        self.assertTrue(detected[0]["drifted"])
        self.assertGreater(detected[0]["max_rel_deviation"], 0.005)
        self.assertTrue(detected[0]["samples"])
        self.assertIn("hint", detected[0])

        refreshed = self._payloads(emit, "market_data.sync.adjust_drift_refreshed")
        self.assertEqual(len(refreshed), 1)
        self.assertEqual(refreshed[0]["refreshed_start"], "2016-06-01")
        self.assertEqual(refreshed[0]["refreshed_end"], "2026-06-01")
        self.assertEqual(refreshed[0]["upserted_count"], len(upserted_timestamps))

    async def test_drift_refresh_fetch_failure_records_distinct_error_code(self) -> None:
        repo = FakeMarketRepository(_incremental_state(), anchor_bars=_anchor_rows(15.0))
        provider = FakeProvider(fail_on_call_index=2)
        service = self._service(repo, provider)

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(len(provider.calls), 2)
        # No silent fallback to the incremental-only write.
        self.assertEqual(repo.upserts, [])
        self.assertEqual(repo.successes, [])
        self.assertEqual(
            repo.failures[0]["error_code"],
            "market_data_sync_adjust_refresh_failed",
        )
        event_names = [call.args[0] for call in emit.await_args_list]
        self.assertIn("market_data.sync.adjust_drift_detected", event_names)
        self.assertNotIn("market_data.sync.adjust_drift_refreshed", event_names)
        failed = self._payloads(emit, "market_data.sync.symbol_interval_failed")[0]
        self.assertEqual(failed["error_code"], "market_data_sync_adjust_refresh_failed")
        self.assertEqual(failed["error"], "refresh fetch down")
        self.assertIn("hint", failed)

    async def test_drift_refresh_upsert_failure_records_distinct_error_code(self) -> None:
        repo = FakeMarketRepository(
            _incremental_state(),
            anchor_bars=_anchor_rows(15.0),
            upsert_error=RuntimeError("write down"),
        )
        provider = FakeProvider()
        service = self._service(repo, provider)

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 0, "failed": 1, "skipped": 0})
        self.assertEqual(repo.successes, [])
        self.assertEqual(
            repo.failures[0]["error_code"],
            "market_data_sync_adjust_refresh_failed",
        )
        failed = self._payloads(emit, "market_data.sync.symbol_interval_failed")[0]
        self.assertEqual(failed["error_code"], "market_data_sync_adjust_refresh_failed")
        self.assertEqual(failed["error"], "write down")

    async def test_anchor_expected_but_no_overlap_emits_unavailable_event(self) -> None:
        repo = FakeMarketRepository(_incremental_state(), anchor_bars=[])
        provider = FakeProvider()
        service = self._service(repo, provider)

        with patch("doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock) as emit:
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual(len(repo.upserts), 1)
        unavailable = self._payloads(emit, "market_data.sync.adjust_anchor_unavailable")
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(unavailable[0]["reason"], "anchor_overlap_empty")
        self.assertIn("hint", unavailable[0])
        event_names = [call.args[0] for call in emit.await_args_list]
        self.assertNotIn("market_data.sync.adjust_drift_detected", event_names)

    async def test_anchor_read_failure_is_visible_and_does_not_block_sync(self) -> None:
        repo = FakeMarketRepository(
            _incremental_state(),
            anchor_error=RuntimeError("anchor read down"),
        )
        provider = FakeProvider()
        service = self._service(repo, provider)

        with patch(
            "doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock
        ) as emit, self.assertLogs("doyoutrade.data.market_sync", level="ERROR"):
            result = await service.run_once()

        self.assertEqual(result, {"scheduled": 1, "succeeded": 1, "failed": 0, "skipped": 0})
        self.assertEqual(len(repo.upserts), 1)
        unavailable = self._payloads(emit, "market_data.sync.adjust_anchor_unavailable")
        self.assertEqual(len(unavailable), 1)
        self.assertEqual(unavailable[0]["reason"], "anchor_check_failed")
        self.assertEqual(unavailable[0]["error"], "anchor read down")
        self.assertIn("hint", unavailable[0])


def _cloud_profile(
    *,
    plan_name: str = "free",
    disable_download: bool = True,
    sync_lookback_years: int | None = 2,
    provider_rate_limit_per_second: float | None = 0.4,
) -> "CloudProfile":
    return CloudProfile(
        service="doyoutrade-cloud",
        protocol_version=1,
        plan=CloudPlan(plan_name=plan_name),
        quota=CloudQuota(daily_requests=2000, used_today=0, remaining_today=2000),
        capabilities=("rate_limit_headers",),
        recommendations=CloudRecommendations(
            disable_download=disable_download,
            sync_lookback_years=sync_lookback_years,
            provider_rate_limit_per_second=provider_rate_limit_per_second,
        ),
    )


class CloudPresetSyncTests(unittest.IsolatedAsyncioTestCase):
    """Cloud-mode recommendations clamp sync limits to min(configured, recommended)."""

    def _service(
        self,
        repo: FakeMarketRepository,
        provider: FakeProvider,
        *,
        lookback_years: int = 10,
        rate_limit_per_second: float = 1000.0,
        cloud_profile_provider=None,
    ) -> MarketDataSyncService:
        catalog = FakeCatalogRepository(
            [{"symbol": "600000.SH", "instrument_type": "stock", "is_tradable": True}]
        )
        return MarketDataSyncService(
            market_repository=repo,
            instrument_catalog_repository=catalog,
            provider_factory=lambda: provider,
            intervals=("1d",),
            lookback_years=lookback_years,
            provider="qmt",
            adjust="qfq",
            concurrency=1,
            rate_limit_per_second=rate_limit_per_second,
            today_fn=lambda: date(2026, 6, 1),
            cloud_profile_provider=cloud_profile_provider,
        )

    async def test_cloud_recommendations_clamp_lookback_and_rate(self) -> None:
        repo = FakeMarketRepository()
        provider = FakeProvider()

        async def _provider() -> "CloudProfile":
            return _cloud_profile(sync_lookback_years=2, provider_rate_limit_per_second=0.4)

        service = self._service(repo, provider, cloud_profile_provider=_provider)
        with patch(
            "doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            result = await service.run_once()

        self.assertEqual(result["succeeded"], 1)
        # Lookback clamped 10 -> 2 years: fetch starts at 2024-06-01, not 2016-06-01.
        self.assertEqual(provider.calls[0][1], "2024-06-01")
        # Outbound throttle clamped 1000 -> 0.4 req/s for this run.
        self.assertEqual(service._effective_rate_limit_per_second, 0.4)
        self.assertEqual(service._effective_lookback_years, 2)
        # Configured values stay untouched (clamp is per-run, not an override).
        self.assertEqual(service.lookback_years, 10)
        self.assertEqual(service.rate_limit_per_second, 1000.0)
        cloud_events = [
            call.args[1]
            for call in emit.await_args_list
            if call.args[0] == "market_data.sync.cloud_preset_applied"
        ]
        self.assertEqual(len(cloud_events), 1)
        self.assertEqual(cloud_events[0]["plan_name"], "free")
        self.assertEqual(cloud_events[0]["effective_lookback_years"], 2)
        self.assertEqual(cloud_events[0]["effective_rate_limit_per_second"], 0.4)
        self.assertIn("hint", cloud_events[0])

    async def test_cloud_recommendations_never_relax_user_config(self) -> None:
        # Recommended values are *larger* than the configured ones: the
        # conservative min() must keep the user's tighter configuration.
        repo = FakeMarketRepository()
        provider = FakeProvider()

        async def _provider() -> "CloudProfile":
            return _cloud_profile(
                sync_lookback_years=20, provider_rate_limit_per_second=5000.0
            )

        service = self._service(
            repo,
            provider,
            lookback_years=3,
            rate_limit_per_second=1000.0,
            cloud_profile_provider=_provider,
        )
        with patch(
            "doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock
        ):
            result = await service.run_once()

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(provider.calls[0][1], "2023-06-01")
        self.assertEqual(service._effective_lookback_years, 3)
        self.assertEqual(service._effective_rate_limit_per_second, 1000.0)

    async def test_cloud_probe_none_keeps_classic_behaviour(self) -> None:
        repo = FakeMarketRepository()
        provider = FakeProvider()

        async def _provider() -> None:
            return None

        service = self._service(repo, provider, cloud_profile_provider=_provider)
        with patch(
            "doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock
        ) as emit:
            result = await service.run_once()

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(provider.calls[0][1], "2016-06-01")
        self.assertEqual(service._effective_lookback_years, 10)
        self.assertEqual(service._effective_rate_limit_per_second, 1000.0)
        event_names = [call.args[0] for call in emit.await_args_list]
        self.assertNotIn("market_data.sync.cloud_preset_applied", event_names)

    async def test_cloud_probe_failure_keeps_classic_behaviour_and_is_logged(self) -> None:
        repo = FakeMarketRepository()
        provider = FakeProvider()

        async def _provider() -> None:
            raise RuntimeError("hello endpoint unreachable")

        service = self._service(repo, provider, cloud_profile_provider=_provider)
        with patch(
            "doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock
        ) as emit, self.assertLogs("doyoutrade.data.market_sync", level="WARNING") as logs:
            result = await service.run_once()

        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(provider.calls[0][1], "2016-06-01")
        self.assertEqual(service._effective_lookback_years, 10)
        self.assertEqual(service._effective_rate_limit_per_second, 1000.0)
        self.assertTrue(
            any("cloud profile probe failed" in line for line in logs.output)
        )
        event_names = [call.args[0] for call in emit.await_args_list]
        self.assertNotIn("market_data.sync.cloud_preset_applied", event_names)

    async def test_effective_values_reset_between_runs(self) -> None:
        # A cloud run followed by a classic run (account switched back) must
        # restore the configured values — no sticky clamps.
        repo = FakeMarketRepository()
        provider = FakeProvider()
        verdicts: list["CloudProfile | None"] = [_cloud_profile(), None]

        async def _provider() -> "CloudProfile | None":
            return verdicts.pop(0)

        service = self._service(repo, provider, cloud_profile_provider=_provider)
        with patch(
            "doyoutrade.data.market_sync.emit_debug_event", new_callable=AsyncMock
        ):
            await service.run_once()
            self.assertEqual(service._effective_lookback_years, 2)
            await service.run_once()
        self.assertEqual(service._effective_lookback_years, 10)
        self.assertEqual(service._effective_rate_limit_per_second, 1000.0)


if __name__ == "__main__":
    unittest.main()
