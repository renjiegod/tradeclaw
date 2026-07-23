from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any

from opentelemetry import trace

from doyoutrade.core.models import Bar
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.cache_policy import DataCachePolicy, MAX_INTERNAL_GAP_DAYS
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.continuity import ContinuityError, validate_continuity
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_BAOSTOCK
from doyoutrade.debug import emit_debug_event

logger = logging.getLogger(__name__)

# Single source of truth for local market bars intervals. The cache layer only
# distinguishes two *shapes*: daily (``1d``) and intraday (a set). Adding a new
# intraday interval means adding it to both ``SUPPORTED_LOCAL_INTERVALS`` and
# ``_INTRADAY_LOCAL_INTERVALS`` plus its bar step in ``_INTERVAL_STEP`` — keeping
# the three in lockstep avoids the "allowed but step/branch not updated" drift
# bug (§错误可见性: 字面量与属性脱钩必须收敛). config / workspace / service all
# import from here rather than re-hardcoding ``{"1d", "5m"}``.
_INTRADAY_LOCAL_INTERVALS = frozenset({"5m", "60m"})
SUPPORTED_LOCAL_INTERVALS = frozenset({"1d"}) | _INTRADAY_LOCAL_INTERVALS
_INTERVAL_STEP: dict[str, timedelta] = {
    "5m": timedelta(minutes=5),
    "60m": timedelta(minutes=60),
}


def is_intraday_interval(interval: str) -> bool:
    """True for intraday local intervals (``5m`` / ``60m``); False for ``1d``."""
    return interval in _INTRADAY_LOCAL_INTERVALS


def interval_step(interval: str) -> timedelta:
    """Bar-to-bar spacing for an intraday interval.

    Used by the coverage / fetch-segment builders to decide whether two adjacent
    bars are contiguous. Raising on a non-intraday interval keeps a daily
    interval from silently borrowing a wrong step (§错误可见性: schema 违反必须
    raise 带类型 + 值).
    """
    try:
        return _INTERVAL_STEP[interval]
    except KeyError as exc:
        raise ValueError(
            "interval_step is only defined for intraday intervals "
            f"{sorted(_INTRADAY_LOCAL_INTERVALS)}, got {interval!r}"
        ) from exc


