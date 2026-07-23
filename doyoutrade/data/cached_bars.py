from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable

from doyoutrade.data.adjust_drift import (
    ANCHOR_OVERLAP_CALENDAR_DAYS,
    AdjustDriftReport,
    detect_adjust_drift,
)
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.bars_cache_store import (
    BarsCacheStore,
    InMemoryBarsCacheStore,
    is_range_covered,
)
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.debug import emit_debug_event
from doyoutrade.core.models import Bar, MarketContext

logger = logging.getLogger(__name__)

_UNKNOWN_PROVIDER = "unknown"


def _resolve_provider_name(inner: Any) -> str:
    """Extract the provider id from ``inner.capabilities.name`` for cache keying.

    Returns ``"unknown"`` when the inner provider hasn't been migrated to
    declare :class:`doyoutrade.data.protocols.ProviderCapabilities`. The
    fallback string is deliberately not a real provider name so a typo on
    the inner provider can't accidentally make its bars look like a
    legitimate cache hit for a different source.
    """
    caps = getattr(inner, "capabilities", None)
    if caps is None:
        return _UNKNOWN_PROVIDER
    name = getattr(caps, "name", None)
    if not isinstance(name, str) or not name.strip():
        return _UNKNOWN_PROVIDER
    return name.strip()

BACKTEST_BARS_CACHE_EXPANSION_DAYS = 21
# Trading-day → calendar-day conversion factor for A-share (~5 trading days
# per 7 calendar days = 1.4) with a +20% buffer for public holidays.
_TRADING_TO_CALENDAR_DAYS_FACTOR = 1.7
# Safety pad on top of the converted warmup so the right-anchored
# ``history_fetcher.fetch(lookback=startup_history)`` still resolves the
# expected number of bars when the calendar window happens to straddle a
# multi-day exchange holiday block.
_WARMUP_CALENDAR_SAFETY_PAD_DAYS = 5


def _compute_warmup_left_expansion_days(startup_history: int | None) -> int:
    """Return the calendar-day left expansion for the backtest cache preload.

    When ``startup_history`` is ``None`` or non-positive, falls back to the
    base ``BACKTEST_BARS_CACHE_EXPANSION_DAYS`` (legacy 21-day window). When
    a positive value is provided, expands to
    ``max(21, ceil(startup_history * 1.7) + 5)`` calendar days so that on
    the first cycle of the user's reporting window the strategy's
    ``history_fetcher.fetch(lookback=startup_history, as_of=range_start)``
    call resolves the expected count of trading bars.
    """

    base = BACKTEST_BARS_CACHE_EXPANSION_DAYS
    if startup_history is None or startup_history <= 0:
        return base
    # ceil(startup_history * factor) — use int + bool trick to avoid
    # importing math just for one ceil.
    scaled = int(startup_history * _TRADING_TO_CALENDAR_DAYS_FACTOR)
    if scaled < startup_history * _TRADING_TO_CALENDAR_DAYS_FACTOR:
        scaled += 1
    candidate = scaled + _WARMUP_CALENDAR_SAFETY_PAD_DAYS
    return max(base, candidate)


def _normalized_day(value: str) -> str:
    return normalize_bar_timestamp(value)[:10]


