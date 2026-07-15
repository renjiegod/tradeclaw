from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.coverage_ranges import consecutive_trading_day_ranges, merge_date_ranges
from doyoutrade.data.local_market_bars import _query_bound


@dataclass(frozen=True)
class CoverageSegment:
    start: str
    end: str
    status: str


@dataclass
class LocalMarketSyncJob:
    job_id: str
    status: str
    mode: str
    symbol: str
    interval: str
    provider: str
    adjust: str
    requested_start: str
    requested_end: str
    fetched_segments: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    upserted_count: int = 0
    adjust_drift_refreshed: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    error_code: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    hint: str | None = None


def choose_sync_execution_mode(*, interval: str, start: str, end: str) -> str:
    span_days = _calendar_day_span(start, end)
    if interval == "1d" and span_days <= 400:
        return "sync"
    if interval == "5m" and span_days <= 10:
        return "sync"
    return "async"


def build_sync_fetch_segments(
    *,
    interval: str,
    requested_start: str,
    requested_end: str,
    bars: list[dict[str, Any]],
    mode: str,
) -> list[CoverageSegment]:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == "force_refresh":
        return [CoverageSegment(start=requested_start, end=requested_end, status="requested")]
    if normalized_mode != "fill_gap":
        raise ValueError(f"unsupported local market sync mode: {mode!r}")
    if interval == "1d":
        return _build_daily_fetch_segments(
            requested_start=requested_start,
            requested_end=requested_end,
            bars=bars,
        )
    if interval == "5m":
        return _build_intraday_fetch_segments(
            requested_start=requested_start,
            requested_end=requested_end,
            bars=bars,
        )
    raise ValueError(f"unsupported interval for sync segments: {interval!r}")


def normalize_overlay_item(
    *,
    timestamp: str,
    kind: str,
    side: str | None,
    price: float | None,
    label: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "kind": kind,
        "side": side,
        "price": price,
        "label": label,
        "details": details,
    }


def empty_overlay_snapshot(overlay_kind: str, source: dict[str, Any]) -> dict[str, Any]:
    return {
        "overlay_kind": overlay_kind,
        "source": source,
        "items": [],
        "warnings": [],
    }


