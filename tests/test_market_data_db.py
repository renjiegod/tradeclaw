import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import text

from doyoutrade.persistence.db import (
    MarketDataDatabaseError,
    create_engine_and_session_factory,
    dispose_engine,
    ensure_market_data_database_url,
    verify_market_schema,
    verify_sqlite_market_schema,
    verify_timescaledb_market_schema,
)
from doyoutrade.persistence.repositories import SqlAlchemyMarketBarsRepository
from doyoutrade.persistence.runtime_state import run_market_data_migrations


class MarketDataDatabaseTests(unittest.IsolatedAsyncioTestCase):
    def test_market_bars_migration_uses_composite_primary_key_identity(self):
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "market_data"
            / "versions"
            / "20260601_01_market_bars.py"
        )
        migration = migration_path.read_text()

        self.assertIn("sa.PrimaryKeyConstraint(", migration)
        self.assertIn('name="pk_market_bars"', migration)
        self.assertNotIn("sa.UniqueConstraint(", migration)

    def test_market_bars_migration_guards_timescale_ddl_by_dialect(self):
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "market_data"
            / "versions"
            / "20260601_01_market_bars.py"
        )
        migration = migration_path.read_text()

        self.assertIn('if dialect == "postgresql":', migration)
        self.assertIn("CREATE EXTENSION IF NOT EXISTS timescaledb", migration)
        self.assertIn("create_hypertable", migration)

    def test_market_data_url_accepts_sqlite_file(self):
        parsed = ensure_market_data_database_url("sqlite+aiosqlite:///market.db")
        self.assertEqual(parsed.drivername, "sqlite+aiosqlite")
        self.assertEqual(parsed.database, "market.db")

    def test_market_data_url_rejects_sqlite_memory(self):
        for url in ("sqlite+aiosqlite://", "sqlite+aiosqlite:///:memory:"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(
                    MarketDataDatabaseError, "market_data_database_url_sqlite_memory"
                ):
                    ensure_market_data_database_url(url)

    def test_market_data_url_rejects_unsupported_driver(self):
        with self.assertRaisesRegex(
            MarketDataDatabaseError, "market_data_database_url_unsupported_driver"
        ):
            ensure_market_data_database_url("mysql+aiomysql://user:pass@localhost/market")

    def test_market_data_url_accepts_postgresql_asyncpg(self):
        parsed = ensure_market_data_database_url(
            "postgresql+asyncpg://user:pass@localhost:5432/market"
        )
        self.assertEqual(parsed.drivername, "postgresql+asyncpg")

    def test_market_data_url_rejects_missing_database(self):
        with self.assertRaisesRegex(
            MarketDataDatabaseError, "market_data_database_url_missing_database"
        ):
            ensure_market_data_database_url("postgresql+asyncpg://user:pass@localhost")

    async def test_verify_timescaledb_market_schema_fails_when_extension_missing(self):
        conn = AsyncMock()
        conn.scalar.side_effect = [None]
        with self.assertRaisesRegex(
            MarketDataDatabaseError, "timescaledb_extension_unavailable"
        ):
            await verify_timescaledb_market_schema(conn)

    async def test_verify_timescaledb_market_schema_fails_when_hypertable_missing(self):
        conn = AsyncMock()
        conn.scalar.side_effect = ["2.15.0", None]
        with self.assertRaisesRegex(
            MarketDataDatabaseError, "market_bars_hypertable_unavailable"
        ):
            await verify_timescaledb_market_schema(conn)

    async def test_verify_timescaledb_market_schema_scopes_hypertable_check(self):
        conn = AsyncMock()
        conn.scalar.side_effect = ["2.15.0", None]
        with self.assertRaisesRegex(
            MarketDataDatabaseError, "market_bars_hypertable_unavailable"
        ):
            await verify_timescaledb_market_schema(conn)

        hypertable_query = str(conn.scalar.call_args_list[1].args[0])
        self.assertIn("hypertable_schema = current_schema()", hypertable_query)
        self.assertIn("hypertable_name = 'market_bars'", hypertable_query)

    async def test_verify_timescaledb_market_schema_fails_when_sync_state_missing(self):
        conn = AsyncMock()
        conn.scalar.side_effect = ["2.15.0", 1, None]
        with self.assertRaisesRegex(
            MarketDataDatabaseError, "market_bar_sync_state_unavailable"
        ):
            await verify_timescaledb_market_schema(conn)

    async def test_verify_timescaledb_market_schema_passes(self):
        conn = AsyncMock()
        conn.scalar.side_effect = ["2.15.0", 1, 1]
        await verify_timescaledb_market_schema(conn)

    async def test_verify_market_schema_dispatches_postgresql(self):
        conn = AsyncMock()
        conn.scalar.side_effect = ["2.15.0", 1, 1]
        await verify_market_schema(conn, drivername="postgresql+asyncpg")
        self.assertEqual(conn.scalar.await_count, 3)

    async def test_verify_market_schema_rejects_unsupported_driver(self):
        conn = AsyncMock()
        with self.assertRaisesRegex(
            MarketDataDatabaseError, "market_data_database_url_unsupported_driver"
        ):
            await verify_market_schema(conn, drivername="mysql+aiomysql")
        conn.scalar.assert_not_awaited()

    async def test_verify_sqlite_market_schema_fails_on_empty_database(self):
        with tempfile.TemporaryDirectory() as tempdir:
            url = f"sqlite+aiosqlite:///{Path(tempdir) / 'market.db'}"
            engine, _ = create_engine_and_session_factory(url)
            try:
                async with engine.begin() as conn:
                    with self.assertRaisesRegex(
                        MarketDataDatabaseError, "market_bars_table_unavailable"
                    ):
                        await verify_sqlite_market_schema(conn)
            finally:
                await dispose_engine(engine)

    async def test_verify_sqlite_market_schema_fails_when_sync_state_missing(self):
        with tempfile.TemporaryDirectory() as tempdir:
            url = f"sqlite+aiosqlite:///{Path(tempdir) / 'market.db'}"
            engine, _ = create_engine_and_session_factory(url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text("CREATE TABLE market_bars (symbol TEXT PRIMARY KEY)")
                    )
                async with engine.begin() as conn:
                    with self.assertRaisesRegex(
                        MarketDataDatabaseError, "market_bar_sync_state_unavailable"
                    ):
                        await verify_sqlite_market_schema(conn)
            finally:
                await dispose_engine(engine)

    async def test_sqlite_migrations_verify_and_repository_round_trip(self):
        """Real alembic run against a SQLite file, then verify + upsert + read."""
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "nested" / "market.db"
            url = f"sqlite+aiosqlite:///{db_path}"

            await run_market_data_migrations(url)
            self.assertTrue(db_path.is_file())

            engine, session_factory = create_engine_and_session_factory(url)
            try:
                async with engine.begin() as conn:
                    await verify_market_schema(conn, drivername="sqlite+aiosqlite")

                repo = SqlAlchemyMarketBarsRepository(session_factory)
                accepted = await repo.upsert_bars(
                    provider="akshare",
                    adjust="qfq",
                    interval="1d",
                    bars=[
                        {
                            "symbol": "600000.SH",
                            "timestamp": "2026-01-02",
                            "open": 10.5,
                            "high": 11.5,
                            "low": 10.0,
                            "close": 11.0,
                            "volume": 1000.0,
                            "amount": 11000.0,
                            "adjust_type": "qfq",
                        }
                    ],
                )
                self.assertEqual(accepted, 1)
                rows = await repo.bars_in_range(
                    provider="akshare",
                    adjust="qfq",
                    symbol="600000.SH",
                    interval="1d",
                    start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    end=datetime(2026, 1, 3, tzinfo=timezone.utc),
                )
                self.assertEqual([row["close"] for row in rows], [11.0])
            finally:
                await dispose_engine(engine)

    async def test_market_data_migration_runner_uses_isolated_script_location(self):
        with patch("doyoutrade.persistence.runtime_state.command.upgrade") as upgrade:
            await run_market_data_migrations(
                "postgresql+asyncpg://user:pass@localhost:5432/market"
            )

        upgrade.assert_called_once()
        config, revision = upgrade.call_args.args
        self.assertEqual(revision, "head")
        self.assertEqual(
            config.get_main_option("sqlalchemy.url"),
            "postgresql+asyncpg://user:pass@localhost:5432/market",
        )
        self.assertEqual(
            config.get_main_option("version_table"),
            "alembic_version_market_data",
        )
        self.assertTrue(
            config.get_main_option("script_location").endswith("alembic/market_data")
        )

    async def test_market_data_migration_runner_accepts_percent_encoded_url(self):
        url = "postgresql+asyncpg://user:p%40ss@localhost:5432/market"
        with patch("doyoutrade.persistence.runtime_state.command.upgrade") as upgrade:
            await run_market_data_migrations(url)

        upgrade.assert_called_once()
        config, revision = upgrade.call_args.args
        self.assertEqual(revision, "head")
        self.assertEqual(config.get_main_option("sqlalchemy.url"), url)

    async def test_market_data_migration_runner_rejects_unsupported_driver(self):
        with patch("doyoutrade.persistence.runtime_state.command.upgrade") as upgrade:
            with self.assertRaisesRegex(
                MarketDataDatabaseError, "market_data_database_url_unsupported_driver"
            ):
                await run_market_data_migrations(
                    "mysql+aiomysql://user:pass@localhost/market"
                )

        upgrade.assert_not_called()

    async def test_market_data_migration_runner_rejects_sqlite_memory(self):
        with patch("doyoutrade.persistence.runtime_state.command.upgrade") as upgrade:
            with self.assertRaisesRegex(
                MarketDataDatabaseError, "market_data_database_url_sqlite_memory"
            ):
                await run_market_data_migrations("sqlite+aiosqlite:///:memory:")

        upgrade.assert_not_called()


if __name__ == "__main__":
    unittest.main()
