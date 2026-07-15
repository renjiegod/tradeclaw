"""Pin test for code_version on cycle_runs.

End-to-end check:
1. Build runtime, create definition, write strategy file to draft, finalize -> v0001.
2. Worker runs cycle: assert cycle_run.code_version == v0001, code_hash matches.
3. Simulate assistant edit: finalize second draft -> v0002 (current_version bumped).
4. CycleRunRecord for the v0001 run is unchanged.
5. New cycle picks up v0002.
6. Verify no-version error: StrategyConfigurationError with error_code="strategy_no_current_version".
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from doyoutrade.bootstrap import (
    InstanceSignalGenerator,
    StrategyConfigurationError,
    StrategyRuntimeBinding,
)
from doyoutrade.config import load_config
from doyoutrade.observability import reset_observability
from doyoutrade.persistence.repositories import SqlAlchemyCycleRunRepository
from doyoutrade.persistence.strategy_storage import StrategyStorage
from doyoutrade.runtime.cycle_task import merge_task_settings
from doyoutrade.strategy_registry import StrategyDefinitionCreate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STRATEGY_CODE = """\
from doyoutrade.strategy_sdk import Strategy, Signal


class Strategy(Strategy):
    startup_history = 1

    def on_bar(self, df, ctx):
        return Signal.hold()
"""


def _write_strategy_to_draft(storage: StrategyStorage, definition_id: str, session_id: str) -> Path:
    """Open a draft and write _STRATEGY_CODE to it."""
    draft = storage.open_draft(definition_id, session_id, base_version=None)
    (draft / "strategy.py").write_text(_STRATEGY_CODE, encoding="utf-8")
    return draft


async def _finalize_version(
    storage: StrategyStorage,
    definition_repository,
    definition_id: str,
    session_id: str,
) -> tuple[str, str]:
    """Finalize the draft and update the DB pointer. Returns (version_label, code_hash)."""
    version_label, code_hash = storage.finalize_draft(definition_id, session_id)
    await definition_repository.update_definition(
        definition_id,
        current_version=version_label,
        code_hash=code_hash,
        status="active",
    )
    return version_label, code_hash


class _FakeMarketEngine:
    async def dispose(self):
        return None


class _FakeMarketBarsRepository:
    async def bars_in_range(self, **_kwargs):
        return []

    async def upsert_bars(self, **kwargs):
        return len(kwargs["bars"])


class _FakeMarketSyncService:
    async def start(self):
        return None

    async def aclose(self):
        return None


async def _fake_market_data_runtime(*_args, **_kwargs):
    return _FakeMarketEngine(), _FakeMarketBarsRepository(), _FakeMarketSyncService()


def _market_data_config() -> str:
    return """
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/market
  default_provider: mock
  sync_on_startup: false