def build_local_market_summary(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {
            "bar_count": 0,
            "latest_close": None,
            "window_change": None,
            "window_change_pct": None,
            "window_high": None,
            "window_low": None,
            "amplitude_pct": None,
            "total_volume": 0.0,
            "total_amount": None,
        }

    first_open = _require_float(bars[0], "open", index=0)
    last_close = _require_float(bars[-1], "close", index=len(bars) - 1)
    window_low = min(_require_float(bar, "low", index=i) for i, bar in enumerate(bars))
    window_high = max(_require_float(bar, "high", index=i) for i, bar in enumerate(bars))
    total_volume = sum(_require_float(bar, "volume", index=i) for i, bar in enumerate(bars))

    amount_available = False
    total_amount = 0.0
    for index, bar in enumerate(bars):
        amount = bar.get("amount")
        if amount is None:
            continue
        amount_available = True
        total_amount += _coerce_float(amount, field_name="amount", index=index)

    window_change = last_close - first_open
    return {
        "bar_count": len(bars),
        "latest_close": last_close,
        "window_change": window_change,
        "window_change_pct": None if first_open == 0 else window_change / first_open,
        "window_high": window_high,
        "window_low": window_low,
        "amplitude_pct": None if window_low == 0 else (window_high - window_low) / window_low,
        "total_volume": total_volume,
        "total_amount": total_amount if amount_available else None,
    }


def build_requested_window_coverage(
    *,
    interval: str,
    requested_start: str,
    requested_end: str,
    bars: list[dict[str, Any]],
    sync_state: dict[str, Any] | None,
) -> dict[str, Any]:
    if interval == "1d":
        covered_segments, missing_segments = _build_daily_coverage(
            requested_start=requested_start,
            requested_end=requested_end,
            bars=bars,
            sync_state=sync_state,
        )
    elif interval == "5m":
        covered_segments, missing_segments = _build_intraday_coverage(
            requested_start=requested_start,
            requested_end=requested_end,
            bars=bars,
            sync_state=sync_state,
        )
    else:
        raise ValueError(f"unsupported interval for coverage: {interval!r}")
    return {
        "requested_start": requested_start,
        "requested_end": requested_end,
        "covered_segments": [segment.__dict__ for segment in covered_segments],
        "missing_segments": [segment.__dict__ for segment in missing_segments],
    }


def _require_float(row: dict[str, Any], field_name: str, *, index: int) -> float:
    return _coerce_float(row.get(field_name), field_name=field_name, index=index)


def _calendar_day_span(start: str, end: str) -> int:
    start_day = _coerce_dateish(start, field_name="start")
    end_day = _coerce_dateish(end, field_name="end")
    if end_day < start_day:
        raise ValueError(f"end must be on or after start, got {start!r}..{end!r}")
    return (end_day - start_day).days + 1


def _build_daily_coverage(
    *,
    requested_start: str,
    requested_end: str,
    bars: list[dict[str, Any]],
    sync_state: dict[str, Any] | None,
) -> tuple[list[CoverageSegment], list[CoverageSegment]]:
    start_day = _coerce_dateish(requested_start, field_name="requested_start")
    end_day = _coerce_dateish(requested_end, field_name="requested_end")
    _ensure_window_order(
        requested_start=requested_start,
        requested_end=requested_end,
        start_value=start_day,
        end_value=end_day,
    )

    ranges: list[tuple[date, date]] = []
    covered_days = sorted(
        {
            _coerce_dateish(bar.get("timestamp"), field_name="timestamp")
            for bar in bars
            if start_day <= _coerce_dateish(bar.get("timestamp"), field_name="timestamp") <= end_day
        }
    )
    ranges.extend(consecutive_trading_day_ranges(covered_days))
    sync_range = _daily_sync_overlap(
        sync_state,
        window_start=start_day,
        window_end=end_day,
    )
    if sync_range is not None:
        ranges.append(sync_range)
    merged = merge_date_ranges(ranges)
    covered = [
        CoverageSegment(start=_format_day(seg_start), end=_format_day(seg_end), status="covered")
        for seg_start, seg_end in merged
    ]
    # Without a trading calendar, daily missing segments would invent weekends/holidays.
    return covered, []


def _build_intraday_coverage(
    *,
    requested_start: str,
    requested_end: str,
    bars: list[dict[str, Any]],
    sync_state: dict[str, Any] | None,
) -> tuple[list[CoverageSegment], list[CoverageSegment]]:
    window_start = _coerce_intradayish(requested_start, field_name="requested_start", is_end=False)
    window_end = _coerce_intradayish(requested_end, field_name="requested_end", is_end=True)
    _ensure_window_order(
        requested_start=requested_start,
        requested_end=requested_end,
        start_value=window_start,
        end_value=window_end,
    )

    step = timedelta(minutes=5)
    ranges: list[tuple[datetime, datetime]] = []
    bar_points = sorted(
        {
            _coerce_intradayish(bar.get("timestamp"), field_name="timestamp", is_end=False)
            for bar in bars
            if window_start
            <= _coerce_intradayish(bar.get("timestamp"), field_name="timestamp", is_end=False)
            <= window_end
        }
    )
    ranges.extend((point, point) for point in bar_points)
    sync_range = _intraday_sync_overlap(
        sync_state,
        window_start=window_start,
        window_end=window_end,
    )
    if sync_range is not None:
        ranges.append(sync_range)
    merged = _merge_datetime_ranges(ranges, step=step)
    covered = [
        CoverageSegment(
            start=_format_instant(seg_start),
            end=_format_instant(seg_end),
            status="covered",
        )
        for seg_start, seg_end in merged
    ]
    missing: list[CoverageSegment] = []
    if sync_range is not None and _is_explicit_intraday_bound(requested_start) and _is_explicit_intraday_bound(
        requested_end
    ):
        missing = [
            CoverageSegment(
                start=_format_instant(seg_start),
                end=_format_instant(seg_end),
                status="missing",
            )
            for seg_start, seg_end in _invert_datetime_ranges(
                merged,
                window_start=window_start,
                window_end=window_end,
                step=step,
            )
        ]
    return covered, missing


def _build_daily_fetch_segments(
    *,
    requested_start: str,
    requested_end: str,
    bars: list[dict[str, Any]],
) -> list[CoverageSegment]:
    window_start = _coerce_dateish(requested_start, field_name="requested_start")
    window_end = _coerce_dateish(requested_end, field_name="requested_end")
    _ensure_window_order(
        requested_start=requested_start,
        requested_end=requested_end,
        start_value=window_start,
        end_value=window_end,
    )
    covered_days = {
        _coerce_dateish(bar.get("timestamp"), field_name="timestamp")
        for bar in bars
        if window_start <= _coerce_dateish(bar.get("timestamp"), field_name="timestamp") <= window_end
    }
    missing_days: list[date] = []
    current = window_start
    while current <= window_end:
        if current.weekday() < 5 and current not in covered_days:
            missing_days.append(current)
        current += timedelta(days=1)
    return [
        CoverageSegment(start=_format_day(seg_start), end=_format_day(seg_end), status="missing")
        for seg_start, seg_end in consecutive_trading_day_ranges(missing_days)
    ]


def _build_intraday_fetch_segments(
    *,
    requested_start: str,
    requested_end: str,
    bars: list[dict[str, Any]],
) -> list[CoverageSegment]:
    window_start = _coerce_intradayish(requested_start, field_name="requested_start", is_end=False)
    window_end = _coerce_intradayish(requested_end, field_name="requested_end", is_end=True)
    _ensure_window_order(
        requested_start=requested_start,
        requested_end=requested_end,
        start_value=window_start,
        end_value=window_end,
    )
    step = timedelta(minutes=5)
    points_by_day: dict[date, list[datetime]] = {}
    for bar in bars:
        point = _coerce_intradayish(bar.get("timestamp"), field_name="timestamp", is_end=False)
        if point < window_start or point > window_end:
            continue
        points_by_day.setdefault(point.date(), []).append(point)

    segments: list[CoverageSegment] = []
    current_day = window_start.date()
    end_day = window_end.date()
    while current_day <= end_day:
        if current_day.weekday() >= 5:
            current_day += timedelta(days=1)
            continue
        day_points = sorted(set(points_by_day.get(current_day, [])))
        if not day_points:
            segments.append(
                CoverageSegment(
                    start=current_day.isoformat(),
                    end=current_day.isoformat(),
                    status="missing",
                )
            )
            current_day += timedelta(days=1)
            continue

        if _is_explicit_intraday_bound(requested_start) and current_day == window_start.date():
            left_end = day_points[0] - step
            if left_end >= window_start:
                segments.append(
                    CoverageSegment(
                        start=_format_instant(window_start),
                        end=_format_instant(left_end),
                        status="missing",
                    )
                )
        for previous, current in zip(day_points, day_points[1:]):
            gap_start = previous + step
            gap_end = current - step
            if gap_start <= gap_end:
                segments.append(
                    CoverageSegment(
                        start=_format_instant(gap_start),
                        end=_format_instant(gap_end),
                        status="missing",
                    )
                )
        if _is_explicit_intraday_bound(requested_end) and current_day == window_end.date():
            right_start = day_points[-1] + step
            if right_start <= window_end:
                segments.append(
                    CoverageSegment(
                        start=_format_instant(right_start),
                        end=_format_instant(window_end),
                        status="missing",
                    )
                )
        current_day += timedelta(days=1)
    return segments


def _coerce_float(value: Any, *, field_name: str, index: int) -> float:
    if value is None:
        raise ValueError(f"bar[{index}] missing required numeric field {field_name!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"bar[{index}] field {field_name!r} must be numeric, got {type(value).__name__}: {value!r}"
        ) from exc


def _coerce_dateish(value: Any, *, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{field_name} must be a date/datetime string or object, got {type(value).__name__}: {value!r}"
        )
    text = value.strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"{field_name} must start with YYYY-MM-DD, got {text!r}") from exc