def _accounted_coverage_ranges(
    trading_days: list[str],
    accounted_days: set[str],
    start: str,
    end: str,
) -> list[tuple[str, str]]:
    """Coverage ranges the fetched data can *actually* back.

    ``record_fetch`` historically recorded the whole requested ``[start, end]``
    as covered regardless of how many bars the upstream returned. A partial
    fetch — or an adjust-drift invalidation that only rebuilt a tail window —
    then left the store claiming full coverage over sparse bars: every later
    ``is_range_covered`` check hit the gap-ridden cache and never re-fetched,
    so a strategy's per-bar history read silently came back short and the
    uncovered early cycles emitted ``strategy_base_history_insufficient``
    (observed on 000636.SZ after its 2025-06-11 ex-rights rescale).

    This returns only the coverage that is genuinely justified:

    * No trading days in the window (pure weekend/holiday block) → the whole
      ``[start, end]`` is legitimately "covered empty" (keeps the empty-range
      optimisation described on ``CachedBarRangeRecord``).
    * Every trading day accounted for (has a bar or a recorded suspension) →
      the fetch is complete → record the full ``[start, end]`` (leading /
      trailing non-trading days stay covered too).
    * Otherwise the fetch is INCOMPLETE → record only the maximal contiguous
      runs of accounted trading days, so unaccounted gaps stay uncovered and
      the next read misses and re-fetches them. An all-gaps result (e.g. an
      empty partial fetch) records nothing.
    """
    ordered = sorted(
        {str(day).strip()[:10] for day in trading_days if str(day).strip()}
    )
    if not ordered:
        return [(start, end)]
    if all(day in accounted_days for day in ordered):
        return [(start, end)]
    ranges: list[tuple[str, str]] = []
    run_start: str | None = None
    run_end: str | None = None
    for day in ordered:
        if day in accounted_days:
            if run_start is None:
                run_start = day
            run_end = day
        elif run_start is not None:
            ranges.append((run_start, run_end))  # type: ignore[arg-type]
            run_start = run_end = None
    if run_start is not None:
        ranges.append((run_start, run_end))  # type: ignore[arg-type]
    return ranges


def expanded_backtest_bar_range(
    range_start: date,
    range_end: date,
    *,
    startup_history: int | None = None,
) -> tuple[str, str]:
    """Compute the preload window for a backtest's bar cache.

    Left side is expanded by
    ``_compute_warmup_left_expansion_days(startup_history)`` calendar days
    so the strategy's first-day ``history_fetcher.fetch(lookback=...)``
    call has enough bars in the cache. Right side keeps the legacy
    ``BACKTEST_BARS_CACHE_EXPANSION_DAYS`` window since the runner never
    looks beyond ``range_end``.

    ``startup_history`` is the per-strategy
    :attr:`doyoutrade.strategy_sdk.Strategy.startup_history` (the number of
    trading-day bars the strategy needs before it can produce a signal).
    When unset / non-positive the legacy 21-day fallback applies.
    """

    left_days = _compute_warmup_left_expansion_days(startup_history)
    start = range_start - timedelta(days=left_days)
    end = range_end + timedelta(days=BACKTEST_BARS_CACHE_EXPANSION_DAYS)
    return start.isoformat(), end.isoformat()


async def build_backtest_cached_data_provider(
    inner: Any,
    *,
    run_id: str,
    symbols: list[str],
    range_start: date,
    range_end: date,
    interval: str = "1d",
    startup_history: int | None = None,
    store: BarsCacheStore | None = None,
) -> "CachedBarsDataProvider":
    """Build the backtest-scoped cached data provider and preload bars.

    ``startup_history`` flows through to
    :func:`expanded_backtest_bar_range` so the preload window covers the
    strategy's warmup requirement. When a caller cannot resolve the value
    (e.g. the strategy runtime is not configured) it MUST pass ``None``
    and a warning should be logged at the call site — this function does
    not silently fall back to a magic default that hides the situation.

    A ``backtest_cache_preload_with_warmup`` debug event records the
    breakdown (``base_expansion_days`` / ``computed_left_days`` /
    ``startup_history``) so an operator can see exactly which formula
    decided the preload boundary.
    """

    provider = CachedBarsDataProvider(
        inner, scope="backtest", run_id=run_id, store=store
    )
    preload_start, preload_end = expanded_backtest_bar_range(
        range_start, range_end, startup_history=startup_history
    )
    computed_left_days = _compute_warmup_left_expansion_days(startup_history)
    await emit_debug_event(
        "backtest_cache_preload_with_warmup",
        {
            "run_id": run_id,
            "scope": "backtest",
            "interval": interval,
            "range_start": range_start.isoformat(),
            "range_end": range_end.isoformat(),
            "preload_start": preload_start,
            "preload_end": preload_end,
            "startup_history": startup_history,
            "base_expansion_days": BACKTEST_BARS_CACHE_EXPANSION_DAYS,
            "computed_left_days": computed_left_days,
            "right_expansion_days": BACKTEST_BARS_CACHE_EXPANSION_DAYS,
            "warmup_applied": computed_left_days > BACKTEST_BARS_CACHE_EXPANSION_DAYS,
        },
    )
    await provider.preload_bars(symbols, preload_start, preload_end, interval=interval)
    return provider


