import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from doyoutrade.persistence.db import Base, create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.errors import PersistenceError
from doyoutrade.persistence.repositories import SqlAlchemyMarketBarsRepository


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


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
        "adjust_type": "qfq",
    }


class MarketBarsRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "market-test.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyMarketBarsRepository(self.session_factory)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_upsert_and_query_daily_bars(self):
        await self.repo.upsert_bars(
            provider="qmt",
            adjust="qfq",
            interval="1d",
            bars=[_bar("600000.SH", "2026-01-02", 11.0)],
        )
        rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="1d",
            start=_dt("2026-01-01T00:00:00+00:00"),
            end=_dt("2026-01-03T00:00:00+00:00"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 11.0)
        self.assertEqual(rows[0]["timestamp"], "2026-01-02")

    async def test_large_batch_is_chunked_under_bind_param_limit(self):
        # A multi-year single-symbol backfill produces thousands of rows. At 13
        # columns/row a single INSERT would bind > 32767 params and raise
        # asyncpg/SQLite "too many variables". upsert_bars must chunk so every
        # row still lands. 3500 rows * 13 = 45500 params > the cap in one stmt.
        from datetime import timedelta

        base = datetime(2000, 1, 1, tzinfo=timezone.utc)
        n = 3500
        bars = [
            _bar("600000.SH", (base + timedelta(days=i)).strftime("%Y-%m-%d"), 10.0 + i * 0.001)
            for i in range(n)
        ]
        accepted = await self.repo.upsert_bars(
            provider="qmt", adjust="qfq", interval="1d", bars=bars,
        )
        self.assertEqual(accepted, n)
        rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="1d",
            start=base,
            end=base + timedelta(days=n + 1),
        )
        self.assertEqual(len(rows), n)

    async def test_upsert_is_idempotent_and_updates_values(self):
        await self.repo.upsert_bars(
            provider="qmt",
            adjust="qfq",
            interval="1d",
            bars=[_bar("600000.SH", "2026-01-02", 11.0)],
        )
        await self.repo.upsert_bars(
            provider="qmt",
            adjust="qfq",
            interval="1d",
            bars=[_bar("600000.SH", "2026-01-02", 99.0)],
        )
        rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="1d",
            start=_dt("2026-01-01T00:00:00+00:00"),
            end=_dt("2026-01-03T00:00:00+00:00"),
        )
        self.assertEqual([row["close"] for row in rows], [99.0])

    async def test_concurrent_upserts_for_same_bar_do_not_conflict(self):
        await asyncio.gather(
            self.repo.upsert_bars(
                provider="qmt",
                adjust="qfq",
                interval="1d",
                bars=[_bar("600000.SH", "2026-01-02", 11.0)],
            ),
            self.repo.upsert_bars(
                provider="qmt",
                adjust="qfq",
                interval="1d",
                bars=[_bar("600000.SH", "2026-01-02", 12.0)],
            ),
        )
        rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="1d",
            start=_dt("2026-01-01T00:00:00+00:00"),
            end=_dt("2026-01-03T00:00:00+00:00"),
        )
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["close"], {11.0, 12.0})

    async def test_provider_and_adjust_do_not_contaminate(self):
        await self.repo.upsert_bars(
            provider="qmt",
            adjust="qfq",
            interval="1d",
            bars=[_bar("600000.SH", "2026-01-02", 11.0)],
        )
        await self.repo.upsert_bars(
            provider="akshare",
            adjust="qfq",
            interval="1d",
            bars=[_bar("600000.SH", "2026-01-02", 12.0)],
        )
        await self.repo.upsert_bars(
            provider="qmt",
            adjust="hfq",
            interval="1d",
            bars=[_bar("600000.SH", "2026-01-02", 13.0)],
        )
        qmt_rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="1d",
            start=_dt("2026-01-01T00:00:00+00:00"),
            end=_dt("2026-01-03T00:00:00+00:00"),
        )
        self.assertEqual([row["close"] for row in qmt_rows], [11.0])
        hfq_rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="hfq",
            symbol="600000.SH",
            interval="1d",
            start=_dt("2026-01-01T00:00:00+00:00"),
            end=_dt("2026-01-03T00:00:00+00:00"),
        )
        self.assertEqual([row["close"] for row in hfq_rows], [13.0])

    async def test_daily_intraday_timestamp_normalizes_to_single_daily_row(self):
        await self.repo.upsert_bars(
            provider="qmt",
            adjust="qfq",
            interval="1d",
            bars=[_bar("600000.SH", "2026-01-02T15:00:00", 11.0)],
        )
        await self.repo.upsert_bars(
            provider="qmt",
            adjust="qfq",
            interval="1d",
            bars=[_bar("600000.SH", "2026-01-02", 99.0)],
        )
        rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="1d",
            start=_dt("2026-01-01T00:00:00+00:00"),
            end=_dt("2026-01-03T00:00:00+00:00"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp"], "2026-01-02")
        self.assertEqual(rows[0]["close"], 99.0)

    async def test_daily_timezone_aware_timestamp_preserves_source_date(self):
        await self.repo.upsert_bars(
            provider="qmt",
            adjust="qfq",
            interval="1d",
            bars=[_bar("600000.SH", "2026-01-02T00:00:00+08:00", 11.0)],
        )
        rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="1d",
            start=datetime.fromisoformat("2026-01-02T00:00:00+08:00"),
            end=datetime.fromisoformat("2026-01-02T23:59:00+08:00"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp"], "2026-01-02")
        self.assertEqual(rows[0]["close"], 11.0)

    async def test_daily_intraday_query_bounds_find_daily_row(self):
        await self.repo.upsert_bars(
            provider="qmt",
            adjust="qfq",
            interval="1d",
            bars=[_bar("600000.SH", "2026-01-02", 11.0)],
        )
        rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="1d",
            start=_dt("2026-01-02T09:30:00+00:00"),
            end=_dt("2026-01-02T15:00:00+00:00"),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timestamp"], "2026-01-02")
        self.assertEqual(rows[0]["close"], 11.0)

    async def test_upsert_returns_unique_identity_count_for_duplicate_batch(self):
        accepted = await self.repo.upsert_bars(
            provider="qmt",
            adjust="qfq",
            interval="1d",
            bars=[
                _bar("600000.SH", "2026-01-02", 11.0),
                _bar("600000.SH", "2026-01-02T15:00:00", 12.0),
            ],
        )
        rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="1d",
            start=_dt("2026-01-01T00:00:00+00:00"),
            end=_dt("2026-01-03T00:00:00+00:00"),
        )
        self.assertEqual(accepted, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 12.0)

    async def test_sync_state_success_and_failure(self):
        await self.repo.mark_sync_success(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="5m",
            target_start=_dt("2025-01-01T00:00:00+00:00"),
            target_end=_dt("2026-01-01T00:00:00+00:00"),
            covered_start=_dt("2025-01-01T00:00:00+00:00"),
            covered_end=_dt("2026-01-01T00:00:00+00:00"),
        )
        state = await self.repo.get_sync_state(
            provider="qmt", adjust="qfq", symbol="600000.SH", interval="5m"
        )
        self.assertEqual(state["status"], "ok")
        await self.repo.mark_sync_failure(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="5m",
            target_start=_dt("2025-01-01T00:00:00+00:00"),
            target_end=_dt("2026-01-01T00:00:00+00:00"),
            error_code="upstream_fetch_failed",
            error_type="RuntimeError",
            error_message="boom",
        )
        state = await self.repo.get_sync_state(
            provider="qmt", adjust="qfq", symbol="600000.SH", interval="5m"
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["last_error_code"], "upstream_fetch_failed")
        self.assertEqual(state["retry_count"], 1)

    async def test_intraday_timestamp_is_returned_without_timezone_suffix(self):
        await self.repo.upsert_bars(
            provider="qmt",
            adjust="qfq",
            interval="5m",
            bars=[_bar("600000.SH", "2026-01-02T09:35:00+08:00", 11.0)],
        )
        rows = await self.repo.bars_in_range(
            provider="qmt",
            adjust="qfq",
            symbol="600000.SH",
            interval="5m",
            start=_dt("2026-01-02T01:30:00+00:00"),
            end=_dt("2026-01-02T01:40:00+00:00"),
        )
        self.assertEqual([row["timestamp"] for row in rows], ["2026-01-02T01:35:00"])

    async def test_intraday_timestamp_without_timezone_raises_persistence_error(self):
        with self.assertRaises(PersistenceError) as ctx:
            await self.repo.upsert_bars(
                provider="qmt",
                adjust="qfq",
                interval="5m",
                bars=[_bar("600000.SH", "2026-01-02T09:35:00", 11.0)],
            )
        message = str(ctx.exception)
        self.assertIn("timestamp", message)
        self.assertIn("2026-01-02T09:35:00", message)

    async def test_invalid_bar_payload_raises_persistence_error(self):
        with self.assertRaises(PersistenceError):
            await self.repo.upsert_bars(
                provider="qmt",
                adjust="qfq",
                interval="1d",
                bars=[{"symbol": "600000.SH", "timestamp": ""}],
            )

    async def test_blank_identity_inputs_raise_persistence_error(self):
        with self.assertRaises(PersistenceError) as provider_ctx:
            await self.repo.upsert_bars(
                provider=" ",
                adjust="qfq",
                interval="1d",
                bars=[_bar("600000.SH", "2026-01-02", 11.0)],
            )
        self.assertIn("provider", str(provider_ctx.exception))

        with self.assertRaises(PersistenceError) as interval_ctx:
            await self.repo.bars_in_range(
                provider="qmt",
                adjust="qfq",
                symbol="600000.SH",
                interval="",
                start=_dt("2026-01-01T00:00:00+00:00"),
                end=_dt("2026-01-03T00:00:00+00:00"),
            )
        self.assertIn("interval", str(interval_ctx.exception))

        with self.assertRaises(PersistenceError) as symbol_ctx:
            await self.repo.get_sync_state(
                provider="qmt",
                adjust="qfq",
                symbol=" ",
                interval="5m",
            )
        self.assertIn("symbol", str(symbol_ctx.exception))

    async def test_invalid_numeric_bar_payload_raises_persistence_error(self):
        cases = [
            ("close", True),
            ("close", "nan"),
            ("volume", "inf"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                bar = _bar("600000.SH", "2026-01-02", 11.0)
                bar[field] = value
                with self.assertRaises(PersistenceError) as ctx:
                    await self.repo.upsert_bars(
                        provider="qmt",
                        adjust="qfq",
                        interval="1d",
                        bars=[bar],
                    )
                message = str(ctx.exception)
                self.assertIn(field, message)
                self.assertIn(repr(value), message)
