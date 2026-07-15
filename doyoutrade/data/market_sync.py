from __future__ import annotations

import asyncio
import logging
import math
import time as monotonic_time
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Sequence

from opentelemetry import trace as trace_api

from doyoutrade.data.adjust_drift import (
    ANCHOR_OVERLAP_CALENDAR_DAYS,
    AdjustDriftReport,
    detect_adjust_drift,
)
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.debug import emit_debug_event

logger = logging.getLogger(__name__)

MAX_COVERAGE_GAP_DAYS = 90
MIN_INTRADAY_BARS_PER_DAY = 40


def _utc_day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, timezone.utc)


def _utc_day_end(day: date) -> datetime:
    return datetime.combine(day, time.max, timezone.utc)


def _subtract_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def _bar_payload(bar: Any, *, interval: str) -> dict[str, Any]:
    normalized = normalize_bar_timestamp(bar.timestamp)
    if interval != "1d":
        if not normalized or "T" not in normalized:
            raise ValueError(
                "market_data_sync_bar_timestamp_invalid: "
                "intraday bars require timestamp with time"
            )
        normalized = f"{normalized}+00:00"
    return {
        "symbol": bar.symbol,
        "timestamp": normalized,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "amount": getattr(bar, "amount", None),
        "adjust_type": getattr(bar, "adjust_type", DEFAULT_BAR_ADJUST),
    }


def _is_a_share_stock(row: dict[str, Any]) -> bool:
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol.endswith((".SH", ".SZ", ".BJ")):
        return False
    instrument_type = str(row.get("instrument_type") or "").strip().lower()
    if instrument_type and instrument_type not in {"stock", "a_share", "ashare"}:
        return False
    if row.get("is_tradable") is False:
        return False
    return True


def _listing_date(row: dict[str, Any]) -> date | None:
    raw = row.get("raw")
    raw_dict = raw if isinstance(raw, dict) else {}
    candidates = [
        row.get("listing_date"),
        row.get("listed_at"),
        row.get("ipo_date"),
        raw_dict.get("listing_date"),
        raw_dict.get("listed_at"),
        raw_dict.get("ipo_date"),
        raw_dict.get("list_date"),
        raw_dict.get("上市日期"),
    ]
    for candidate in candidates:
        normalized = normalize_bar_timestamp(candidate)
        if not normalized:
            continue
        try:
            return date.fromisoformat(normalized[:10])
        except ValueError:
            continue
    return None