class LocalHistoricalBarsDataProvider:
    """Local-first historical bars provider backed by market bars storage.

    Read/backfill behaviour and the write-time continuity guarantee are driven
    by a :class:`doyoutrade.data.cache_policy.DataCachePolicy` (per-task config).
    When no policy is supplied the defaults reproduce the legacy behaviour
    (local-first read, auto-backfill on miss) *plus* the continuity gate — which
    is always on: any path that would persist bars must first prove them
    continuous (no missing trading day except suspensions / holidays) or the
    whole write is rejected (§错误可见性 / user invariant). The check never
    persists partial data — it runs on the in-memory payload before any DB
    write, so a violation leaves both the ``market_bars`` and ``cached_bars``
    layers untouched.
    """

    def __init__(
        self,
        repository: Any,
        upstream: Any,
        *,
        provider: str,
        adjust: str = DEFAULT_BAR_ADJUST,
        policy: DataCachePolicy | None = None,
    ) -> None:
        self.repository = repository
        self.upstream = upstream
        self.provider = provider
        self.adjust = adjust
        self.policy = policy or DataCachePolicy()
        if hasattr(upstream, "capabilities"):
            self.capabilities = upstream.capabilities

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[Bar]:
        resolved_adjust = adjust or self.adjust
        payload = self._base_payload(
            symbol, interval, start_time, end_time, adjust=resolved_adjust
        )
        with data_span("market_data", "get_bars"):
            if interval not in SUPPORTED_LOCAL_INTERVALS:
                logger.warning(
                    "local market bars interval unsupported symbol=%s interval=%s "
                    "provider=%s error_type=ValueError error=%s",
                    symbol,
                    interval,
                    self.provider,
                    "market_data_interval_unsupported",
                )
                await self._emit_failed(
                    payload,
                    error_code="market_data_interval_unsupported",
                    error_type="ValueError",
                    error=f"unsupported interval: {interval}",
                    hint=(
                        "Use one of the supported local intervals: "
                        f"{', '.join(sorted(SUPPORTED_LOCAL_INTERVALS))}."
                    ),
                )
                raise ValueError(
                    "market_data_interval_unsupported: "
                    f"unsupported local market bars interval {interval!r}"
                )

            try:
                start_bound = _query_bound(start_time, interval=interval, is_end=False)
                end_bound = _query_bound(end_time, interval=interval, is_end=True)
            except ValueError as exc:
                logger.warning(
                    "local market bars query bound invalid symbol=%s interval=%s "
                    "provider=%s requested_start=%s requested_end=%s "
                    "error_type=%s error=%s",
                    symbol,
                    interval,
                    self.provider,
                    start_time,
                    end_time,
                    type(exc).__name__,
                    exc,
                )
                await self._emit_failed(
                    payload,
                    error_code="market_data_bound_invalid",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    hint="Use YYYY-MM-DD for daily bars; use YYYY-MM-DD or timezone-aware ISO bounds for intraday bars.",
                )
                raise

            # --- local-first read -----------------------------------------
            local_rows: list[Any] = []
            if self.policy.local_first:
                try:
                    with data_span("market_data", "local_lookup"):
                        local_rows = await self.repository.bars_in_range(
                            provider=self.provider,
                            adjust=resolved_adjust,
                            symbol=symbol,
                            interval=interval,
                            start=start_bound,
                            end=end_bound,
                        )
                except Exception as exc:
                    logger.warning(
                        "local market bars read failed symbol=%s interval=%s provider=%s "
                        "error_type=%s error=%s",
                        symbol,
                        interval,
                        self.provider,
                        type(exc).__name__,
                        exc,
                    )
                    await self._emit_failed(
                        payload,
                        error_code="market_data_local_read_failed",
                        error_type=type(exc).__name__,
                        error=str(exc),
                        hint="Check local market bars repository connectivity and schema.",
                    )
                    raise

                # Local hit serves directly (the local-first onion layer).
                if local_rows:
                    bars = [_row_to_bar(row) for row in local_rows]
                    await emit_debug_event(
                        "market_data.get_bars.hit",
                        {**payload, "returned_count": len(bars)},
                    )
                    return bars

            # --- backfill gate: honour auto_backfill --------------------------
            if not self.policy.auto_backfill:
                # Read-only local layer: a miss is NOT backfilled from upstream.
                bars = [_row_to_bar(row) for row in local_rows]
                await emit_debug_event(
                    "market_data.get_bars.miss",
                    {
                        **payload,
                        "returned_count": len(bars),
                        "missing_ranges": (
                            []
                            if bars
                            else [{"start": start_time, "end": end_time, "reason": "local_empty"}]
                        ),
                        "reason": "auto_backfill_disabled",
                        "hint": (
                            "data_cache.auto_backfill is false; a local miss is not "
                            "backfilled from upstream"
                        ),
                    },
                )
                return bars

            missing_ranges = [
                {
                    "start": start_time,
                    "end": end_time,
                    "reason": "local_empty",
                }
            ]
            await emit_debug_event(
                "market_data.get_bars.miss",
                {
                    **payload,
                    "missing_ranges": missing_ranges,
                    "returned_count": 0,
                },
            )

            # --- upstream fetch ------------------------------------------------
            try:
                with data_span("market_data", "upstream_gap_fetch"):
                    fetched = await self.upstream.get_bars(
                        symbol,
                        start_time,
                        end_time,
                        interval=interval,
                        adjust=resolved_adjust,
                    )
            except Exception as exc:
                logger.warning(
                    "local market bars upstream fetch failed symbol=%s interval=%s "
                    "provider=%s error_type=%s error=%s",
                    symbol,
                    interval,
                    self.provider,
                    type(exc).__name__,
                    exc,
                )
                await self._emit_failed(
                    payload,
                    error_code="market_data_upstream_fetch_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    hint="Check upstream market data provider availability and request parameters.",
                    missing_ranges=missing_ranges,
                )
                raise

            # Build the upsert payloads first. A malformed bar (e.g. a date-only
            # timestamp on a 5m bar) is a write-prep failure and keeps mapping to
            # ``market_data_upsert_failed`` — same contract as before the
            # continuity gate was added.
            try:
                bar_payloads = [_bar_dict(bar, interval=interval) for bar in fetched]
            except Exception as exc:
                logger.warning(
                    "local market bars payload build failed symbol=%s interval=%s "
                    "provider=%s returned_count=%s error_type=%s error=%s",
                    symbol,
                    interval,
                    self.provider,
                    len(fetched),
                    type(exc).__name__,
                    exc,
                )
                await self._emit_failed(
                    payload,
                    error_code="market_data_upsert_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    hint="Check local market bars write path and bar payload validation.",
                    missing_ranges=missing_ranges,
                    returned_count=len(fetched),
                )
                raise

            # --- continuity gate (user invariant): a discontinuous payload must
            # fail the whole write, never land partial/dirty rows. Runs on the
            # in-memory payload BEFORE any upsert, so a violation leaves both the
            # market_bars and cached_bars layers untouched (no cross-table
            # transaction needed). Empty fetch = nothing to persist, no dirty
            # data possible → skip the check (the no-op upsert below is harmless).
            if bar_payloads:
                await self._validate_continuity_before_write(
                    symbol=symbol,
                    start_time=start_time,
                    end_time=end_time,
                    interval=interval,
                    bar_payloads=bar_payloads,
                    payload=payload,
                )

            # --- persist -------------------------------------------------------
            try:
                with data_span("market_data", "upsert"):
                    upserted_count = await self.repository.upsert_bars(
                        provider=self.provider,
                        adjust=resolved_adjust,
                        interval=interval,
                        bars=bar_payloads,
                    )
            except Exception as exc:
                logger.warning(
                    "local market bars upsert failed symbol=%s interval=%s "
                    "provider=%s returned_count=%s error_type=%s error=%s",
                    symbol,
                    interval,
                    self.provider,
                    len(fetched),
                    type(exc).__name__,
                    exc,
                )
                await self._emit_failed(
                    payload,
                    error_code="market_data_upsert_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                    hint="Check local market bars write path and bar payload validation.",
                    missing_ranges=missing_ranges,
                    returned_count=len(fetched),
                )
                raise

            await emit_debug_event(
                "market_data.get_bars.gap_fetch",
                {
                    **payload,
                    "missing_ranges": missing_ranges,
                    "returned_count": len(fetched),
                    "upserted_count": upserted_count,
                },
            )
            return fetched

    async def _validate_continuity_before_write(
        self,
        *,
        symbol: str,
        start_time: str,
        end_time: str,
        interval: str,
        bar_payloads: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> None:
        """Reject a discontinuous payload before it is persisted.

        Resolves the continuity inputs from the *served* provider (the one that
        actually answered ``get_bars``), runs the pure
        :func:`doyoutrade.data.continuity.validate_continuity` judge, mirrors the
        verdict onto the active OTel span, emits the structured debug event, and
        ``raise``\\ s :class:`ContinuityError` when the policy says reject. A
        cross-source calendar (served ≠ calendar provider) or a non-authoritative
        source is downgraded to the internal-gap check instead of being trusted.
        """
        policy = self.policy
        upstream = self.upstream
        calendar_name = self._upstream_name()
        served = getattr(upstream, "last_used_provider", None) or calendar_name
        cap = getattr(upstream, "capabilities", None)
        authoritative_flag = bool(getattr(cap, "authoritative_calendar", False))
        same_source = served == calendar_name
        suspended = set(getattr(upstream, "last_suspended_days", None) or set())
        # Only baostock exposes a trustworthy per-date suspension signal today.
        suspension_source_available = served == PROVIDER_NAME_BAOSTOCK

        expected: set[str] | None = None
        authoritative = False
        degraded_reason: str | None = None
        # Continuity is always judged against the served provider's authoritative
        # calendar. A non-authoritative / cross-source / fetch-failed calendar
        # auto-degrades to the internal-gap check (handled below) — the
        # degradation is driven by the source's capabilities, not by config.
        if authoritative_flag and same_source:
            try:
                with data_span("market_data", "continuity_calendar"):
                    expected = set(
                        await upstream.get_trading_dates(start_time, end_time)
                    )
                authoritative = True
            except Exception as exc:
                degraded_reason = f"calendar_fetch_failed:{type(exc).__name__}"
                logger.warning(
                    "continuity calendar fetch failed symbol=%s interval=%s "
                    "served=%s error_type=%s error=%s — falling back to "
                    "internal-gap check",
                    symbol, interval, served, type(exc).__name__, exc,
                )
        elif authoritative_flag and not same_source:
            degraded_reason = "cross_source_calendar"
        else:
            degraded_reason = "non_authoritative_calendar"

        report = validate_continuity(
            bars=bar_payloads,
            interval=interval,
            expected_trading_days=expected,
            suspended_days=suspended,
            authoritative=authoritative,
            suspension_source_available=authoritative and suspension_source_available,
            max_internal_gap_days=MAX_INTERNAL_GAP_DAYS,
        )

        span = trace.get_current_span()
        span.set_attribute("market_data.continuity.mode", report.mode)
        span.set_attribute("market_data.continuity.classification", report.classification)
        span.set_attribute("market_data.continuity.authoritative", report.authoritative)
        span.set_attribute("market_data.continuity.served_provider", served)
        span.set_attribute("market_data.continuity.missing_days", len(report.missing_days))

        event_payload = {
            **payload,
            "served_provider": served,
            "calendar_provider": calendar_name,
            "on_unverifiable_gap": policy.on_unverifiable_gap,
            **report.as_payload(),
        }
        if degraded_reason:
            event_payload["calendar_degraded_reason"] = degraded_reason

        def _violation(rejected_by: str) -> ContinuityError:
            return ContinuityError(
                f"market_data_continuity_violation: {report.reason} for {symbol} "
                f"[{start_time}, {end_time}] interval={interval} served={served} "
                f"missing={report.missing_days[:10]}; {report.hint}",
                report=report,
            )

        # Hard violations reject regardless of policy.
        if report.is_hard_violation:
            await emit_debug_event(
                "market_data.get_bars.continuity_violation",
                {**event_payload, "rejected_by": "hard_violation"},
            )
            logger.warning(
                "continuity violation symbol=%s interval=%s served=%s class=%s "
                "missing=%d — rejecting write",
                symbol, interval, served, report.classification, len(report.missing_days),
            )
            raise _violation("hard_violation")

        # Authoritative gap that cannot be proven a suspension → policy decides.
        if report.classification == "calendar_unverifiable":
            if policy.on_unverifiable_gap == "fail":
                await emit_debug_event(
                    "market_data.get_bars.continuity_violation",
                    {**event_payload, "rejected_by": "on_unverifiable_gap=fail"},
                )
                logger.warning(
                    "continuity unverifiable gap symbol=%s interval=%s served=%s "
                    "missing=%d — rejecting write (on_unverifiable_gap=fail)",
                    symbol, interval, served, len(report.missing_days),
                )
                raise _violation("on_unverifiable_gap=fail")
            await emit_debug_event(
                "market_data.get_bars.continuity_degraded",
                {**event_payload, "degraded_by": "on_unverifiable_gap=degrade"},
            )
            logger.warning(
                "continuity unverifiable gap accepted under degrade policy "
                "symbol=%s interval=%s served=%s missing=%d",
                symbol, interval, served, len(report.missing_days),
            )
            return

        # Degraded-but-acceptable (non-authoritative / cross-source / calendar
        # fetch failed): persist, but make the degradation visible — the
        # authoritative calendar check could not run, only the internal-gap one.
        if report.classification == "degraded_ok":
            await emit_debug_event(
                "market_data.get_bars.continuity_degraded", event_payload
            )
            logger.warning(
                "continuity check degraded symbol=%s interval=%s served=%s "
                "reason=%s — only the internal-gap check ran",
                symbol, interval, served, degraded_reason,
            )
            return

        await emit_debug_event(
            "market_data.get_bars.continuity_passed", event_payload
        )

    def _upstream_name(self) -> str:
        cap = getattr(self.upstream, "capabilities", None)
        name = getattr(cap, "name", None) if cap is not None else None
        if isinstance(name, str) and name.strip():
            return name.strip()
        return self.provider

    async def get_market_context(self):
        return await self.upstream.get_market_context()

    async def is_trading_day(self, value: str) -> bool:
        return await self.upstream.is_trading_day(value)

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        return await self.upstream.get_trading_dates(start, end)

    async def aclose(self) -> None:
        close = getattr(self.upstream, "aclose", None)
        if close is not None:
            await close()

    def _base_payload(
        self,
        symbol: str,
        interval: str,
        requested_start: str,
        requested_end: str,
        *,
        adjust: str,
    ) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "interval": interval,
            "provider": self.provider,
            "adjust": adjust,
            "requested_start": requested_start,
            "requested_end": requested_end,
        }

    async def _emit_failed(
        self,
        payload: dict[str, Any],
        *,
        error_code: str,
        error_type: str,
        error: str,
        hint: str,
        **extra: Any,
    ) -> None:
        await emit_debug_event(
            "market_data.get_bars.failed",
            {
                **payload,
                **extra,
                "error_code": error_code,
                "error_type": error_type,
                "error": error,
                "hint": hint,
            },
        )