def install_cached_data_provider(
    worker: Any,
    provider: "CachedBarsDataProvider",
    *,
    previous: Any | None = None,
) -> None:
    old_provider = previous if previous is not None else getattr(worker, "data_provider", None)
    worker.data_provider = provider
    strategy = getattr(worker, "strategy", None)
    if strategy is not None:
        signal_target = getattr(strategy, "signal_component", None) or strategy
        if getattr(signal_target, "data_provider", None) is old_provider:
            signal_target.data_provider = provider
    # The live signal generator (worker.signal_generator, e.g.
    # InstanceSignalGenerator) holds its OWN data_provider reference and
    # rebuilds the per-bar BarsHistoryFetcher from it on every
    # generate_intents() call. If it keeps pointing at the raw provider, every
    # bar's base/informative history fetch bypasses this cache and re-hits the
    # live source (a QMT HTTP round-trip per bar — the dominant backtest cost).
    # Rebind it so per-bar reads resolve from the preloaded cache. A mismatch
    # is logged (not silently skipped) so the bypass stays observable.
    signal_generator = getattr(worker, "signal_generator", None)
    if signal_generator is not None and hasattr(signal_generator, "data_provider"):
        if getattr(signal_generator, "data_provider", None) is old_provider:
            signal_generator.data_provider = provider
        else:
            logger.warning(
                "install_cached_data_provider: signal_generator.data_provider "
                "did not match the expected previous provider (got %s); per-bar "
                "history fetches may bypass the cache and re-hit the live source",
                type(getattr(signal_generator, "data_provider", None)).__name__,
            )
    universe = getattr(worker, "universe_provider", None)
    if universe is not None and getattr(universe, "data_provider", None) is old_provider:
        universe.data_provider = provider


