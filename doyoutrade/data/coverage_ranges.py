from __future__ import annotations

from datetime import date, timedelta


def trading_day_adjacent(previous_end: date, next_start: date) -> bool:
    """Return True when *next_start* immediately follows *previous_end* on the trading calendar.

    Weekend-only gaps (Saturday/Sunday) do not break adjacency, so a Friday bar
    range can merge with the following Monday range. Weekday gaps (holidays,
    missing data) still split ranges.
    """
    if next_start <= previous_end:
        return True
    if next_start.toordinal() == previous_end.toordinal() + 1:
        return True
    cursor = previous_end + timedelta(days=1)
    while cursor < next_start:
        if cursor.weekday() < 5:
            return False
        cursor += timedelta(days=1)
    return True


def consecutive_trading_day_ranges(days: list[date]) -> list[tuple[date, date]]:
    if not days:
        return []
    ordered = sorted(days)
    ranges: list[tuple[date, date]] = []
    start = ordered[0]
    end = ordered[0]
    for current in ordered[1:]:
        if trading_day_adjacent(end, current):
            end = current
            continue
        ranges.append((start, end))
        start = current
        end = current
    ranges.append((start, end))
    return ranges


def merge_date_ranges(ranges: list[tuple[date, date]]) -> list[tuple[date, date]]:
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda item: (item[0], item[1]))
    merged: list[tuple[date, date]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if trading_day_adjacent(last_end, start):
            merged[-1] = (last_start, max(last_end, end))
            continue
        merged.append((start, end))
    return merged


def merge_cached_day_ranges(ranges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge ``(start, end)`` day strings using trading-day adjacency."""
    if not ranges:
        return []
    parsed = [
        (_coerce_day_string(start), _coerce_day_string(end))
        for start, end in ranges
    ]
    merged = merge_date_ranges(parsed)
    return [(start.isoformat(), end.isoformat()) for start, end in merged]


def _coerce_day_string(value: str) -> date:
    text = str(value or "").strip()
    if not text:
        raise ValueError("coverage range day must be non-empty")
    return date.fromisoformat(text[:10])