def _bar_dict(bar: Bar, *, interval: str) -> dict[str, Any]:
    return {
        "symbol": bar.symbol,
        "timestamp": _repository_timestamp(bar.timestamp, interval=interval),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "amount": bar.amount,
        "adjust_type": bar.adjust_type,
    }


def _row_to_bar(row: dict[str, Any]) -> Bar:
    return Bar(
        symbol=row["symbol"],
        timestamp=row["timestamp"],
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        amount=row.get("amount"),
        adjust_type=row.get("adjust_type", DEFAULT_BAR_ADJUST),
    )


def _query_bound(value: str, *, interval: str, is_end: bool) -> datetime:
    if interval == "1d":
        day = _source_date_part(value)
        bound_time = time.max if is_end else time.min
        return datetime.combine(datetime.fromisoformat(day).date(), bound_time, timezone.utc)
    if is_intraday_interval(interval):
        if _is_date_only(value):
            day = _source_date_part(value)
            bound_time = time.max if is_end else time.min
            return datetime.combine(
                datetime.fromisoformat(day).date(),
                bound_time,
                timezone.utc,
            )
        normalized = normalize_bar_timestamp(value)
        dt = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if dt.tzinfo is None:
            raise ValueError(
                "market_data_bound_invalid: timezone-aware ISO bounds are required for intraday bars"
            )
        normalized_dt = datetime.fromisoformat(normalized)
        return normalized_dt.replace(tzinfo=timezone.utc)
    raise ValueError(f"market_data_interval_unsupported: {interval!r}")


def _repository_timestamp(value: str, *, interval: str) -> str:
    if interval == "1d":
        return value
    normalized = normalize_bar_timestamp(value)
    if not normalized:
        return normalized
    if "T" not in normalized:
        raise ValueError(
            "market_data_bar_timestamp_invalid: intraday bars require timestamp with time"
        )
    return f"{normalized}+00:00"


def _is_date_only(value: str) -> bool:
    raw = str(value).strip()
    if len(raw) == 8 and raw.isdigit():
        return True
    return len(raw) == 10 and raw[4] == "-" and raw[7] == "-"


def _source_date_part(value: str) -> str:
    raw = str(value).strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw.replace("T", " ").split(" ", 1)[0]
