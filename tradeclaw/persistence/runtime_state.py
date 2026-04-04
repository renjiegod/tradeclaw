from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

from tradeclaw.persistence.trace_store import AsyncTraceStore


def create_trace_store(trace_repository) -> AsyncTraceStore:
    return AsyncTraceStore(trace_repository)


async def run_migrations(db_url: str):
    config_path = Path(__file__).resolve().parents[2] / "alembic.ini"
    config = Config(str(config_path))
    config.set_main_option("sqlalchemy.url", db_url)
    await asyncio.to_thread(command.upgrade, config, "head")