""".rstrip()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class CodeRootPinTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        reset_observability()

    async def test_pin_code_version_happy_path(self) -> None:
        """InstanceSignalGenerator.pin_code_version() returns current_version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StrategyStorage(Path(tmpdir) / "strategies")

            # Mock repos
            mock_defn_repo = AsyncMock()
            mock_defn_repo.get_definition.return_value = MagicMock(
                definition_id="sd-test-pin",
                current_version="v0001-abc123ef",
                code_hash="abc123ef",
                default_parameters_json={},
            )

            config = MagicMock()
            config.strategy_definition_id = "sd-test-pin"
            config.strategy_parameter_overrides = {}
            config.strategy_execution_profile = "default"

            gen = InstanceSignalGenerator(
                config=config,
                definition_repository=mock_defn_repo,
                compiler=MagicMock(),
                storage=storage,
                data_provider=None,
            )

            version_label, code_hash = await gen.pin_code_version()

            self.assertEqual(version_label, "v0001-abc123ef")
            self.assertEqual(code_hash, "abc123ef")
            self.assertEqual(gen._pinned_version, "v0001-abc123ef")
            self.assertEqual(gen._pinned_code_hash, "abc123ef")

    async def test_pin_code_version_no_current_version_raises(self) -> None:
        """pin_code_version() raises StrategyConfigurationError when no version exists."""
        mock_defn_repo = AsyncMock()
        mock_defn_repo.get_definition.return_value = MagicMock(
            definition_id="sd-unversioned",
            current_version=None,
            code_hash="",
            default_parameters_json={},
        )

        config = MagicMock()
        config.strategy_definition_id = "sd-unversioned"
        config.strategy_parameter_overrides = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StrategyStorage(Path(tmpdir) / "strategies")
            gen = InstanceSignalGenerator(
                config=config,
                definition_repository=mock_defn_repo,
                compiler=MagicMock(),
                storage=storage,
            )

        with self.assertRaises(StrategyConfigurationError) as ctx:
            await gen.pin_code_version()
        self.assertEqual(ctx.exception.error_code, "strategy_no_current_version")

    async def test_generate_intents_uses_pinned_version(self) -> None:
        """generate_intents uses _pinned_version instead of definition.current_version."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = StrategyStorage(Path(tmpdir) / "strategies")
            definition_id = "sd-pin-intents"

            # Create v0001 on disk
            _write_strategy_to_draft(storage, definition_id, "sess-v1")
            version_label, code_hash = storage.finalize_draft(definition_id, "sess-v1")

            # Simulate that current_version is now v0002 (assistant bumped it)
            mock_defn_repo = AsyncMock()
            mock_defn_repo.get_definition.return_value = MagicMock(
                definition_id=definition_id,
                # current_version is v0002, but we pinned v0001
                current_version=f"v0002-xxxxxxxx",
                code_hash="xxxxxxxx",
                default_parameters_json={},
            )

            compiler = MagicMock()
            # Mock a successful compile result
            compiled = MagicMock()
            compiled.success = True
            compiled.artifact = MagicMock()
            compiled.artifact.strategy_class = MagicMock(return_value=MagicMock(
                on_bar=MagicMock(return_value=None),
            ))
            compiled.artifact.class_name = "Strategy"
            compiler.validate_directory.return_value = compiled

            config = MagicMock()
            config.strategy_definition_id = definition_id
            config.strategy_parameter_overrides = {}
            config.strategy_execution_profile = "default"
            config.review_equity_fraction = "0.1"
            config.max_single_order_amount = None
            config.max_position_ratio = "0.5"

            gen = InstanceSignalGenerator(
                config=config,
                definition_repository=mock_defn_repo,
                compiler=compiler,
                storage=storage,
            )
            # Pre-pin to v0001
            gen._pinned_version = version_label
            gen._pinned_code_hash = code_hash

            # The compiler must be called with the v0001 path
            ctx = MagicMock()
            ctx.universe = []
            ctx.market_context = MagicMock()
            ctx.account_snapshot = MagicMock()
            ctx.positions = []
            ctx.cycle_state = MagicMock()

            # generate_intents should use v0001 path regardless of current_version=v0002.
            # We verify this by inspecting what path the compiler was called with.
            # The generator is pre-pinned to v0001, so it will reach the compiler call
            # before any signal-generation error; let unexpected exceptions propagate.
            with self.assertRaises(Exception):
                # Signal generation is expected to fail (mock strategy_class returns a
                # MagicMock, not a real Strategy), but the compiler must be called first.
                await gen.generate_intents(ctx)

            call_args = compiler.validate_directory.call_args
            self.assertIsNotNone(
                call_args,
                "compiler.validate_directory was never called — version pin did not "
                "route through the compiler",
            )
            called_path = call_args[0][0]
            self.assertIn(version_label, str(called_path))
            self.assertNotIn("v0002", str(called_path))

    async def test_cycle_run_record_has_code_version(self) -> None:
        """Full integration: cycle_runs.code_version is written at cycle-start."""
        import os
        from doyoutrade.bootstrap import build_platform_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime.db"
            config_path = Path(tmpdir) / "config.yaml"
            # Use a temp dir for DOYOUTRADE_HOME so StrategyStorage is isolated per test run
            doyoutrade_home = Path(tmpdir) / "doyoutrade_home"

            config_path.write_text(
                f"""
data:
  default_provider: mock
