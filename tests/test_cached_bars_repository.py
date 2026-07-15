"""Tests for :class:`doyoutrade.persistence.SqlAlchemyCachedBarsRepository`.

Backs the DB-side of :class:`doyoutrade.data.bars_cache_store.RepositoryBarsCacheStore`.
Covers the three invariants the rest of the cache layer relies on:

* ``(provider, symbol, interval, bar_timestamp)`` is the PK — same
  symbol can be cached side-by-side across data sources without
  cross-source contamination (the bug the in-memory cache had).
* ``cached_bar_ranges`` rows compact into the smallest covering set so
  a backtest that asks for ``[start, end]`` after several adjacent
  fetches sees a single ``covered_ranges`` entry, not N.
* Mixing ``adjust`` modes within one ``(provider, symbol, interval)``
  is refused — refused-then-keep-existing is the only way to keep the
  cache from silently mixing qfq + hfq rows.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from doyoutrade.persistence.db import Base, create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.errors import PersistenceError
from doyoutrade.persistence.repositories import SqlAlchemyCachedBarsRepository


def _bar(symbol: str, day: str, close: float) -> dict:
    return {
        "symbol": symbol,
        "timestamp": day,
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": 1000.0,
        "amount": close * 1000.0,
    }


class SqlAlchemyCachedBarsRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyCachedBarsRepository(self.session_factory)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_round_trip_records_and_returns_bars(self):
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-03",
            bars=[_bar("600000.SH", "2026-01-02", 11.0)],
        )

        ranges = await self.repo.covered_ranges(provider="qmt", symbol="600000.SH", interval="1d")
        self.assertEqual(ranges, [("2026-01-01", "2026-01-03")])

        bars = await self.repo.bars_in_range(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-03",
        )
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 11.0)
        self.assertEqual(bars[0]["adjust_type"], "qfq")

    async def test_same_symbol_different_provider_does_not_collide(self):
        """Rows under (qmt, X, 1d) and (akshare, X, 1d) live in parallel."""
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-02",
            bars=[_bar("600000.SH", "2026-01-01", 10.0)],
        )
        await self.repo.record_fetch(
            provider="akshare",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-02",
            bars=[_bar("600000.SH", "2026-01-01", 12.5)],
        )

        qmt_bars = await self.repo.bars_in_range(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-02",
        )
        ak_bars = await self.repo.bars_in_range(
            provider="akshare",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-02",
        )
        self.assertEqual(qmt_bars[0]["close"], 10.0)
        self.assertEqual(ak_bars[0]["close"], 12.5)

    async def test_adjacent_ranges_compact_into_single_coverage(self):
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-03",
            bars=[],
        )
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-04",
            end="2026-01-06",
            bars=[],
        )

        ranges = await self.repo.covered_ranges(
            provider="qmt", symbol="600000.SH", interval="1d"
        )
        # Compacted because 2026-01-03 + 1 day = 2026-01-04.
        self.assertEqual(ranges, [("2026-01-01", "2026-01-06")])

    async def test_weekend_gap_ranges_compact_into_single_coverage(self):
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-02",
            end="2026-01-02",
            bars=[],
        )
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-05",
            end="2026-01-09",
            bars=[],
        )

        ranges = await self.repo.covered_ranges(
            provider="qmt", symbol="600000.SH", interval="1d"
        )
        self.assertEqual(ranges, [("2026-01-02", "2026-01-09")])

    async def test_disjoint_ranges_stay_separate(self):
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-03",
            bars=[],
        )
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-10",
            end="2026-01-12",
            bars=[],
        )
        ranges = await self.repo.covered_ranges(
            provider="qmt", symbol="600000.SH", interval="1d"
        )
        self.assertEqual(
            ranges,
            [("2026-01-01", "2026-01-03"), ("2026-01-10", "2026-01-12")],
        )

    async def test_upsert_overwrites_bar_fields(self):
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-02",
            end="2026-01-02",
            bars=[_bar("600000.SH", "2026-01-02", 11.0)],
        )
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-02",
            end="2026-01-02",
            bars=[_bar("600000.SH", "2026-01-02", 99.0)],
        )

        bars = await self.repo.bars_in_range(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-02",
            end="2026-01-02",
        )
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["close"], 99.0)

    async def test_mixed_adjust_modes_coexist(self):
        """Different adjust modes (none/qfq/hfq) are stored separately since adjust is part of PK."""
        # Record qfq data
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-02",
            bars=[_bar("600000.SH", "2026-01-01", 10.0)],
            adjust="qfq",
        )

        # Record none data (explicit non-adjusted mode)
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-03",
            end="2026-01-04",
            bars=[_bar("600000.SH", "2026-01-03", 10.5)],
            adjust="none",
        )

        # qfq bars
        qfq_bars = await self.repo.bars_in_range(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-01",
            end="2026-01-02",
            adjust="qfq",
        )
        self.assertEqual(len(qfq_bars), 1)
        self.assertEqual(qfq_bars[0]["close"], 10.0)
        self.assertEqual(qfq_bars[0]["adjust_type"], "qfq")

        # none bars (explicit non-adjusted mode)
        none_bars = await self.repo.bars_in_range(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-01-03",
            end="2026-01-04",
            adjust="none",
        )
        self.assertEqual(len(none_bars), 1)
        self.assertEqual(none_bars[0]["close"], 10.5)
        self.assertEqual(none_bars[0]["adjust_type"], "none")

        # Check ranges are also separate
        qfq_ranges = await self.repo.covered_ranges(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            adjust="qfq",
        )
        self.assertEqual(qfq_ranges, [("2026-01-01", "2026-01-02")])

        none_ranges = await self.repo.covered_ranges(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            adjust="none",
        )
        self.assertEqual(none_ranges, [("2026-01-03", "2026-01-04")])

    async def test_bars_in_range_handles_intraday_timestamps(self):
        """Date-prefix filter must include intraday bars on the end date."""
        intraday = {
            "symbol": "600000.SH",
            "timestamp": "2026-01-02T15:00:00",
            "open": 11.0,
            "high": 11.2,
            "low": 10.9,
            "close": 11.1,
            "volume": 5000.0,
            "amount": 55000.0,
        }
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1m",
            start="2026-01-02",
            end="2026-01-02",
            bars=[intraday],
        )
        bars = await self.repo.bars_in_range(
            provider="qmt",
            symbol="600000.SH",
            interval="1m",
            start="2026-01-02",
            end="2026-01-02",
        )
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["timestamp"], "2026-01-02T15:00:00")

    async def test_invalidate_symbol_cache_removes_bars_and_ranges(self):
        """Adjust-drift self-heal: the whole (provider, symbol, interval, adjust)
        key is dropped — bars AND coverage ranges — without touching sibling
        adjust modes or other providers."""
        await self.repo.record_fetch(
            provider="qmt",
            symbol="000636.SZ",
            interval="1d",
            start="2026-01-01",
            end="2026-01-03",
            bars=[_bar("000636.SZ", "2026-01-01", 130.0), _bar("000636.SZ", "2026-01-02", 131.0)],
        )
        await self.repo.record_fetch(
            provider="qmt",
            symbol="000636.SZ",
            interval="1d",
            start="2026-01-01",
            end="2026-01-01",
            bars=[_bar("000636.SZ", "2026-01-01", 99.0)],
            adjust="none",
        )
        await self.repo.record_fetch(
            provider="akshare",
            symbol="000636.SZ",
            interval="1d",
            start="2026-01-01",
            end="2026-01-01",
            bars=[_bar("000636.SZ", "2026-01-01", 128.0)],
        )

        removed = await self.repo.invalidate_symbol_cache(
            provider="qmt", symbol="000636.SZ", interval="1d", adjust="qfq"
        )

        self.assertEqual(removed, 2)
        self.assertEqual(
            await self.repo.covered_ranges(
                provider="qmt", symbol="000636.SZ", interval="1d", adjust="qfq"
            ),
            [],
        )
        self.assertEqual(
            await self.repo.bars_in_range(
                provider="qmt", symbol="000636.SZ", interval="1d",
                start="2026-01-01", end="2026-01-03", adjust="qfq",
            ),
            [],
        )
        # Sibling adjust mode and other provider untouched.
        none_bars = await self.repo.bars_in_range(
            provider="qmt", symbol="000636.SZ", interval="1d",
            start="2026-01-01", end="2026-01-01", adjust="none",
        )
        self.assertEqual([b["close"] for b in none_bars], [99.0])
        ak_bars = await self.repo.bars_in_range(
            provider="akshare", symbol="000636.SZ", interval="1d",
            start="2026-01-01", end="2026-01-01",
        )
        self.assertEqual([b["close"] for b in ak_bars], [128.0])
        # Idempotent: a second invalidate finds nothing.
        self.assertEqual(
            await self.repo.invalidate_symbol_cache(
                provider="qmt", symbol="000636.SZ", interval="1d", adjust="qfq"
            ),
            0,
        )

    async def test_repository_bars_cache_store_invalidate_round_trip(self):
        """RepositoryBarsCacheStore.invalidate delegates to invalidate_symbol_cache."""
        from doyoutrade.data.bars_cache_store import RepositoryBarsCacheStore

        store = RepositoryBarsCacheStore(self.repo)
        await self.repo.record_fetch(
            provider="qmt",
            symbol="000636.SZ",
            interval="1d",
            start="2026-01-01",
            end="2026-01-02",
            bars=[_bar("000636.SZ", "2026-01-01", 130.0), _bar("000636.SZ", "2026-01-02", 131.0)],
        )

        removed = await store.invalidate(provider="qmt", symbol="000636.SZ", interval="1d")

        self.assertEqual(removed, 2)
        self.assertEqual(
            await store.covered_ranges(provider="qmt", symbol="000636.SZ", interval="1d"),
            [],
        )
        self.assertEqual(
            await store.bars_in_range(
                provider="qmt", symbol="000636.SZ", interval="1d",
                start="2026-01-01", end="2026-01-02",
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
