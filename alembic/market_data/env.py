from __future__ import annotations

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from doyoutrade.persistence.db import ensure_market_data_database_url

config = context.config

target_metadata = None


def _get_db_url() -> str:
    x_args = context.get_x_argument(as_dictionary=True)
    return x_args.get("db_url") or config.get_main_option("sqlalchemy.url")


def _get_version_table() -> str:
    return config.get_main_option("version_table") or "alembic_version_market_data"


def run_migrations_offline() -> None:
    db_url = _get_db_url()
    ensure_market_data_database_url(db_url)
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        version_table=_get_version_table(),
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        version_table=_get_version_table(),
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    db_url = _get_db_url()
    ensure_market_data_database_url(db_url)
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = db_url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    import asyncio

    asyncio.run(run_migrations_online())
