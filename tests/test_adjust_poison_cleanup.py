from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.persistence.adjust_poison_cleanup import (
    QFQ_POISON_RATIO_THRESHOLD,
    purge_poisoned_qfq_rows,
)
from doyoutrade.persistence.db import Base, create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.models import (
    CachedBarRangeRecord,
    CachedBarRecord,
    MarketBarRecord,
    MarketBarSyncStateRecord,
)


class AdjustPoisonCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_skips_when_no_poisoned_rows(self) -> None:
        await self._insert_market_pair(symbol="600000.SH", qfq_close=10.5, none_close=10.4)

        result = await purge_poisoned_qfq_rows(self.session_factory, self.session_factory)

        self.assertEqual(result["poisoned_keys"], 0)
        self.assertEqual(await self._count("market_bars"), 2)

    async def test_purges_poisoned_market_and_cached_qfq_rows(self) -> None:
        await self._insert_market_pair(
            symbol="000636.SZ",
            qfq_close=55.0 * (QFQ_POISON_RATIO_THRESHOLD + 1.0),
            none_close=55.0,
        )
        await self._insert_market_pair(
            symbol="000001.SZ",
            qfq_close=12.0 * (QFQ_POISON_RATIO_THRESHOLD + 2.0),
            none_close=12.0,
        )
        await self._insert_cached_qfq(symbol="000636.SZ", provider="qmt")
        await self._insert_cached_qfq(symbol="000001.SZ", provider="baostock")

        result = await purge_poisoned_qfq_rows(self.session_factory, self.session_factory)

        self.assertEqual(result["poisoned_keys"], 2)
        self.assertEqual(result["poisoned_symbols"], 2)
        self.assertGreaterEqual(result["deleted_market_bars"], 2)
        self.assertGreaterEqual(result["deleted_market_sync_state"], 2)
        self.assertGreaterEqual(result["deleted_cached_bars"], 2)
        self.assertGreaterEqual(result["deleted_cached_ranges"], 2)

        async with self.session_factory() as session:
            remaining_market_qfq = (
                await session.execute(
                    sa_text(
                        "select count(*) from market_bars where adjust='qfq' and symbol in ('000636.SZ','000001.SZ')"
                    )
                )
            ).scalar_one()
            remaining_market_none = (
                await session.execute(
                    sa_text(
                        "select count(*) from market_bars where adjust='none' and symbol in ('000636.SZ','000001.SZ')"
                    )
                )
            ).scalar_one()
            remaining_cached_qfq = (
                await session.execute(
                    sa_text(
                        "select count(*) from cached_bars where adjust='qfq' and symbol in ('000636.SZ','000001.SZ')"
                    )
                )
            ).scalar_one()
        self.assertEqual(remaining_market_qfq, 0)
        self.assertEqual(remaining_cached_qfq, 0)
        self.assertEqual(remaining_market_none, 2)

    async def _insert_market_pair(self, *, symbol: str, qfq_close: float, none_close: float) -> None:
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            session.add(
                MarketBarRecord(
                    symbol=symbol,
                    interval="1d",
                    provider="auto",
                    adjust="qfq",
                    bar_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
                    open_price=qfq_close,
                    high_price=qfq_close,
                    low_price=qfq_close,
                    close_price=qfq_close,
                    volume=1000.0,
                    amount=1000.0,
                    source_fetched_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                MarketBarRecord(
                    symbol=symbol,
                    interval="1d",
                    provider="auto",
                    adjust="none",
                    bar_time=datetime(2026, 6, 1, tzinfo=timezone.utc),
                    open_price=none_close,
                    high_price=none_close,
                    low_price=none_close,
                    close_price=none_close,
                    volume=1000.0,
                    amount=1000.0,
                    source_fetched_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                MarketBarSyncStateRecord(
                    symbol=symbol,
                    interval="1d",
                    provider="auto",
                    adjust="qfq",
                    target_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    target_end=datetime(2026, 6, 1, tzinfo=timezone.utc),
                    covered_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    covered_end=datetime(2026, 6, 1, tzinfo=timezone.utc),
                    last_success_at=now,
                    last_attempt_at=now,
                    last_error_code=None,
                    last_error_type=None,
                    last_error_message=None,
                    retry_count=0,
                    status="ok",
                )
            )
            await session.commit()

    async def _insert_cached_qfq(self, *, symbol: str, provider: str) -> None:
        async with self.session_factory() as session:
            session.add(
                CachedBarRecord(
                    provider=provider,
                    symbol=symbol,
                    interval="1d",
                    adjust=DEFAULT_BAR_ADJUST,
                    bar_timestamp="2026-06-01",
                    open_price=500.0,
                    high_price=500.0,
                    low_price=500.0,
                    close_price=500.0,
                    volume=1000.0,
                    amount=1000.0,
                )
            )
            session.add(
                CachedBarRangeRecord(
                    provider=provider,
                    symbol=symbol,
                    interval="1d",
                    adjust=DEFAULT_BAR_ADJUST,
                    range_start="2026-06-01",
                    range_end="2026-06-01",
                )
            )
            await session.commit()

    async def _count(self, table: str) -> int:
        async with self.session_factory() as session:
            return int((await session.execute(sa_text(f"select count(*) from {table}"))).scalar_one())


def sa_text(sql: str):
    import sqlalchemy as sa

    return sa.text(sql)
