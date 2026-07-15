from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import ArgumentError
from sqlalchemy.engine import URL
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool


class Base(DeclarativeBase):
    pass


class MarketDataDatabaseError(RuntimeError):
    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        super().__init__(f"{error_code}: {message}")


def ensure_sqlite_parent_directory(url: str) -> None:
    """Create parent directories for on-disk SQLite files so migrations can run."""
    parsed = make_url(url)
    if "sqlite" not in (parsed.drivername or ""):
        return
    db = parsed.database
    if not db or db == ":memory:":
        return
    path = Path(db)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)


MARKET_DATA_SUPPORTED_DRIVERS = ("postgresql+asyncpg", "sqlite+aiosqlite")


def ensure_market_data_database_url(url: str) -> URL:
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise MarketDataDatabaseError(
            "market_data_database_url_invalid",
            f"market data database URL is invalid: {exc}",
        ) from exc

    if parsed.drivername not in MARKET_DATA_SUPPORTED_DRIVERS:
        raise MarketDataDatabaseError(
            "market_data_database_url_unsupported_driver",
            "market_data.database_url must use one of "
            f"{', '.join(MARKET_DATA_SUPPORTED_DRIVERS)}; got {parsed.drivername!r}",
        )
    if parsed.drivername == "sqlite+aiosqlite":
        # NullPool gives every session its own connection, so an in-memory
        # SQLite market store would silently start empty on each checkout.
        if not parsed.database or parsed.database == ":memory:":
            raise MarketDataDatabaseError(
                "market_data_database_url_sqlite_memory",
                "market_data.database_url with sqlite+aiosqlite must point to a "
                f"file path, not memory; got {url!r}",
            )
        return parsed
    if not parsed.database:
        raise MarketDataDatabaseError(
            "market_data_database_url_missing_database",
            "market_data.database_url must include a database name",
        )
    return parsed


async def verify_timescaledb_market_schema(conn) -> None:
    ext_version = await conn.scalar(
        text("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
    )
    if not ext_version:
        raise MarketDataDatabaseError(
            "timescaledb_extension_unavailable",
            "TimescaleDB extension is not installed in the market data database",
        )

    hypertable_exists = await conn.scalar(
        text(
            """
            SELECT 1
            FROM timescaledb_information.hypertables
            WHERE hypertable_schema = current_schema()
              AND hypertable_name = 'market_bars'
            LIMIT 1
            """
        )
    )
    if not hypertable_exists:
        raise MarketDataDatabaseError(
            "market_bars_hypertable_unavailable",
            "market_bars TimescaleDB hypertable is not available",
        )

    sync_state_exists = await conn.scalar(
        text(
            """
            SELECT 1
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relname = 'market_bar_sync_state'
              AND c.relkind IN ('r', 'p')
            LIMIT 1
            """
        )
    )
    if not sync_state_exists:
        raise MarketDataDatabaseError(
            "market_bar_sync_state_unavailable",
            "market_bar_sync_state table is not available",
        )


async def verify_sqlite_market_schema(conn) -> None:
    bars_exists = await conn.scalar(
        text(
            "SELECT 1 FROM sqlite_master"
            " WHERE type = 'table' AND name = 'market_bars' LIMIT 1"
        )
    )
    if not bars_exists:
        raise MarketDataDatabaseError(
            "market_bars_table_unavailable",
            "market_bars table is not available in the SQLite market data database",
        )

    sync_state_exists = await conn.scalar(
        text(
            "SELECT 1 FROM sqlite_master"
            " WHERE type = 'table' AND name = 'market_bar_sync_state' LIMIT 1"
        )
    )
    if not sync_state_exists:
        raise MarketDataDatabaseError(
            "market_bar_sync_state_unavailable",
            "market_bar_sync_state table is not available",
        )


async def verify_market_schema(conn, *, drivername: str) -> None:
    if drivername == "postgresql+asyncpg":
        await verify_timescaledb_market_schema(conn)
        return
    if drivername == "sqlite+aiosqlite":
        await verify_sqlite_market_schema(conn)
        return
    raise MarketDataDatabaseError(
        "market_data_database_url_unsupported_driver",
        "market data schema verification supports "
        f"{', '.join(MARKET_DATA_SUPPORTED_DRIVERS)}; got {drivername!r}",
    )


def create_engine_and_session_factory(
    url: str,
    echo: bool = False,
    pool_pre_ping: bool = True,
):
    import os

    from doyoutrade.diagnostics import runtime_diag

    parsed = make_url(url)
    # aiosqlite + default QueuePool: multiple pooled connections to one SQLite file can
    # stall checkout (e.g. another connection holds a lock). NullPool matches SQLite usage.
    pool_kw: dict = {}
    if "sqlite" in (parsed.drivername or ""):
        pool_kw["poolclass"] = NullPool
    engine = create_async_engine(url, echo=echo, pool_pre_ping=pool_pre_ping, **pool_kw)
    if os.environ.get("DOYOUTRADE_RUNTIME_DIAG") == "1":
        runtime_diag(
            f"create_engine: driver={parsed.drivername!r} pool={type(engine.pool).__name__}"
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory


async def dispose_engine(engine: AsyncEngine | None):
    if engine is not None:
        await engine.dispose()
