"""Pluggable backends for the OHLCV bar cache.

``CachedBarsDataProvider`` used to keep its cache in a per-instance
``dict[tuple[str, str], _BarCacheEntry]`` keyed by ``(symbol, interval)``.
Two problems with that:

* It was process-local, so every fresh backtest paid the full upstream
  cost (network + rate limit) even for symbols that had been pulled
  five minutes earlier.
* ``provider`` wasn't part of the key, so a strategy that asked akshare
  for qfq daily bars and later switched to tushare's qfq would have
  silently been served akshare's rows.

This module defines the storage contract and ships two implementations:

* :class:`InMemoryBarsCacheStore` — the legacy ``dict`` form, kept as
  the unit-test default so the existing ``CachedBarsDataProvider`` test
  matrix doesn't need a SQLAlchemy fixture.
* :class:`RepositoryBarsCacheStore` — wraps
  :class:`doyoutrade.persistence.SqlAlchemyCachedBarsRepository` for
  the production runtime (live + backtest).

The bootstrap layer picks the implementation; the data provider only
ever talks to the protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Protocol, runtime_checkable

from doyoutrade.core.models import Bar
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST


def _next_day(value: str) -> str:
    try:
        return (date.fromisoformat(value[:10]) + timedelta(days=1)).isoformat()
    except ValueError:
        return value


def _merge_ranges(ranges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge overlapping / adjacent ``(start, end)`` ranges by ISO calendar day."""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged: list[tuple[str, str]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= _next_day(prev_end):
            merged[-1] = (prev_start, max(prev_end, end))
            continue
        merged.append((start, end))
    return merged


def is_range_covered(ranges: list[tuple[str, str]], start: str, end: str) -> bool:
    """``True`` when one merged range fully covers ``[start, end]``."""
    return any(cached_start <= start and end <= cached_end for cached_start, cached_end in ranges)


@runtime_checkable
class BarsCacheStore(Protocol):
    """Storage backend for cached OHLCV bars and their coverage ranges.

    Implementations must be safe to call concurrently from the worker
    event loop (the in-memory store is single-task, the repository
    store relies on SQLAlchemy's async session per-call pattern).
    """

    async def covered_ranges(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[tuple[str, str]]: ...

    async def bars_in_range(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[Bar]: ...

    async def suspended_days_in_range(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> set[str]: ...

    async def record_fetch(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        bars: list[Bar],
        adjust: str = DEFAULT_BAR_ADJUST,
        suspended_days: set[str] | None = None,
    ) -> None: ...

    async def invalidate(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> int: ...


@dataclass
class _InMemoryEntry:
    bars_by_timestamp: dict[str, Bar] = field(default_factory=dict)
    ranges: list[tuple[str, str]] = field(default_factory=list)
    suspended_days: set[str] = field(default_factory=set)


class InMemoryBarsCacheStore:
    """Per-process dict-backed cache. Default for unit tests."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str, str], _InMemoryEntry] = {}

    async def covered_ranges(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[tuple[str, str]]:
        entry = self._entries.get((provider, symbol, interval, adjust))
        if entry is None:
            return []
        return list(entry.ranges)

    async def bars_in_range(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[Bar]:
        entry = self._entries.get((provider, symbol, interval, adjust))
        if entry is None:
            return []
        return [
            entry.bars_by_timestamp[ts]
            for ts in sorted(entry.bars_by_timestamp)
            if start[:10] <= ts[:10] <= end[:10]
        ]

    async def suspended_days_in_range(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> set[str]:
        entry = self._entries.get((provider, symbol, interval, adjust))
        if entry is None:
            return set()
        return {
            day for day in entry.suspended_days if start[:10] <= day[:10] <= end[:10]
        }

    async def record_fetch(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        bars: list[Bar],
        adjust: str = DEFAULT_BAR_ADJUST,
        suspended_days: set[str] | None = None,
    ) -> None:
        key = (provider, symbol, interval, adjust)
        entry = self._entries.setdefault(key, _InMemoryEntry())
        for bar in bars:
            ts = normalize_bar_timestamp(bar.timestamp)
            if ts:
                entry.bars_by_timestamp[ts] = bar
        for raw_day in suspended_days or ():
            day = str(raw_day or "").strip()[:10]
            if day:
                entry.suspended_days.add(day)
        entry.ranges.append((start, end))
        entry.ranges = _merge_ranges(entry.ranges)

    async def invalidate(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> int:
        """Drop the whole entry for the key; returns the number of bars removed.

        Used by the adjust-drift self-heal: a 复权 factor change stales every
        bar under the key, so partial deletes would leave a price cliff.
        """
        entry = self._entries.pop((provider, symbol, interval, adjust), None)
        if entry is None:
            return 0
        return len(entry.bars_by_timestamp)


class RepositoryBarsCacheStore:
    """DB-backed store wrapping :class:`SqlAlchemyCachedBarsRepository`."""

    def __init__(self, repository: Any) -> None:
        self._repo = repository

    async def covered_ranges(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[tuple[str, str]]:
        return await self._repo.covered_ranges(
            provider=provider, symbol=symbol, interval=interval, adjust=adjust
        )

    async def bars_in_range(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[Bar]:
        rows = await self._repo.bars_in_range(
            provider=provider,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            adjust=adjust,
        )
        return [Bar(**row) for row in rows]

    async def suspended_days_in_range(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> set[str]:
        return await self._repo.suspended_days_in_range(
            provider=provider,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            adjust=adjust,
        )

    async def record_fetch(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        bars: list[Bar],
        adjust: str = DEFAULT_BAR_ADJUST,
        suspended_days: set[str] | None = None,
    ) -> None:
        payload = [
            {
                "symbol": bar.symbol,
                "timestamp": normalize_bar_timestamp(bar.timestamp),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "amount": bar.amount,
            }
            for bar in bars
        ]
        await self._repo.record_fetch(
            provider=provider,
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            bars=payload,
            adjust=adjust,
            suspended_days=suspended_days,
        )

    async def invalidate(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> int:
        return await self._repo.invalidate_symbol_cache(
            provider=provider, symbol=symbol, interval=interval, adjust=adjust
        )


__all__ = [
    "BarsCacheStore",
    "InMemoryBarsCacheStore",
    "RepositoryBarsCacheStore",
    "is_range_covered",
]
