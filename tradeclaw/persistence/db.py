from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def create_engine_and_session_factory(
    url: str,
    echo: bool = False,
    pool_pre_ping: bool = True,
):
    engine = create_async_engine(url, echo=echo, pool_pre_ping=pool_pre_ping)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, session_factory


async def dispose_engine(engine: AsyncEngine | None):
    if engine is not None:
        await engine.dispose()
