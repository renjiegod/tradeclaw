from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from doyoutrade.persistence.cached_bars_cleanup import (
    POISONED_NONE_DAILY_CLOSE_THRESHOLD,
    purge_poisoned_cached_bars,
)
from doyoutrade.persistence.db import Base, create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.repositories import SqlAlchemyCachedBarsRepository


class CachedBarsCleanupTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_skips_when_no_poisoned_rows(self) -> None:
        await self.repo.record_fetch(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-06-01",
            end="2026-06-03",
            bars=[
                {
                    "symbol": "600000.SH",
                    "timestamp": "2026-06-02",
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "volume": 1000.0,
                }
            ],
            adjust="none",
        )

        result = await purge_poisoned_cached_bars(self.session_factory)

        self.assertEqual(result["poisoned_rows"], 0)
        bars = await self.repo.bars_in_range(
            provider="qmt",
            symbol="600000.SH",
            interval="1d",
            start="2026-06-01",
            end="2026-06-03",
            adjust="none",
        )
        self.assertEqual(len(bars), 1)

    async def test_purges_legacy_qfq_relabeled_as_none(self) -> None:
        await self.repo.record_fetch(
            provider="qmt",
            symbol="000636.SZ",
            interval="1d",
            start="2026-06-05",
            end="2026-06-09",
            bars=[
                {
                    "symbol": "000636.SZ",
                    "timestamp": "2026-06-05",
                    "open": 590.0,
                    "high": 600.0,
                    "low": 580.0,
                    "close": POISONED_NONE_DAILY_CLOSE_THRESHOLD + 1.0,
                    "volume": 1000.0,
                }
            ],
            adjust="none",
        )

        result = await purge_poisoned_cached_bars(self.session_factory)

        self.assertEqual(result["poisoned_rows"], 1)
        self.assertGreaterEqual(result["deleted_bars"], 1)
        bars = await self.repo.bars_in_range(
            provider="qmt",
            symbol="000636.SZ",
            interval="1d",
            start="2026-06-05",
            end="2026-06-09",
            adjust="none",
        )
        self.assertEqual(bars, [])