def _coerce_sync_bound(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _state_has_accepted_leading_gap(state: dict[str, Any] | None) -> bool:
    if not state or state.get("status") != "ok":
        return False
    stored_target_start = _coerce_sync_bound(state.get("target_start"))
    covered_start = _coerce_sync_bound(state.get("covered_start"))
    if stored_target_start is None or covered_start is None:
        return False
    return covered_start > stored_target_start


class MarketDataSyncService:
    """Background all-A-share market bars sync into the local market repository."""

    def __init__(
        self,
        *,
        market_repository: Any,
        instrument_catalog_repository: Any,
        provider_factory: Callable[[], Any],
        intervals: Sequence[str],
        lookback_years: int,
        provider: str,
        adjust: str,
        concurrency: int,
        rate_limit_per_second: float,
        watchlist_repository: Any | None = None,
        sync_full_market: bool = False,
        today_fn: Callable[[], date] | None = None,
    ) -> None:
        if concurrency <= 0:
            raise ValueError("market data sync concurrency must be > 0")
        if lookback_years <= 0:
            raise ValueError("market data sync lookback_years must be > 0")
        if (
            isinstance(rate_limit_per_second, bool)
            or not math.isfinite(float(rate_limit_per_second))
            or float(rate_limit_per_second) <= 0
        ):
            raise ValueError("market data sync rate_limit_per_second must be finite and > 0")
        self.market_repository = market_repository
        self.instrument_catalog_repository = instrument_catalog_repository
        self.watchlist_repository = watchlist_repository
        self.sync_full_market = bool(sync_full_market)
        self.provider_factory = provider_factory
        self.intervals = tuple(intervals)
        self.lookback_years = lookback_years
        self.provider = provider
        self.adjust = adjust
        self.concurrency = concurrency
        self.rate_limit_per_second = float(rate_limit_per_second)
        self.today_fn = today_fn or date.today
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._rate_limit_lock = asyncio.Lock()
        self._last_fetch_at = 0.0

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run_forever(), name="market-data-sync")

    async def aclose(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            logger.info("market data sync service cancelled")

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                logger.exception(
                    "market data sync loop failed error_type=%s error=%s",
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                await emit_debug_event(
                    "market_data.sync.failed",
                    {
                        "error_code": "market_data_sync_loop_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "hint": "check instrument catalog and market data sync dependencies",
                    },
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3600)
            except asyncio.TimeoutError:
                continue

    async def run_once(self) -> dict[str, int]:
        symbols = await self._list_symbols()
        jobs = [
            (symbol, interval, listed_from)
            for symbol, listed_from in symbols
            for interval in self.intervals
        ]
        result = {"scheduled": len(jobs), "succeeded": 0, "failed": 0, "skipped": 0}
        await emit_debug_event(
            "market_data.sync.started",
            {
                "scheduled": len(jobs),
                "symbols": len(symbols),
                "intervals": list(self.intervals),
                "provider": self.provider,
                "adjust": self.adjust,
            },
        )
        provider = self.provider_factory()
        sem = asyncio.Semaphore(self.concurrency)
        lock = asyncio.Lock()

        async def _run_job(
            symbol: str,
            interval: str,
            listed_from: date | None,
        ) -> None:
            async with sem:
                status = await self._sync_symbol_interval(
                    provider,
                    symbol,
                    interval,
                    listed_from=listed_from,
                )
                async with lock:
                    result[status] += 1

        try:
            await asyncio.gather(
                *[
                    _run_job(symbol, interval, listed_from)
                    for symbol, interval, listed_from in jobs
                ]
            )
        finally:
            await self._close_provider(provider)
            await emit_debug_event("market_data.sync.finished", dict(result))
        return result

    async def _list_symbols(self) -> list[tuple[str, date | None]]:
        # Determine the sync scope. With a watchlist repository injected, the
        # local K-line library defaults to syncing only watchlisted symbols
        # (re-read every cycle, so freshly added symbols are picked up on the
        # next run). Without one we keep the historical full-catalog behaviour
        # for tests and special scenarios.
        # ``sync_full_market`` (market_data.sync_full_market) overrides the
        # watchlist scoping: the sync covers the whole A-share catalog so the
        # local warehouse can serve full-market ``stock screen`` reads. Opt-in —
        # default False keeps the watchlist-scoped sync load for existing deployments.
        wl_symbols: set[str] | None = None
        if self.watchlist_repository is not None and not self.sync_full_market:
            try:
                wl_symbols = {
                    str(symbol).strip()
                    for symbol in await self.watchlist_repository.list_symbols()
                    if str(symbol).strip()
                }
            except Exception as exc:
                # Watchlist read failed: do not silently swallow and do not
                # mistakenly fall back to the full catalog (would sync the
                # entire A-share market). Degrade conservatively to zero symbols
                # and make the failure visible.
                logger.exception(
                    "market data sync watchlist read failed error_type=%s error=%s; "
                    "scoping sync to 0 symbols",
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                await emit_debug_event(
                    "market_sync_scoped_to_watchlist",
                    {
                        "watchlisted": 0,
                        "selected": 0,
                        "reason": "watchlist_read_failed",
                        "error_code": "market_data_sync_watchlist_read_failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "hint": "check watchlist repository connectivity; sync degraded to 0 symbols",
                    },
                )
                return []

        symbols: list[tuple[str, date | None]] = []
        seen: set[str] = set()
        offset = 0
        limit = 1000
        while True:
            rows, total = await self.instrument_catalog_repository.list_page(
                q=None,
                limit=limit,
                offset=offset,
            )
            for row in rows:
                if not isinstance(row, dict) or not _is_a_share_stock(row):
                    continue
                symbol = str(row.get("symbol") or "").strip()
                if not symbol or symbol in seen:
                    continue
                if wl_symbols is not None and symbol not in wl_symbols:
                    continue
                seen.add(symbol)
                symbols.append((symbol, _listing_date(row)))
            offset += len(rows)
            if not rows or offset >= total:
                break

        if wl_symbols is not None:
            await emit_debug_event(
                "market_sync_scoped_to_watchlist",
                {
                    "watchlisted": len(wl_symbols),
                    "selected": len(symbols),
                    "reason": "watchlist_scope",
                    "hint": "add symbols via doyoutrade-cli watchlist add to widen K-line sync",
                },
            )
            logger.info(
                "market data sync scoped to watchlist watchlisted=%d selected=%d reason=watchlist_scope",
                len(wl_symbols),
                len(symbols),
            )
        else:
            reason = (
                "sync_full_market_override"
                if self.sync_full_market and self.watchlist_repository is not None
                else "unscoped_full_catalog"
            )
            await emit_debug_event(
                "market_sync_scoped_to_watchlist",
                {
                    "watchlisted": 0,
                    "selected": len(symbols),
                    "reason": reason,
                    "sync_full_market": self.sync_full_market,
                    "hint": (
                        "full A-share catalog sync (market_data.sync_full_market=true) "
                        "warms the local warehouse for full-market stock screen"
                        if reason == "sync_full_market_override"
                        else "add symbols via doyoutrade-cli watchlist add to widen K-line sync"
                    ),
                },
            )
            logger.info(
                "market data sync full catalog selected=%d reason=%s sync_full_market=%s",
                len(symbols),
                reason,
                self.sync_full_market,
            )
        return symbols

    async def _sync_symbol_interval(
        self,
        upstream: Any,
        symbol: str,
        interval: str,
        *,
        listed_from: date | None = None,
    ) -> str:
        today = self.today_fn()
        start_day = _subtract_years(today, self.lookback_years)
        if listed_from is not None and listed_from > start_day:
            start_day = listed_from
        target_start = _utc_day_start(start_day)
        target_end = _utc_day_end(today)
        payload = {
            "symbol": symbol,
            "interval": interval,
            "provider": self.provider,
            "adjust": self.adjust,
            "requested_start": start_day.isoformat(),
            "requested_end": today.isoformat(),
        }
        try:
            state = await self.market_repository.get_sync_state(
                provider=self.provider,
                adjust=self.adjust,
                symbol=symbol,
                interval=interval,
            )
        except Exception as exc:
            return await self._record_failure(
                payload,
                target_start=target_start,
                target_end=target_end,
                error_code="market_data_sync_state_read_failed",
                exc=exc,
                hint="check local market bars sync-state repository connectivity",
            )

        sync_window = _missing_sync_window(
            state,
            target_start=target_start,
            target_end=target_end,
        )
        if sync_window is None:
            await emit_debug_event("market_data.sync.symbol_interval_skipped", payload)
            return "skipped"
        fetch_start, fetch_end, covered_start = sync_window

        # Anchor-overlap widening: when syncing an incremental tail window on
        # top of existing local coverage, widen the upstream request backwards
        # into covered territory so the response contains anchor days whose
        # closes can be compared against the stored qfq history. 除权/除息
        # rescales the entire front-adjusted series, so stale local bars are
        # only detectable by re-fetching days we already hold
        # (see doyoutrade/data/adjust_drift.py).
        anchor_expected = state is not None and covered_start < _utc_day_start(fetch_start)
        request_start = fetch_start
        if anchor_expected:
            request_start = max(
                fetch_start - timedelta(days=ANCHOR_OVERLAP_CALENDAR_DAYS),
                covered_start.date(),
                target_start.date(),
            )
        payload = {
            **payload,
            "requested_start": request_start.isoformat(),
            "requested_end": fetch_end.isoformat(),
            "fetch_start": fetch_start.isoformat(),
            "anchor_start": request_start.isoformat(),
            "target_start": start_day.isoformat(),
            "target_end": today.isoformat(),
        }

        try:
            await self._throttle()
            with data_span("market_data_sync", "upstream_fetch"):
                bars = list(
                    await upstream.get_bars(
                        symbol,
                        request_start.isoformat(),
                        fetch_end.isoformat(),
                        interval=interval,
                    )
                )
        except Exception as exc:
            return await self._record_failure(
                payload,
                target_start=target_start,
                target_end=target_end,
                error_code="market_data_sync_fetch_failed",
                exc=exc,
                hint="check upstream provider availability or reduce sync concurrency/rate",
            )

        served_provider = _served_provider(upstream, fallback=self.provider)
        payload = {**payload, "served_provider": served_provider}
        if not bars:
            return await self._record_failure(
                payload,
                target_start=target_start,
                target_end=target_end,
                error_code="market_data_sync_empty_result",
                exc=ValueError("upstream returned no bars for requested sync window"),
                hint="check whether the symbol and interval have upstream historical data",
            )

        if anchor_expected:
            report = await self._evaluate_adjust_drift(
                symbol=symbol,
                interval=interval,
                anchor_start=request_start,
                fetch_start=fetch_start,
                fresh_bars=bars,
                payload=payload,
            )
            if report is not None and report.drifted:
                return await self._refresh_full_range_after_drift(
                    upstream,
                    symbol,
                    interval,
                    report=report,
                    payload=payload,
                    covered_start=covered_start,
                    target_start=target_start,
                    target_end=target_end,
                )

        status, _info = await self._validate_and_store(
            bars,
            symbol=symbol,
            interval=interval,
            payload=payload,
            request_start=request_start,
            target_start=target_start,
            target_end=target_end,
            covered_start=covered_start,
        )
        return status

    async def _evaluate_adjust_drift(
        self,
        *,
        symbol: str,
        interval: str,
        anchor_start: date,
        fetch_start: date,
        fresh_bars: Sequence[Any],
        payload: dict[str, Any],
    ) -> AdjustDriftReport | None:
        """Compare freshly fetched anchor bars against locally stored bars.

        Returns the drift report, or ``None`` when no verdict could be
        rendered (anchor read/compare failed, or zero overlapping trading
        days). Both no-verdict paths emit a
        ``market_data.sync.adjust_anchor_unavailable`` debug event so the
        skipped check is never silent.
        """
        anchor_end = fetch_start - timedelta(days=1)
        try:
            cached_bars = await self.market_repository.bars_in_range(
                provider=self.provider,
                adjust=self.adjust,
                symbol=symbol,
                interval=interval,
                start=_utc_day_start(anchor_start),
                end=_utc_day_end(anchor_end),
            )
            report = detect_adjust_drift(cached_bars, fresh_bars)
        except Exception as exc:
            logger.error(
                "market data sync adjust anchor check failed symbol=%s interval=%s "
                "provider=%s adjust=%s error_type=%s error=%s; "
                "drift was NOT checked this run",
                symbol,
                interval,
                self.provider,
                self.adjust,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            await emit_debug_event(
                "market_data.sync.adjust_anchor_unavailable",
                {
                    **payload,
                    "reason": "anchor_check_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "hint": (
                        "local anchor bars could not be read/compared; adjust-factor "
                        "drift was NOT checked this run — fix the local market bars "
                        "read path or the cached bar schema"
                    ),
                },
            )
            return None
        if report.overlap_count == 0:
            logger.info(
                "market data sync adjust anchor unavailable symbol=%s interval=%s "
                "provider=%s anchor_start=%s anchor_end=%s reason=anchor_overlap_empty",
                symbol,
                interval,
                self.provider,
                anchor_start.isoformat(),
                anchor_end.isoformat(),
            )
            await emit_debug_event(
                "market_data.sync.adjust_anchor_unavailable",
                {
                    **payload,
                    "reason": "anchor_overlap_empty",
                    "anchor_end": anchor_end.isoformat(),
                    "hint": (
                        "no overlapping trading days between locally stored bars and "
                        "the widened upstream window; adjust-factor drift was NOT "
                        "checked this run — verify local bars exist for the anchor window"
                    ),
                },
            )
            return None
        return report

    async def _refresh_full_range_after_drift(
        self,
        upstream: Any,
        symbol: str,
        interval: str,
        *,
        report: AdjustDriftReport,
        payload: dict[str, Any],
        covered_start: datetime,
        target_start: datetime,
        target_end: datetime,
    ) -> str:
        """Escalate a detected adjust-factor drift to a full-range refresh.

        The whole locally covered history is re-fetched with the new
        adjustment factor and upserted over the stale rows. Any failure here
        is recorded as ``market_data_sync_adjust_refresh_failed`` — never a
        silent fallback to the incremental-only write.
        """
        refresh_start = min(covered_start, target_start)
        refresh_start_day = refresh_start.date()
        refresh_end_day = target_end.date()
        logger.warning(
            "market data sync adjust drift detected symbol=%s interval=%s "
            "provider=%s adjust=%s max_rel_deviation=%.6f overlap_count=%d "
            "samples=%s; escalating to full-range refresh %s..%s",
            symbol,
            interval,
            self.provider,
            self.adjust,
            report.max_rel_deviation,
            report.overlap_count,
            [sample.as_payload() for sample in report.samples],
            refresh_start_day.isoformat(),
            refresh_end_day.isoformat(),
        )
        await emit_debug_event(
            "market_data.sync.adjust_drift_detected",
            {
                **payload,
                **report.as_payload(),
                "refresh_start": refresh_start_day.isoformat(),
                "refresh_end": refresh_end_day.isoformat(),
                "hint": (
                    "ex-rights/dividend event rescaled qfq history; escalating to "
                    "full-range refresh"
                ),
            },
        )
        refresh_payload = {
            **payload,
            "requested_start": refresh_start_day.isoformat(),
            "requested_end": refresh_end_day.isoformat(),
        }
        try:
            await self._throttle()
            with data_span("market_data_sync", "adjust_refresh_fetch"):
                bars = list(
                    await upstream.get_bars(
                        symbol,
                        refresh_start_day.isoformat(),
                        refresh_end_day.isoformat(),
                        interval=interval,
                    )
                )
        except Exception as exc:
            return await self._record_failure(
                refresh_payload,
                target_start=target_start,
                target_end=target_end,
                error_code="market_data_sync_adjust_refresh_failed",
                exc=exc,
                hint=(
                    "adjust-factor drift was detected but the full-range refresh "
                    "fetch failed; local qfq history stays stale until a refresh "
                    "succeeds"
                ),
            )
        if not bars:
            return await self._record_failure(
                refresh_payload,
                target_start=target_start,
                target_end=target_end,
                error_code="market_data_sync_adjust_refresh_failed",
                exc=ValueError(
                    "upstream returned no bars for adjust-drift full-range refresh"
                ),
                hint=(
                    "adjust-factor drift was detected but the full-range refresh "
                    "returned no bars; local qfq history stays stale until a refresh "
                    "succeeds"
                ),
            )
        status, info = await self._validate_and_store(
            bars,
            symbol=symbol,
            interval=interval,
            payload=refresh_payload,
            request_start=refresh_start_day,
            target_start=target_start,
            target_end=target_end,
            covered_start=refresh_start,
            error_code_override="market_data_sync_adjust_refresh_failed",
        )
        if status == "succeeded":
            await emit_debug_event(
                "market_data.sync.adjust_drift_refreshed",
                {
                    **refresh_payload,
                    **report.as_payload(),
                    "refreshed_start": refresh_start_day.isoformat(),
                    "refreshed_end": refresh_end_day.isoformat(),
                    "upserted_count": info.get("upserted_count"),
                },
            )
        return status

    async def _validate_and_store(
        self,
        bars: list[Any],
        *,
        symbol: str,
        interval: str,
        payload: dict[str, Any],
        request_start: date,
        target_start: datetime,
        target_end: datetime,
        covered_start: datetime,
        error_code_override: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Validate fetched bars, upsert them, and record sync success.

        Shared by the incremental sync path and the adjust-drift full-range
        refresh. ``error_code_override`` maps every failure of the refresh
        path onto ``market_data_sync_adjust_refresh_failed`` so the two paths
        stay distinguishable at the error_code level.
        """

        def _code(default: str) -> str:
            return error_code_override or default

        try:
            bar_payloads = [_bar_payload(bar, interval=interval) for bar in bars]
        except Exception as exc:
            error_code = (
                "market_data_sync_bar_timestamp_invalid"
                if "market_data_sync_bar_timestamp_invalid" in str(exc)
                else "market_data_sync_bar_payload_invalid"
            )
            return (
                await self._record_failure(
                    payload,
                    target_start=target_start,
                    target_end=target_end,
                    error_code=_code(error_code),
                    exc=exc,
                    hint="check upstream bar payload schema and timestamp granularity",
                ),
                {},
            )

        try:
            (
                returned_start,
                returned_end,
                covered_days,
                intraday_counts,
                suspension_segments,
            ) = _coverage_from_bars(
                bar_payloads,
                interval=interval,
            )
        except Exception as exc:
            error_code = (
                "market_data_sync_bar_timestamp_invalid"
                if "market_data_sync_bar_timestamp_invalid" in str(exc)
                else "market_data_sync_insufficient_coverage"
            )
            return (
                await self._record_failure(
                    payload,
                    target_start=target_start,
                    target_end=target_end,
                    error_code=_code(error_code),
                    exc=exc,
                    hint="check upstream response coverage and bar timestamps",
                ),
                {},
            )
        if interval != "1d":
            try:
                _validate_intraday_density(
                    covered_days=covered_days,
                    intraday_counts=intraday_counts,
                )
            except Exception as exc:
                return (
                    await self._record_failure(
                        payload,
                        target_start=target_start,
                        target_end=target_end,
                        error_code=_code("market_data_sync_insufficient_coverage"),
                        exc=exc,
                        hint="check upstream intraday bar density for the requested interval",
                    ),
                    {},
                )
        allowed_start_gap = _utc_day_start(request_start) + timedelta(
            days=MAX_COVERAGE_GAP_DAYS
        )
        leading_gap_accepted = False
        if returned_start > allowed_start_gap:
            if covered_start == target_start and returned_end >= _utc_day_start(request_start):
                leading_gap_accepted = True
                gap_days = (returned_start.date() - request_start).days
                span = trace_api.get_current_span()
                if span is not None:
                    span.set_attribute("market_data.sync.leading_gap_accepted", True)
                    span.set_attribute(
                        "market_data.sync.leading_gap_days",
                        gap_days,
                    )
                    span.set_attribute(
                        "market_data.sync.leading_gap_returned_start",
                        returned_start.isoformat(),
                    )
                logger.info(
                    "market data sync accepted leading history gap symbol=%s interval=%s "
                    "provider=%s requested_start=%s returned_start=%s gap_days=%s",
                    symbol,
                    interval,
                    self.provider,
                    request_start.isoformat(),
                    returned_start.isoformat(),
                    gap_days,
                )
                await emit_debug_event(
                    "market_data.sync.leading_gap_accepted",
                    {
                        **payload,
                        "served_provider": payload.get("served_provider", self.provider),
                        "requested_start": request_start.isoformat(),
                        "returned_start": returned_start.isoformat(),
                        "returned_end": returned_end.isoformat(),
                        "leading_gap_days": gap_days,
                        "hint": (
                            "upstream does not expose the earliest requested history; "
                            "accept the earliest available start and only backfill newer bars later"
                        ),
                    },
                )
            else:
                return (
                    await self._record_failure(
                        payload,
                        target_start=target_start,
                        target_end=target_end,
                        error_code=_code("market_data_sync_insufficient_coverage"),
                        exc=ValueError(
                            "upstream returned insufficient bars for requested sync window"
                        ),
                        hint="check upstream response coverage for the requested sync window",
                    ),
                    {},
                )
        if returned_end < _utc_day_start(request_start):
            return (
                await self._record_failure(
                    payload,
                    target_start=target_start,
                    target_end=target_end,
                    error_code=_code("market_data_sync_insufficient_coverage"),
                    exc=ValueError(
                        "upstream returned insufficient bars for requested sync window"
                    ),
                    hint="check upstream response coverage for the requested sync window",
                ),
                {},
            )

        try:
            with data_span("market_data_sync", "upsert"):
                upserted = await self.market_repository.upsert_bars(
                    provider=self.provider,
                    adjust=self.adjust,
                    interval=interval,
                    bars=bar_payloads,
                )
        except Exception as exc:
            return (
                await self._record_failure(
                    payload,
                    target_start=target_start,
                    target_end=target_end,
                    error_code=_code("market_data_sync_upsert_failed"),
                    exc=exc,
                    hint="check local market bars repository schema and write path",
                ),
                {},
            )

        try:
            effective_covered_start = (
                returned_start if leading_gap_accepted else min(covered_start, returned_start)
            )
            await self.market_repository.mark_sync_success(
                provider=self.provider,
                adjust=self.adjust,
                symbol=symbol,
                interval=interval,
                target_start=target_start,
                target_end=target_end,
                covered_start=effective_covered_start,
                covered_end=min(returned_end, target_end),
            )
        except Exception as exc:
            return (
                await self._record_failure(
                    payload,
                    target_start=target_start,
                    target_end=target_end,
                    error_code=_code("market_data_sync_state_write_failed"),
                    exc=exc,
                    hint="check local market bars sync-state repository write path",
                ),
                {},
            )

        completed_payload = {
            **payload,
            "returned_count": len(bars),
            "upserted_count": upserted,
        }
        if leading_gap_accepted:
            completed_payload["leading_gap_accepted"] = True
            completed_payload["returned_start"] = returned_start.isoformat()
            completed_payload["returned_end"] = returned_end.isoformat()
            completed_payload["leading_gap_days"] = (returned_start.date() - request_start).days
        if len(suspension_segments) > 1:
            completed_payload["suspension_segment_count"] = len(suspension_segments)
            completed_payload["suspension_gaps"] = [
                {
                    "from": left[-1].isoformat(),
                    "to": right[0].isoformat(),
                    "gap_days": (right[0] - left[-1]).days,
                }
                for left, right in zip(suspension_segments, suspension_segments[1:])
            ]
        await emit_debug_event(
            "market_data.sync.symbol_interval_completed",
            completed_payload,
        )
        if len(suspension_segments) > 1:
            await emit_debug_event(
                "market_data.sync.suspension_segments",
                {
                    **payload,
                    "segment_count": len(suspension_segments),
                    "segment_day_counts": [len(segment) for segment in suspension_segments],
                    "hint": (
                        "long suspension gaps were accepted; bars were upserted per "
                        "continuous trading segment"
                    ),
                },
            )
        return "succeeded", {
            "upserted_count": upserted,
            "returned_start": returned_start,
            "returned_end": returned_end,
        }

    async def _record_failure(
        self,
        payload: dict[str, Any],
        *,
        target_start: datetime,
        target_end: datetime,
        error_code: str,
        exc: Exception,
        hint: str,
    ) -> str:
        logger.exception(
            "market data sync failed symbol=%s interval=%s provider=%s "
            "error_code=%s error_type=%s error=%s",
            payload.get("symbol"),
            payload.get("interval"),
            self.provider,
            error_code,
            type(exc).__name__,
            exc,
            exc_info=exc.__traceback__ is not None,
        )
        failed_payload = {
            **payload,
            "error_code": error_code,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "hint": hint,
        }
        await emit_debug_event("market_data.sync.symbol_interval_failed", failed_payload)
        try:
            await self.market_repository.mark_sync_failure(
                provider=self.provider,
                adjust=self.adjust,
                symbol=str(payload.get("symbol") or ""),
                interval=str(payload.get("interval") or ""),
                target_start=target_start,
                target_end=target_end,
                error_code=error_code,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        except Exception as state_exc:
            logger.exception(
                "market data sync failure record failed symbol=%s interval=%s "
                "provider=%s error_type=%s error=%s",
                payload.get("symbol"),
                payload.get("interval"),
                self.provider,
                type(state_exc).__name__,
                state_exc,
                exc_info=True,
            )
            await emit_debug_event(
                "market_data.sync.failure_record_failed",
                {
                    **failed_payload,
                    "state_error_type": type(state_exc).__name__,
                    "state_error": str(state_exc),
                    "state_hint": "check sync-state repository write path",
                },
            )
        return "failed"

    async def _close_provider(self, provider: Any) -> None:
        close = getattr(provider, "aclose", None)
        if close is None:
            return
        try:
            await close()
        except Exception as exc:
            logger.exception(
                "market data sync provider close failed provider=%s "
                "error_type=%s error=%s",
                self.provider,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            await emit_debug_event(
                "market_data.sync.provider_close_failed",
                {
                    "provider": self.provider,
                    "error_code": "market_data_sync_provider_close_failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "hint": "check upstream provider resource cleanup",
                },
            )

    async def _throttle(self) -> None:
        min_interval = 1.0 / self.rate_limit_per_second
        async with self._rate_limit_lock:
            now = monotonic_time.monotonic()
            wait = self._last_fetch_at + min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_fetch_at = monotonic_time.monotonic()


def _state_covers_target(
    state: dict[str, Any] | None,
    *,
    target_start: datetime,
    target_end: datetime,
) -> bool:
    if not state or state.get("status") != "ok":
        return False
    covered_start = _coerce_sync_bound(state.get("covered_start"))
    covered_end = _coerce_sync_bound(state.get("covered_end"))
    if covered_start is None or covered_end is None:
        return False
    if _state_has_accepted_leading_gap(state):
        return covered_end >= target_end
    return covered_start <= target_start and covered_end >= target_end


def _missing_sync_window(
    state: dict[str, Any] | None,
    *,
    target_start: datetime,
    target_end: datetime,
) -> tuple[date, date, datetime] | None:
    if _state_covers_target(state, target_start=target_start, target_end=target_end):
        return None
    if not state:
        return target_start.date(), target_end.date(), target_start
    covered_start = _coerce_sync_bound(state.get("covered_start"))
    covered_end = _coerce_sync_bound(state.get("covered_end"))
    if covered_start is None or covered_end is None:
        return target_start.date(), target_end.date(), target_start
    if (_state_has_accepted_leading_gap(state) or covered_start <= target_start) and covered_end < target_end:
        covered_day = covered_end.astimezone(timezone.utc).date()
        start_day = covered_day
        if covered_end >= _utc_day_end(covered_day):
            start_day = covered_day + timedelta(days=1)
        if start_day > target_end.date():
            start_day = target_end.date()
        return start_day, target_end.date(), covered_start
    return target_start.date(), target_end.date(), target_start


def _served_provider(upstream: Any, *, fallback: str) -> str:
    last_used = getattr(upstream, "last_used_provider", None)
    if isinstance(last_used, str) and last_used.strip():
        return last_used.strip()
    caps = getattr(upstream, "capabilities", None)
    name = getattr(caps, "name", None) if caps is not None else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return fallback


def _split_into_coverage_segments(ordered_days: list[date]) -> list[list[date]]:
    if not ordered_days:
        return []
    segments: list[list[date]] = [[ordered_days[0]]]
    for previous, current in zip(ordered_days, ordered_days[1:]):
        if (current - previous).days > MAX_COVERAGE_GAP_DAYS:
            segments.append([current])
        else:
            segments[-1].append(current)
    return segments


def _validate_coverage_segments(segments: list[list[date]]) -> None:
    """Allow long suspension gaps between segments, but reject sparse boundary-only payloads."""
    if len(segments) <= 1:
        return
    for segment in segments:
        if len(segment) < 2:
            raise ValueError(
                "market_data_sync_insufficient_coverage: suspension segment has "
                f"only {len(segment)} trading day(s); expected dense data on each side "
                "of a long halt"
            )


def _coverage_from_bars(
    bars: list[dict[str, Any]],
    *,
    interval: str,
) -> tuple[datetime, datetime, set[date], dict[date, int], list[list[date]]]:
    covered_days: set[date] = set()
    intraday_counts: dict[date, int] = {}
    covered_start: datetime | None = None
    covered_end: datetime | None = None
    for bar in bars:
        timestamp = str(bar.get("timestamp") or "").strip()
        if not timestamp:
            continue
        if interval == "1d":
            normalized = normalize_bar_timestamp(timestamp)
            if not normalized:
                raise ValueError(
                    "market_data_sync_bar_timestamp_invalid: empty bar timestamp"
                )
            try:
                parsed = date.fromisoformat(normalized[:10])
            except ValueError as exc:
                raise ValueError(
                    "market_data_sync_bar_timestamp_invalid: invalid daily bar timestamp"
                ) from exc
            candidate_start = _utc_day_start(parsed)
            candidate_end = _utc_day_end(parsed)
            candidate_day = parsed
        else:
            raw = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
            try:
                parsed_dt = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError(
                    "market_data_sync_bar_timestamp_invalid: invalid intraday bar timestamp"
                ) from exc
            if parsed_dt.tzinfo is None:
                raise ValueError(
                    "market_data_sync_bar_timestamp_invalid: intraday bars require timezone"
            )
            candidate_day = parsed_dt.astimezone(timezone.utc).date()
            intraday_counts[candidate_day] = intraday_counts.get(candidate_day, 0) + 1
            candidate_start = _utc_day_start(candidate_day)
            parsed_utc = parsed_dt.astimezone(timezone.utc)
            close_time = time(15, 0)
            candidate_end = (
                _utc_day_end(candidate_day)
                if parsed_utc.time() >= close_time
                else parsed_utc
            )
        covered_days.add(candidate_day)
        covered_start = (
            candidate_start
            if covered_start is None
            else min(covered_start, candidate_start)
        )
        covered_end = candidate_end if covered_end is None else max(covered_end, candidate_end)
    if covered_start is None or covered_end is None:
        raise ValueError("market_data_sync_bar_timestamp_invalid: no valid bar timestamps")
    ordered_days = sorted(covered_days)
    segments = _split_into_coverage_segments(ordered_days)
    for segment in segments:
        _validate_no_large_coverage_gap(set(segment))
    _validate_coverage_segments(segments)
    return covered_start, covered_end, covered_days, intraday_counts, segments


def _validate_no_large_coverage_gap(covered_days: set[date]) -> None:
    ordered = sorted(covered_days)
    for previous, current in zip(ordered, ordered[1:]):
        if (current - previous).days > MAX_COVERAGE_GAP_DAYS:
            raise ValueError(
                "market_data_sync_insufficient_coverage: returned bars contain "
                f"coverage gap from {previous.isoformat()} to {current.isoformat()}"
            )


def _validate_intraday_density(
    *,
    covered_days: set[date],
    intraday_counts: dict[date, int],
) -> None:
    for day in sorted(covered_days):
        count = intraday_counts.get(day, 0)
        if count < MIN_INTRADAY_BARS_PER_DAY:
            raise ValueError(
                "market_data_sync_insufficient_coverage: intraday bars too sparse "
                f"for {day.isoformat()}: {count}"
            )
