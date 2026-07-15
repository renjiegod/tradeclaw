"""Live-mode approval queue against an isolated DB (not the dev ``doyoutrade`` database).

By default the test uses a fresh SQLite file under ``tempfile.TemporaryDirectory`` so
``restore_instances`` does not scan a shared Postgres full of historical rows.

To exercise Postgres instead, create an empty database (e.g. ``doyoutrade_test``) and set::

    export DOYOUTRADE_TEST_DATABASE_URL='postgresql+asyncpg://user:pass@localhost:5432/doyoutrade_test'

Alembic will migrate that URL on ``build_platform_runtime`` the same as production.

This module uses ``asyncio.run`` inside a plain ``unittest.TestCase`` (not
``IsolatedAsyncioTestCase``): combining ``@patch`` on ``build_model_adapter`` with
``IsolatedAsyncioTestCase`` and ``asyncio.to_thread(alembic upgrade)`` + aiosqlite has
been observed to deadlock after ``build_platform_runtime`` creates repositories.

For ordering diagnostics on stalls, set ``DOYOUTRADE_RUNTIME_DIAG=1`` (see
``doyoutrade.diagnostics.runtime_diag``).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import uuid
from datetime import date
from pathlib import Path

from doyoutrade.bootstrap import build_platform_runtime
from doyoutrade.config import load_config
from doyoutrade.data.mock_provider import StaticUniverseProvider
from doyoutrade.observability import reset_observability
from doyoutrade.runtime.cycle_task import merge_task_settings
from doyoutrade.strategy_registry import StrategyDefinitionCreate


def _instance_settings(model_route_name: str) -> dict:
    return merge_task_settings(
        {
            "model_route_name": model_route_name,
            "strategy": {"definition_id": "sd-shared-approval"},
            "universe": ["600000.SH"],
            "position_constraints": {
                "max_single_order_amount": 2000,
                "review_equity_fraction": 0.05,
            },
            "approval": {
                "min_notional_for_approval": 1000,
                "timeout_seconds": 300,
            },
        }
    )


class SharedApprovalRuntimeTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_observability()

    def test_live_instance_uses_shared_approval_queue(self) -> None:
        async def _run() -> None:
            with tempfile.TemporaryDirectory() as tempdir:
                base = Path(tempdir)
                config_path = base / "config.yaml"
                db_path = base / "shared_approval_test.db"
                db_url = os.environ.get("DOYOUTRADE_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{db_path}"
                config_path.write_text(
                    f"""
data:
  default_provider: mock
database:
  url: {json.dumps(db_url)}
observability:
  tracing_enabled: false
""".strip(),
                    encoding="utf-8",
                )
                runtime = None
                try:
                    cfg = load_config(config_path)
                    runtime = await build_platform_runtime(app_cfg=cfg)
                    service = runtime["service"]
                    approval_gate = runtime["approval_gate"]
                    await service.instrument_catalog_repository.upsert_rows([
                        {
                            "symbol": "600000.SH",
                            "display_name": "600000.SH",
                            "market": "SH",
                            "instrument_type": "stock",
                            "is_tradable": True,
                            "last_sync_source": "test",
                            "raw": {"source": "test_shared_approval_runtime"},
                        }
                    ])
                    route_repo = runtime["model_route_repository"]
                    await route_repo.create(
                        route_name="shared-approval-route",
                        provider_kind="anthropic",
                        api_key="test-shared-approval-key",
                        target_model="gpt-4o-mini",
                    )
                    strategy_registry = runtime["strategy_registry_service"]
                    definition_repo = runtime["strategy_definition_repository"]
                    storage = runtime["strategy_storage"]
                    source_code = "\n".join(
                        [
                            "from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal",
                            "",
                            "class Strategy(BaseStrategy):",
                            "    timeframe = \"1d\"",
                            "    startup_history = 1",
                            "",
                            "    def on_bar(self, df, ctx):",
                            "        return Signal.buy(tag=\"always_long\")",
                        ]
                    )
                    await strategy_registry.create_definition(
                        StrategyDefinitionCreate(
                            definition_id="sd-shared-approval",
                            name="Shared Approval Strategy",
                            class_name="Strategy",
                            source_code=source_code,
                            api_version="v1",
                            parameter_schema={},
                            default_parameters={},
                            capabilities={},
                            provenance={"source": "test"},
                        )
                    )
                    draft = storage.open_draft("sd-shared-approval", "sess-shared-approval", base_version=None)
                    (draft / "strategy.py").write_text(source_code, encoding="utf-8")
                    version_label, code_hash = storage.finalize_draft(
                        "sd-shared-approval",
                        "sess-shared-approval",
                    )
                    await definition_repo.update_definition(
                        "sd-shared-approval",
                        current_version=version_label,
                        code_hash=code_hash,
                        status="active",
                    )
                    # A live task now requires a resolvable account; create a
                    # default mock account (no live terminal needed for the test).
                    account_repo = runtime["account_repository"]
                    acct = await account_repo.upsert_account(
                        {"name": "shared-approval-acct", "mode": "mock", "base_url": ""}
                    )
                    await account_repo.set_default(acct["id"])
                    instance = await service.create_task(
                        name=f"live-alpha-{uuid.uuid4().hex[:8]}",
                        mode="live",
                        settings=_instance_settings("shared-approval-route"),
                    )
                    await service.start_task(instance.task_id)
                    live = service.tasks[instance.task_id]
                    if hasattr(live.worker, "universe_provider"):
                        live.worker.universe_provider = StaticUniverseProvider(["600000.SH"])
                    # Seed at least one bar so the new Strategy runner's
                    # base-bar window is satisfied (startup_history=1).
                    # The old SignalEngine ignored data_map, so this seeding
                    # wasn't needed; the new Strategy API uses bars even when
                    # on_bar doesn't inspect df contents.
                    from doyoutrade.core.models import Bar as _Bar
                    from doyoutrade.data.cached_bars import CachedBarsDataProvider

                    seeded_provider = live.worker.data_provider
                    if isinstance(seeded_provider, CachedBarsDataProvider):
                        seeded_provider = seeded_provider._inner
                    bars_store = getattr(seeded_provider, "_bars_store", None)
                    if bars_store is not None:
                        bars_store._bars_by_symbol["600000.SH"] = [
                            _Bar(
                                symbol="600000.SH",
                                timestamp=date.today().isoformat(),
                                open=10.0,
                                high=10.5,
                                low=9.5,
                                close=10.0,
                                volume=100000.0,
                            )
                        ]
                    price_map = getattr(seeded_provider, "_symbol_to_price", None)
                    if isinstance(price_map, dict):
                        price_map["600000.SH"] = 10.0
                    await service.tick_once()

                    pending = await approval_gate.list_pending()
                    self.assertGreaterEqual(len(pending), 1)
                    self.assertEqual(pending[0].mode, "live")
                finally:
                    if runtime is not None:
                        aclose = runtime.get("aclose")
                        if aclose is not None:
                            await aclose()
                    reset_observability()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
