#!/usr/bin/env python3
"""Migrate legacy factor strategy module paths in task settings.

This script rewrites:
    doyoutrade.strategies.signal...
to:
    doyoutrade.strategies.factor...

Targets:
- settings["factor_strategy_class"]
- settings["factor"]["strategy_class"]
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from doyoutrade.config import get_config
from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.models import Task

LEGACY_PREFIX = "doyoutrade.strategies.signal"
CURRENT_PREFIX = "doyoutrade.strategies.factor"


def _migrate_value(raw: Any) -> tuple[Any, bool]:
    if not isinstance(raw, str):
        return raw, False
    if raw == LEGACY_PREFIX or raw.startswith(f"{LEGACY_PREFIX}."):
        return raw.replace(LEGACY_PREFIX, CURRENT_PREFIX, 1), True
    return raw, False


def _migrate_settings(settings: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    out = dict(settings)
    changed = False

    migrated, touched = _migrate_value(out.get("factor_strategy_class"))
    if touched:
        out["factor_strategy_class"] = migrated
        changed = True

    factor_block = out.get("factor")
    if isinstance(factor_block, dict):
        factor_copy = dict(factor_block)
        migrated_nested, touched_nested = _migrate_value(factor_copy.get("strategy_class"))
        if touched_nested:
            factor_copy["strategy_class"] = migrated_nested
            out["factor"] = factor_copy
            changed = True

    return out, changed


@dataclass
class MigrationResult:
    scanned: int = 0
    changed: int = 0


async def _run(*, apply: bool) -> MigrationResult:
    cfg = get_config()
    engine, session_factory = create_engine_and_session_factory(
        cfg.database.url,
        echo=cfg.database.echo,
        pool_pre_ping=cfg.database.pool_pre_ping,
    )
    result = MigrationResult()
    try:
        async with session_factory() as session:
            rows = await session.scalars(select(Task))
            for task in rows:
                result.scanned += 1
                if not isinstance(task.settings, dict):
                    continue
                new_settings, changed = _migrate_settings(task.settings)
                if not changed:
                    continue
                result.changed += 1
                print(f"{task.task_id} | {task.name}")
                print(f"  factor_strategy_class -> {new_settings.get('factor_strategy_class')!r}")
                if isinstance(new_settings.get("factor"), dict):
                    print(
                        "  factor.strategy_class -> "
                        f"{new_settings['factor'].get('strategy_class')!r}"
                    )
                if apply:
                    task.settings = new_settings
            if apply and result.changed > 0:
                await session.commit()
    finally:
        await dispose_engine(engine)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate legacy factor strategy import paths in tasks.settings.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist changes to database (default is dry-run).",
    )
    args = parser.parse_args()
    result = asyncio.run(_run(apply=args.apply))
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] scanned={result.scanned} changed={result.changed}")


if __name__ == "__main__":
    main()