class CachedBarsDataProvider:
    """TradingDataProvider wrapper that caches historical bars by (provider, symbol, interval, adjust).

    The ``provider`` portion of the cache key comes from ``inner.capabilities.name``
    (see :func:`_resolve_provider_name`). This keeps a tushare-hfq fetch
    from accidentally serving an akshare-qfq query on the same symbol —
    the previous in-memory cache keyed by ``(symbol, interval)`` only,
    which masked cross-source contamination in the legacy code.

    The ``adjust`` portion of the cache key ensures that different 复权 modes
    (none/qfq/hfq) are cached separately, avoiding 复权断崖 misleading technical
    indicators like SMA crossovers.

    The actual storage backend is plug-in via ``store``:

    * :class:`doyoutrade.data.bars_cache_store.InMemoryBarsCacheStore` —
      legacy ``dict`` form, kept as the default so unit tests don't need
      a SQLAlchemy fixture.
    * :class:`doyoutrade.data.bars_cache_store.RepositoryBarsCacheStore` —
      DB-backed, wired in by ``bootstrap.py`` / ``platform/service.py``
      against the live runtime ``SqlAlchemyCachedBarsRepository``.
    """

    def __init__(
        self,
        inner: Any,
        *,
        scope: str = "backtest",
        run_id: str | None = None,
        today_fn: Callable[[], date] | None = None,
        store: BarsCacheStore | None = None,
    ) -> None:
        self._inner = inner
        self.scope = scope
        self.run_id = run_id
        self._today_fn = today_fn or date.today
        self._store: BarsCacheStore = store or InMemoryBarsCacheStore()
        # Cached because every ``get_bars`` call needs it for the store
        # lookup and ``inner.capabilities.name`` is immutable per provider.
        self._provider_name = _resolve_provider_name(inner)

    async def get_market_context(self) -> MarketContext:
        # In backtest the inner provider's get_market_context would hit a
        # realtime quote API (e.g. qmt_sdk.data.get_full_tick) for data that
        # is then 100% overwritten by merge_simulated_bar_marks_into_market
        # (overlay covers universe + held positions = inner's symbol set).
        # The live call is wasted bandwidth AND requires the live data source
        # to be online for an offline backtest, so skip it and return an
        # empty context — the worker's overlay populates it from cached
        # bars. The skip is announced via debug event per §错误可见性 so an
        # operator can see why the inner call did not run.
        if self.scope == "backtest":
            await emit_debug_event(
                "cached_bars_market_context_backtest_skip",
                {
                    "scope": self.scope,
                    "run_id": self.run_id,
                    "reason": "overlay_owns_quotes_in_backtest",
                    "hint": (
                        "merge_simulated_bar_marks_into_market populates "
                        "MarketContext from cached bars; calling inner.get_"
                        "market_context would hit a realtime API that is "
                        "overwritten anyway."
                    ),
                },
            )
            return MarketContext()
        return await self._inner.get_market_context()

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[Bar]:
        start = _normalized_day(start_time)
        end = _normalized_day(end_time)
        with data_span("cache", "get_bars"):
            today = self._today_fn().isoformat()
            if self._should_split_live_range(start, end, today):
                return await self._get_live_split_bars(
                    symbol=symbol,
                    start=start,
                    end=end,
                    interval=interval,
                    today=today,
                    adjust=adjust,
                )
            if self._should_bypass_cache(end):
                bars = list(await self._inner.get_bars(symbol, start_time, end_time, interval=interval, adjust=adjust))
                await self._emit_cache_event(
                    "bypass",
                    symbol=symbol,
                    interval=interval,
                    start=start,
                    end=end,
                    returned_count=len(bars),
                    adjust=adjust,
                )
                return bars

            return await self._get_cached_history_bars(
                symbol=symbol,
                start=start,
                end=end,
                interval=interval,
                adjust=adjust,
            )

    async def preload_bars(
        self,
        symbols: list[str],
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> None:
        with data_span("cache", "preload_bars"):
            for symbol in dict.fromkeys(s for s in symbols if s):
                # Anchor revalidation BEFORE the cache read: a pure cache
                # hit never touches upstream, so a 除权 event after the
                # last sync would silently feed the whole backtest stale
                # qfq prices. See doyoutrade.data.adjust_drift.
                revalidated = await self._revalidate_cached_adjust(symbol, interval, adjust)
                bars = await self.get_bars(symbol, start_time, end_time, interval=interval, adjust=adjust)
                await self._emit_cache_event(
                    "preload",
                    symbol=symbol,
                    interval=interval,
                    start=_normalized_day(start_time),
                    end=_normalized_day(end_time),
                    returned_count=len(bars),
                    adjust=adjust,
                    extra={"adjust_revalidated": revalidated},
                )

    async def suspended_days_in_range(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> set[str]:
        """Return the halted ``YYYY-MM-DD`` days cached for ``[start, end]``.

        Served straight from the store (the halts were persisted when the
        window was first fetched), so this never re-hits upstream — a backtest
        replay can consult it on a pure cache hit. Empty when nothing was
        recorded (provider doesn't track halts, or the window predates the
        suspension-capture feature).
        """
        start = _normalized_day(start_time)
        end = _normalized_day(end_time)
        return await self._store.suspended_days_in_range(
            provider=self._provider_name,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            adjust=adjust,
        )

    async def is_trading_day(self, value: str) -> bool:
        return await self._inner.is_trading_day(value)

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        return await self._inner.get_trading_dates(start, end)

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()

    def _should_bypass_cache(self, end: str) -> bool:
        if self.scope != "live":
            return False
        return end >= self._today_fn().isoformat()

    def _should_split_live_range(self, start: str, end: str, today: str) -> bool:
        if self.scope != "live":
            return False
        return start < today <= end

    async def _get_cached_history_bars(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
        interval: str,
        adjust: str,
    ) -> list[Bar]:
        provider = self._provider_name
        ranges = await self._store.covered_ranges(
            provider=provider, symbol=symbol, interval=interval, adjust=adjust
        )
        covered_range = (ranges[0][0], ranges[-1][1]) if ranges else None
        if is_range_covered(ranges, start, end):
            bars = await self._store.bars_in_range(
                provider=provider,
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
                adjust=adjust,
            )
            await self._emit_cache_event(
                "hit",
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
                returned_count=len(bars),
                covered_range=covered_range,
                adjust=adjust,
            )
            return bars

        fetched = list(await self._inner.get_bars(symbol, start, end, interval=interval, adjust=adjust))
        # Capture the halts the inner provider just reported for this window
        # (baostock drops tradestatus==0 days from ``fetched`` but records them
        # on ``last_suspended_days``). Persisting them next to the bars lets a
        # warm-cache replay tell a genuine halt apart from a missing-row gap —
        # the signal the backtest mark overlay needs. Providers that don't
        # track halts (e.g. QMT) expose no attribute → empty set, so their
        # missing days are treated as gaps, matching their full-coverage shape.
        suspended_days = {
            str(day or "").strip()[:10]
            for day in getattr(self._inner, "last_suspended_days", None) or ()
        }
        suspended_days = {
            day for day in suspended_days if day and start[:10] <= day <= end[:10]
        }
        if not fetched:
            logger.warning(
                "data cache miss: inner provider returned 0 bars for %s [%s, %s] interval=%s adjust=%s scope=%s",
                symbol, start, end, interval, adjust, self.scope,
            )
        await self._emit_cache_event(
            "miss",
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            returned_count=len(fetched),
            adjust=adjust,
        )
        # Adjust-factor drift guard: compare the freshly fetched bars
        # against whatever the store already holds on the same trading
        # days BEFORE persisting. A mismatch means an ex-rights/dividend
        # event rescaled the qfq history and the whole cached key is
        # stale (e.g. 000636.SZ 2025-06-11, ~130 → ~13 cliff).
        if fetched:
            cached_overlap = await self._store.bars_in_range(
                provider=provider,
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
                adjust=adjust,
            )
            if cached_overlap:
                report = detect_adjust_drift(cached_overlap, fetched)
                if report.drifted:
                    await self._invalidate_for_adjust_drift(
                        symbol=symbol,
                        interval=interval,
                        adjust=adjust,
                        start=start,
                        end=end,
                        report=report,
                        trigger="history_miss",
                    )
        # Coverage-integrity guard: only claim coverage the fetched bars can
        # actually back. Recording the full requested [start, end] when the
        # upstream returned a partial result is what let a poisoned cache
        # (e.g. 000636.SZ after an adjust-drift rebuild left ~5 months of
        # sparse bars) keep serving gaps forever — is_range_covered stayed
        # True so no read ever re-fetched the missing days. See
        # _accounted_coverage_ranges.
        try:
            trading_days = list(await self._inner.get_trading_dates(start, end))
        except Exception as exc:  # calendar unavailable → preserve legacy behaviour
            logger.warning(
                "coverage integrity: get_trading_dates failed for %s [%s, %s] "
                "interval=%s adjust=%s (%s: %s) — recording the full requested "
                "range as covered (legacy fallback)",
                symbol, start, end, interval, adjust, type(exc).__name__, exc,
            )
            await self._emit_cache_event(
                "coverage_calendar_unavailable",
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
                returned_count=len(fetched),
                adjust=adjust,
                extra={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "hint": (
                        "trading calendar unavailable; coverage recorded as the "
                        "full requested window and may over-claim if the fetch "
                        "was partial — check inner provider connectivity"
                    ),
                },
            )
            trusted_ranges: list[tuple[str, str]] = [(start, end)]
        else:
            bar_days = {
                normalize_bar_timestamp(bar.timestamp)[:10] for bar in fetched
            }
            bar_days.discard("")
            accounted_days = bar_days | {
                str(day or "").strip()[:10] for day in suspended_days
            }
            accounted_days.discard("")
            trusted_ranges = _accounted_coverage_ranges(
                trading_days, accounted_days, start, end
            )
            if trusted_ranges != [(start, end)]:
                expected = {
                    str(day).strip()[:10] for day in trading_days if str(day).strip()
                }
                missing = sorted(day for day in expected if day not in accounted_days)
                await self._emit_cache_event(
                    "coverage_incomplete",
                    symbol=symbol,
                    interval=interval,
                    start=start,
                    end=end,
                    returned_count=len(fetched),
                    adjust=adjust,
                    extra={
                        "expected_trading_days": len(expected),
                        "accounted_trading_days": len(expected & accounted_days),
                        "missing_trading_days": len(missing),
                        "first_missing": missing[0] if missing else None,
                        "last_missing": missing[-1] if missing else None,
                        "trusted_ranges": len(trusted_ranges),
                        "hint": (
                            "upstream returned fewer bars than the window's "
                            "trading days; only the covered sub-ranges are "
                            "recorded so the gaps re-fetch on next read instead "
                            "of the cache over-claiming coverage"
                        ),
                    },
                )

        for idx, (range_start, range_end) in enumerate(trusted_ranges):
            await self._store.record_fetch(
                provider=provider,
                symbol=symbol,
                interval=interval,
                start=range_start,
                end=range_end,
                bars=fetched if idx == 0 else [],
                adjust=adjust,
                suspended_days=suspended_days if idx == 0 else set(),
            )
        ranges_after = await self._store.covered_ranges(
            provider=provider, symbol=symbol, interval=interval, adjust=adjust
        )
        covered_range_after = (
            (ranges_after[0][0], ranges_after[-1][1]) if ranges_after else None
        )
        await self._emit_cache_event(
            "merge",
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            returned_count=len(fetched),
            covered_range=covered_range_after,
            adjust=adjust,
            extra={"suspended_count": len(suspended_days)} if suspended_days else None,
        )
        return await self._store.bars_in_range(
            provider=provider,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            adjust=adjust,
        )

    async def _get_live_split_bars(
        self,
        *,
        symbol: str,
        start: str,
        end: str,
        interval: str,
        today: str,
        adjust: str,
    ) -> list[Bar]:
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        await self._emit_cache_event(
            "split",
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            returned_count=0,
            adjust=adjust,
            extra={
                "history_start": start,
                "history_end": yesterday,
                # Realtime fetch deliberately starts at YESTERDAY, not
                # today: the extra day overlaps the cached history tail
                # and acts as the adjust-drift anchor — a fetch starting
                # today shares no trading day with the cache, so a 除权
                # rescale would never be detected on the live path.
                "realtime_start": yesterday,
                "realtime_end": end,
            },
        )

        history_bars: list[Bar] = []
        if start <= yesterday:
            history_bars = await self._get_cached_history_bars(
                symbol=symbol,
                start=start,
                end=yesterday,
                interval=interval,
                adjust=adjust,
            )

        realtime_bars: list[Bar] = []
        try:
            realtime_bars = list(await self._inner.get_bars(symbol, yesterday, end, interval=interval, adjust=adjust))
            await self._emit_cache_event(
                "bypass",
                symbol=symbol,
                interval=interval,
                start=yesterday,
                end=end,
                returned_count=len(realtime_bars),
                adjust=adjust,
            )
        except Exception as exc:
            logger.warning(
                "live split realtime fetch failed for %s [%s, %s] interval=%s adjust=%s "
                "provider=%s (%s: %s) — falling back to cached history only",
                symbol, yesterday, end, interval, adjust, self._provider_name,
                type(exc).__name__, exc,
            )
            await self._emit_cache_event(
                "split_realtime_error_fallback",
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
                returned_count=len(history_bars),
                adjust=adjust,
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
            return history_bars

        # Yesterday-anchor drift check: the realtime response includes
        # yesterday's bar which must agree with the cached history tail.
        # Disagreement means the qfq factor changed (除权/除息) and the
        # whole cached history for this key is stale.
        if history_bars and realtime_bars:
            report = detect_adjust_drift(history_bars, realtime_bars)
            if report.drifted:
                await self._invalidate_for_adjust_drift(
                    symbol=symbol,
                    interval=interval,
                    adjust=adjust,
                    start=start,
                    end=end,
                    report=report,
                    trigger="live_split_anchor",
                )
                # Cache is now empty for this key — this re-read misses
                # and refetches the history with the new adjust factor.
                history_bars = await self._get_cached_history_bars(
                    symbol=symbol,
                    start=start,
                    end=yesterday,
                    interval=interval,
                    adjust=adjust,
                )

        merged = self._merge_bar_lists(history_bars, realtime_bars)
        await self._emit_cache_event(
            "split_result",
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            returned_count=len(merged),
            adjust=adjust,
            extra={
                "history_count": len(history_bars),
                "realtime_count": len(realtime_bars),
            },
        )
        return merged

    async def _revalidate_cached_adjust(
        self, symbol: str, interval: str, adjust: str
    ) -> bool:
        """Anchor-revalidate the cached qfq history for one symbol.

        Compares the last ``ANCHOR_OVERLAP_CALENDAR_DAYS`` of cached bars
        against a fresh upstream fetch of the same window. Returns ``True``
        when an anchor comparison actually ran (clean OR drift-detected-
        and-invalidated), ``False`` when there was nothing to validate or
        the upstream anchor window was unavailable (conservative pass —
        announced via event + warning, never silent).
        """
        provider = self._provider_name
        ranges = await self._store.covered_ranges(
            provider=provider, symbol=symbol, interval=interval, adjust=adjust
        )
        if not ranges:
            return False
        anchor_end = _normalized_day(ranges[-1][1])
        anchor_start = (
            date.fromisoformat(anchor_end) - timedelta(days=ANCHOR_OVERLAP_CALENDAR_DAYS)
        ).isoformat()
        cached = await self._store.bars_in_range(
            provider=provider,
            symbol=symbol,
            interval=interval,
            start=anchor_start,
            end=anchor_end,
            adjust=adjust,
        )
        if not cached:
            return False
        try:
            fresh = list(
                await self._inner.get_bars(
                    symbol, anchor_start, anchor_end, interval=interval, adjust=adjust
                )
            )
        except Exception as exc:
            logger.warning(
                "adjust revalidation anchor fetch failed for %s interval=%s adjust=%s "
                "provider=%s window=[%s, %s] (%s: %s) — serving cached bars unverified",
                symbol, interval, adjust, provider, anchor_start, anchor_end,
                type(exc).__name__, exc,
            )
            await self._emit_cache_event(
                "adjust_revalidate_anchor_unavailable",
                symbol=symbol,
                interval=interval,
                start=anchor_start,
                end=anchor_end,
                returned_count=0,
                adjust=adjust,
                extra={
                    "reason": "anchor_fetch_error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "hint": (
                        "upstream anchor fetch failed; cached qfq bars are served "
                        "unverified and may hide an ex-rights rescale — check the "
                        "inner provider's connectivity"
                    ),
                },
            )
            return False
        if not fresh:
            logger.warning(
                "adjust revalidation anchor fetch returned 0 bars for %s interval=%s "
                "adjust=%s provider=%s window=[%s, %s] — serving cached bars unverified",
                symbol, interval, adjust, provider, anchor_start, anchor_end,
            )
            await self._emit_cache_event(
                "adjust_revalidate_anchor_unavailable",
                symbol=symbol,
                interval=interval,
                start=anchor_start,
                end=anchor_end,
                returned_count=0,
                adjust=adjust,
                extra={
                    "reason": "anchor_fetch_empty",
                    "hint": (
                        "upstream returned no bars for the anchor window; cached "
                        "qfq bars are served unverified and may hide an ex-rights "
                        "rescale — check the inner provider's data coverage"
                    ),
                },
            )
            return False
        report = detect_adjust_drift(cached, fresh)
        if report.overlap_count == 0:
            logger.warning(
                "adjust revalidation found no overlapping trading days for %s "
                "interval=%s adjust=%s provider=%s window=[%s, %s] — serving cached "
                "bars unverified",
                symbol, interval, adjust, provider, anchor_start, anchor_end,
            )
            await self._emit_cache_event(
                "adjust_revalidate_anchor_unavailable",
                symbol=symbol,
                interval=interval,
                start=anchor_start,
                end=anchor_end,
                returned_count=len(fresh),
                adjust=adjust,
                extra={
                    "reason": "anchor_no_overlap",
                    "hint": (
                        "cached and fresh anchor bars share no trading day; the "
                        "drift verdict cannot be rendered — check timestamp "
                        "normalization between the store and the inner provider"
                    ),
                },
            )
            return False
        if report.drifted:
            await self._invalidate_for_adjust_drift(
                symbol=symbol,
                interval=interval,
                adjust=adjust,
                start=anchor_start,
                end=anchor_end,
                report=report,
                trigger="preload_revalidate",
            )
        return True

    async def _invalidate_for_adjust_drift(
        self,
        *,
        symbol: str,
        interval: str,
        adjust: str,
        start: str,
        end: str,
        report: AdjustDriftReport,
        trigger: str,
    ) -> None:
        """Self-heal a detected adjust-factor drift: warn + event + invalidate.

        An invalidate failure is NOT swallowed — the stale bars would keep
        feeding wrong prices, so it is logged, announced via event, and
        re-raised (§错误可见性).
        """
        provider = self._provider_name
        logger.warning(
            "adjust-factor drift detected for %s interval=%s adjust=%s provider=%s "
            "max_rel_deviation=%.6f overlap_count=%d trigger=%s — invalidating cached bars",
            symbol, interval, adjust, provider,
            report.max_rel_deviation, report.overlap_count, trigger,
        )
        await self._emit_cache_event(
            "adjust_drift_invalidated",
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            returned_count=0,
            adjust=adjust,
            extra={
                **report.as_payload(),
                "trigger": trigger,
                "reason": "adjust_factor_changed",
                "hint": (
                    "ex-rights/dividend event rescaled qfq history; cached bars "
                    "for this symbol were dropped and will refetch fresh"
                ),
            },
        )
        try:
            removed = await self._store.invalidate(
                provider=provider, symbol=symbol, interval=interval, adjust=adjust
            )
        except Exception as exc:
            logger.exception(
                "cache invalidate failed for %s interval=%s adjust=%s provider=%s "
                "after adjust drift (%s: %s) — stale qfq bars remain in the store",
                symbol, interval, adjust, provider, type(exc).__name__, exc,
            )
            await self._emit_cache_event(
                "adjust_drift_invalidate_failed",
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
                returned_count=0,
                adjust=adjust,
                extra={
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "reason": "store_invalidate_error",
                    "hint": (
                        "stale qfq bars remain cached and will keep feeding wrong "
                        "prices; fix the cache store backend before re-running"
                    ),
                },
            )
            raise
        logger.info(
            "adjust drift self-heal: invalidated %d cached bars for %s interval=%s "
            "adjust=%s provider=%s trigger=%s",
            removed, symbol, interval, adjust, provider, trigger,
        )

    def _merge_bar_lists(self, history_bars: list[Bar], realtime_bars: list[Bar]) -> list[Bar]:
        bars_by_ts: dict[str, Bar] = {}
        for bar in history_bars:
            ts = normalize_bar_timestamp(bar.timestamp)
            if ts:
                bars_by_ts[ts] = bar
        for bar in realtime_bars:
            ts = normalize_bar_timestamp(bar.timestamp)
            if ts:
                bars_by_ts[ts] = bar
        return [bars_by_ts[ts] for ts in sorted(bars_by_ts)]

    async def _emit_cache_event(
        self,
        operation: str,
        *,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        returned_count: int,
        adjust: str = DEFAULT_BAR_ADJUST,
        covered_range: tuple[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "operation": operation,
            "scope": self.scope,
            "run_id": self.run_id or "",
            "provider": self._provider_name,
            "symbol": symbol,
            "interval": interval,
            "requested_start": start,
            "requested_end": end,
            "returned_count": returned_count,
            "adjust": adjust,
        }
        if covered_range is not None:
            payload["covered_start"] = covered_range[0]
            payload["covered_end"] = covered_range[1]
        if extra:
            payload.update(extra)
        await emit_debug_event("data_cache.get_bars", payload)
