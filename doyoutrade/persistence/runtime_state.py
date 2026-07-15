from __future__ import annotations

import asyncio
import importlib.resources
from pathlib import Path

from alembic import command
from alembic.config import Config

from doyoutrade.persistence.db import (
    ensure_market_data_database_url,
    ensure_sqlite_parent_directory,
)


def _escape_alembic_config_value(value: str) -> str:
    return value.replace("%", "%%")


def _alembic_base() -> Path:
    """Directory that holds ``alembic.ini`` and the ``alembic/`` script tree.

    Source checkout: the repo root (two levels above this file). Installed
    wheel: the copy force-included at ``doyoutrade/_migrations/`` (the repo root
    does not exist there). Fail loudly if neither is present rather than let
    Alembic silently pick up an empty script location and stamp head with no
    tables created.
    """

    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "alembic.ini").is_file() and (repo_root / "alembic").is_dir():
        return repo_root
    packaged = Path(str(importlib.resources.files("doyoutrade"))) / "_migrations"
    if (packaged / "alembic.ini").is_file() and (packaged / "alembic").is_dir():
        return packaged
    raise RuntimeError(
        "alembic migrations not found: neither the repo-root alembic/ tree nor "
        f"the packaged doyoutrade/_migrations/ copy is present (looked in "
        f"{repo_root} and {packaged}). Reinstall the package or run from a full checkout."
    )


async def run_migrations(db_url: str):
    from doyoutrade.observability.logging import configure_logging

    configure_logging()
    ensure_sqlite_parent_directory(db_url)
    base = _alembic_base()
    config = Config(str(base / "alembic.ini"))
    # Absolute script_location so migrations resolve regardless of CWD (a wheel
    # install runs from an arbitrary directory, not the repo root).
    config.set_main_option("script_location", str(base / "alembic"))
    config.set_main_option("sqlalchemy.url", db_url)
    await asyncio.to_thread(command.upgrade, config, "head")


async def run_market_data_migrations(market_db_url: str):
    from doyoutrade.observability.logging import configure_logging

    configure_logging()
    ensure_market_data_database_url(market_db_url)
    ensure_sqlite_parent_directory(market_db_url)
    base = _alembic_base()
    config = Config(str(base / "alembic.ini"))
    config.set_main_option("script_location", str(base / "alembic" / "market_data"))
    config.set_main_option(
        "sqlalchemy.url", _escape_alembic_config_value(market_db_url)
    )
    config.set_main_option("version_table", "alembic_version_market_data")
    await asyncio.to_thread(command.upgrade, config, "head")