def _coerce_intradayish(value: Any, *, field_name: str, is_end: bool) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.max.time() if is_end else datetime.min.time(), timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return _query_bound(text, interval="5m", is_end=is_end)
        except ValueError as exc:
            normalized = normalize_bar_timestamp(text)
            if field_name not in {"requested_start", "requested_end"} and "T" in normalized:
                try:
                    return datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            raise ValueError(f"{field_name} invalid for 5m coverage: {exc}") from exc
    else:
        raise ValueError(
            f"{field_name} must be a datetime/date string or object, got {type(value).__name__}: {value!r}"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_explicit_intraday_bound(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    if isinstance(value, date):
        return False
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    normalized = text[:-1] if text.endswith("Z") else text
    return "T" in normalized or " " in normalized


def _daily_sync_overlap(
    sync_state: dict[str, Any] | None,
    *,
    window_start: date,
    window_end: date,
) -> tuple[date, date] | None:
    if not sync_state or sync_state.get("status") != "ok":
        return None
    covered_start = sync_state.get("covered_start")
    covered_end = sync_state.get("covered_end")
    if covered_start is None or covered_end is None:
        return None
    sync_start = _coerce_dateish(covered_start, field_name="covered_start")
    sync_end = _coerce_dateish(covered_end, field_name="covered_end")
    overlap_start = max(window_start, sync_start)
    overlap_end = min(window_end, sync_end)
    if overlap_end < overlap_start:
        return None
    return overlap_start, overlap_end


def _intraday_sync_overlap(
    sync_state: dict[str, Any] | None,
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime] | None:
    if not sync_state or sync_state.get("status") != "ok":
        return None
    covered_start = sync_state.get("covered_start")
    covered_end = sync_state.get("covered_end")
    if covered_start is None or covered_end is None:
        return None
    sync_start = _coerce_intradayish(covered_start, field_name="covered_start", is_end=False)
    sync_end = _coerce_intradayish(covered_end, field_name="covered_end", is_end=True)
    overlap_start = max(window_start, sync_start)
    overlap_end = min(window_end, sync_end)
    if overlap_end < overlap_start:
        return None
    return overlap_start, overlap_end


def _ensure_window_order(
    *,
    requested_start: str,
    requested_end: str,
    start_value: date | datetime,
    end_value: date | datetime,
) -> None:
    if end_value < start_value:
        raise ValueError(
            f"requested_end must be on or after requested_start, got {requested_start!r}..{requested_end!r}"
        )


def _merge_datetime_ranges(
    ranges: list[tuple[datetime, datetime]],
    *,
    step: timedelta,
) -> list[tuple[datetime, datetime]]:
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda item: (item[0], item[1]))
    merged: list[tuple[datetime, datetime]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end + step:
            merged[-1] = (last_start, max(last_end, end))
            continue
        merged.append((start, end))
    return merged


def _invert_datetime_ranges(
    ranges: list[tuple[datetime, datetime]],
    *,
    window_start: datetime,
    window_end: datetime,
    step: timedelta,
) -> list[tuple[datetime, datetime]]:
    if window_end < window_start:
        return []
    if not ranges:
        return [(window_start, window_end)]
    missing: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for start, end in ranges:
        if start > cursor:
            missing.append((cursor, start - step))
        next_point = end + step
        if next_point > cursor:
            cursor = next_point
    if cursor <= window_end:
        missing.append((cursor, window_end))
    return missing


def _format_day(value: date) -> str:
    return value.isoformat()


def _format_instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()