database:
  url: sqlite+aiosqlite:///{db_path}
{_market_data_config()}
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            # Keep DOYOUTRADE_HOME override for the entire test so storage root is isolated
            with patch.dict(os.environ, {"DOYOUTRADE_HOME": str(doyoutrade_home)}):
                with patch(
                    "doyoutrade.bootstrap._build_market_data_runtime",
                    new=_fake_market_data_runtime,
                ):
                    runtime = await build_platform_runtime(app_cfg=cfg)

                service = runtime["service"]
                route_repo = runtime["model_route_repository"]
                catalog_repo = runtime["instrument_catalog_repository"]
                strategy_registry = runtime["strategy_registry_service"]
                defn_repo = runtime["strategy_definition_repository"]
                cycle_run_repo: SqlAlchemyCycleRunRepository = runtime["cycle_run_repository"]
                # StrategyStorage is accessible via service.strategy_runtime.storage
                storage: StrategyStorage = service.strategy_runtime.storage

                # Set up model route
                await route_repo.create(
                    route_name="testp-pin",
                    provider_kind="anthropic",
                    api_key="test-pin-key",
                    target_model="claude-test",
                )
                await catalog_repo.upsert_rows(
                    [
                        {
                            "symbol": "000001.SZ",
                            "display_name": "PAB",
                            "market": "CN",
                            "instrument_type": "stock",
                            "is_tradable": True,
                            "last_sync_source": "test",
                            "last_sync_at": datetime(2026, 5, 24, 0, 0, 0),
                            "raw": {},
                        }
                    ]
                )

                # Create strategy definition (no source_code column any more)
                await strategy_registry.create_definition(
                    StrategyDefinitionCreate(
                        definition_id="sd-pin-test",
                        name="Pin Test Strategy",
                        api_version="v1",
                        parameter_schema={},
                        default_parameters={},
                        capabilities={},
                        provenance={"source": "test"},
                    )
                )

                # Write v0001 to disk and promote
                _write_strategy_to_draft(storage, "sd-pin-test", "sess-v0001")
                v1_label, v1_hash = await _finalize_version(
                    storage, defn_repo, "sd-pin-test", "sess-v0001"
                )
                self.assertTrue(v1_label.startswith("v0001-"), v1_label)

                task = await service.create_task(
                    name="pin-test-task",
                    template_id="single-agent-trend",
                    settings=merge_task_settings(
                        {
                            "model_route_name": "testp-pin",
                            "universe": ["000001.SZ"],
                            "strategy": {"definition_id": "sd-pin-test"},
                        }
                    ),
                )
                await service.start_task(task.task_id)

                # Run a cycle
                executed = await service.tick_once()
                self.assertGreaterEqual(executed, 1)

                # Verify the cycle_run record carries v0001
                runs, total = await cycle_run_repo.list_for_task(task.task_id)
                self.assertGreater(total, 0, "expected at least one cycle_run row")
                row = runs[0]
                self.assertEqual(
                    row.get("code_version"),
                    v1_label,
                    f"expected code_version={v1_label!r}, got {row.get('code_version')!r}",
                )
                self.assertEqual(
                    row.get("code_hash"),
                    v1_hash,
                    f"expected code_hash={v1_hash!r}, got {row.get('code_hash')!r}",
                )
                v1_run_id = row["run_id"]

                # Now simulate assistant edit: promote v0002
                _write_strategy_to_draft(storage, "sd-pin-test", "sess-v0002")
                v2_label, v2_hash = await _finalize_version(
                    storage, defn_repo, "sd-pin-test", "sess-v0002"
                )
                self.assertTrue(v2_label.startswith("v0002-"), v2_label)

                # Run another cycle — it should now pick up v0002
                executed2 = await service.tick_once()
                self.assertGreaterEqual(executed2, 1)

                all_runs, total2 = await cycle_run_repo.list_for_task(task.task_id)
                self.assertGreater(total2, 1, "expected two cycle_run rows after second tick")

                # Map run_id -> row
                by_run_id = {r["run_id"]: r for r in all_runs}
                # The v0001 cycle_run must be unchanged
                self.assertEqual(
                    by_run_id[v1_run_id].get("code_version"),
                    v1_label,
                    "v0001 cycle_run code_version must not change after assistant bump",
                )
                # The new cycle should carry v0002
                new_runs = [r for r in all_runs if r["run_id"] != v1_run_id]
                self.assertTrue(new_runs, "expected a second cycle_run row")
                self.assertEqual(
                    new_runs[0].get("code_version"),
                    v2_label,
                    f"new cycle should carry v2 code_version, got {new_runs[0].get('code_version')!r}",
                )

                close_runtime = runtime.get("aclose")
                if close_runtime is not None:
                    await close_runtime()

    async def test_cycle_run_code_version_none_when_no_strategy(self) -> None:
        """A worker without an InstanceSignalGenerator writes NULL code_version."""
        from doyoutrade.bootstrap import build_platform_runtime

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "runtime.db"
            config_path = Path(tmpdir) / "config.yaml"
            config_path.write_text(
                f"""
data:
  default_provider: mock
database:
  url: sqlite+aiosqlite:///{db_path}
{_market_data_config()}
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            with patch(
                "doyoutrade.bootstrap._build_market_data_runtime",
                new=_fake_market_data_runtime,
            ):
                runtime = await build_platform_runtime(app_cfg=cfg)
            cycle_run_repo: SqlAlchemyCycleRunRepository = runtime["cycle_run_repository"]

            # Create a minimal cycle run directly via the repo
            await cycle_run_repo.create_started(
                run_id="run-no-version-test",
                task_id="task-no-strat",
                agent_name="test",
                session_id=None,
                trace_id=None,
                run_mode="paper",
                run_kind="manual",
                clock_mode="wall",
                cycle_time=None,
                runtime_params=None,
                code_version=None,
                code_hash=None,
            )
            row = await cycle_run_repo.get_by_run_id("run-no-version-test")
            self.assertIsNotNone(row)
            self.assertIsNone(row.get("code_version"))
            self.assertIsNone(row.get("code_hash"))

            close_runtime = runtime.get("aclose")
            if close_runtime is not None:
                await close_runtime()
