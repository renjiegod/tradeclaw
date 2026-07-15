import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from doyoutrade.bootstrap import _build_worker_from_config, build_platform_runtime
from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.config import load_config
from doyoutrade.data.account_resolution import ResolvedAccount
from doyoutrade.data.cached_bars import CachedBarsDataProvider
from doyoutrade.data.local_market_bars import LocalHistoricalBarsDataProvider
from doyoutrade.execution.adapters import PaperExecutionAdapter
from doyoutrade.models.base import ModelAdapter, ModelRequest, ModelResponse
from doyoutrade.observability import reset_observability
from doyoutrade.strategy_registry import StrategyDefinitionCreate
from doyoutrade.runtime.cycle_task import CycleTaskConfig, merge_task_settings

class _BootstrapStubAdapter(ModelAdapter):
    def generate(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(text="ok")


class _FakeMarketEngine:
    def __init__(self):
        self.disposed = False

    async def dispose(self):
        self.disposed = True


class _FakeMarketBarsRepository:
    def __init__(self):
        self.upserts = []

    async def bars_in_range(self, **kwargs):
        return []

    async def upsert_bars(self, **kwargs):
        self.upserts.append(kwargs)
        return len(kwargs["bars"])


class _FakeMarketSyncService:
    def __init__(self):
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def aclose(self):
        self.closed = True


async def _fake_market_data_runtime(*_args, **_kwargs):
    return _FakeMarketEngine(), _FakeMarketBarsRepository(), _FakeMarketSyncService()


def _market_data_config(
    *, sync_on_startup: bool = False, default_provider: str = "mock"
) -> str:
    return f"""
market_data:
  database_url: postgresql+asyncpg://user:pass@localhost:5432/market
  default_provider: {default_provider}
  sync_on_startup: {str(sync_on_startup).lower()}
""".rstrip()


async def _failing_market_data_runtime(*_args, **_kwargs):
    raise RuntimeError("timescale schema validation failed")


class _FakeStrategyRuntime:
    definition_repository = object()
    compiler = object()
    storage = object()


def _resolved_mock_account() -> ResolvedAccount:
    return ResolvedAccount(
        account_id="",
        name="mock",
        mode="mock",
        base_url="",
        token=None,
        timeout_seconds=5.0,
        qmt_account_id=None,
        session_id=None,
        mock_cash=100000.0,
        mock_equity=100000.0,
    )


async def _materialize_definition_version(runtime, definition_id: str, class_name: str) -> None:
    """Write a finalized strategy version on disk for ``definition_id``.

    StrategyInstance / ``si-`` bindings were removed; a task binds a
    definition directly. The definition still needs a finalized on-disk
    version (draft → finalize → current_version) before the worker can pin
    and compile it at cycle-start.
    """

    service = runtime["service"]
    storage = service.strategy_runtime.storage
    definition_repo = runtime["strategy_definition_repository"]
    source_code = "\n".join(
        [
            "from doyoutrade.strategy_sdk import Strategy, Signal",
            "",
            f"class {class_name}(Strategy):",
            "    timeframe = \"1d\"",
            "    startup_history = 1",
            "",
            "    def on_bar(self, df, ctx):",
            "        return Signal.buy(tag=\"always_long\")",
        ]
    )
    session_id = f"sess-{definition_id}"
    draft = storage.open_draft(definition_id, session_id, base_version=None)
    (draft / "strategy.py").write_text(source_code, encoding="utf-8")
    version_label, code_hash = storage.finalize_draft(definition_id, session_id)
    await definition_repo.update_definition(
        definition_id,
        current_version=version_label,
        code_hash=code_hash,
        status="active",
    )


class BootstrapTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        reset_observability()

    async def test_timescaledb_runtime_failure_aborts_platform_startup(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
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
                new=_failing_market_data_runtime,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "timescale schema validation failed"
                ):
                    await build_platform_runtime(app_cfg=cfg)

    async def test_market_sync_starts_on_startup_and_closes_with_runtime(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
            config_path.write_text(
                f"""
data:
  default_provider: mock
database:
  url: sqlite+aiosqlite:///{db_path}
{_market_data_config(sync_on_startup=True)}
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)

            with patch(
                "doyoutrade.bootstrap._build_market_data_runtime",
                new=_fake_market_data_runtime,
            ):
                runtime = await build_platform_runtime(app_cfg=cfg)

            sync_service = runtime["market_sync_service"]
            self.assertTrue(sync_service.started)
            self.assertFalse(sync_service.closed)

            await runtime["aclose"]()
            self.assertTrue(sync_service.closed)

    def test_live_worker_wraps_local_market_bars_inside_existing_cache(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
            config_path.write_text(
                f"""
data:
  default_provider: mock
database:
  url: sqlite+aiosqlite:///{db_path}
{_market_data_config(default_provider="bootstrap-market")}
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            task_config = CycleTaskConfig(
                name="local-first-live",
                mode="live",
                data_provider=None,
                universe=("600000.SH",),
                strategy_definition_id="sd-local-first",
            )

            worker = _build_worker_from_config(
                task_config,
                shared_approval_gate=None,
                app_cfg=cfg,
                model_settings=None,
                resolved_account=_resolved_mock_account(),
                strategy_runtime=_FakeStrategyRuntime(),
                cached_bars_repository=None,
                market_bars_repository=_FakeMarketBarsRepository(),
            )

            self.assertIsInstance(worker.data_provider, CachedBarsDataProvider)
            local_provider = worker.data_provider._inner
            self.assertIsInstance(local_provider, LocalHistoricalBarsDataProvider)
            self.assertEqual(local_provider.provider, "bootstrap-market")
            self.assertEqual(local_provider.adjust, "qfq")
            self.assertIs(worker.signal_generator.data_provider, worker.data_provider)

    def test_paper_worker_wires_store_backed_ledger_into_execution_adapter(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
            config_path.write_text(
                f"""
data:
  default_provider: mock
database:
  url: sqlite+aiosqlite:///{db_path}
{_market_data_config(default_provider="bootstrap-market")}
""".strip(),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            task_config = CycleTaskConfig(
                name="paper-ledger",
                mode="paper",
                data_provider=None,
                universe=("600000.SH",),
                strategy_definition_id="sd-paper-ledger",
            )

            worker = _build_worker_from_config(
                task_config,
                shared_approval_gate=None,
                app_cfg=cfg,
                model_settings=None,
                resolved_account=_resolved_mock_account(),
                strategy_runtime=_FakeStrategyRuntime(),
                cached_bars_repository=None,
                market_bars_repository=_FakeMarketBarsRepository(),
            )

            self.assertIsInstance(worker.execution_adapter, PaperExecutionAdapter)
            self.assertIsInstance(worker.execution_adapter._ledger, MockTradingDataProvider)

    async def test_build_runtime_and_run_single_tick_with_strategy_definition(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
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
            service = runtime["service"]
            route_repo = runtime["model_route_repository"]
            catalog_repo = runtime["instrument_catalog_repository"]
            strategy_registry = runtime["strategy_registry_service"]
            await route_repo.create(
                route_name="testp-instance",
                provider_kind="anthropic",
                api_key="test-bootstrap-key",
                target_model="gpt-4o-mini",
            )
            await catalog_repo.upsert_rows(
                [
                    {
                        "symbol": "600000.SH",
                        "display_name": "PF Bank",
                        "market": "CN",
                        "instrument_type": "stock",
                        "is_tradable": True,
                        "last_sync_source": "test",
                        "last_sync_at": datetime(2026, 5, 2, 0, 0, 0),
                        "raw": {},
                    }
                ]
            )
            await strategy_registry.create_definition(
                StrategyDefinitionCreate(
                    definition_id="sd-bootstrap-instance",
                    name="Bootstrap Instance",
                    class_name="BootstrapInstanceStrategy",
                    source_code="\n".join(
                        [
                            "from doyoutrade.strategy_sdk import Strategy, Signal",
                            "",
                            "class BootstrapInstanceStrategy(Strategy):",
                            "    timeframe = \"1d\"",
                            "    startup_history = 1",
                            "",
                            "    def on_bar(self, df, ctx):",
                            "        return Signal.buy(tag=\"always_long\")",
                        ]
                    ),
                    api_version="v1",
                    parameter_schema={},
                    default_parameters={},
                    capabilities={},
                    provenance={"source": "test"},
                )
            )
            await _materialize_definition_version(
                runtime, "sd-bootstrap-instance", "BootstrapInstanceStrategy"
            )
            instance = await service.create_task(
                name="demo-instance",
                template_id="single-agent-trend",
                settings=merge_task_settings(
                    {
                        "model_route_name": "testp-instance",
                        "universe": ["600000.SH"],
                        "strategy": {"definition_id": "sd-bootstrap-instance"},
                    }
                ),
            )
            await service.start_task(instance.task_id)
            executed = await service.tick_once()

            self.assertEqual(executed, 1)
            status = await service.get_task_status(instance.task_id)
            self.assertEqual(status["status"], "running")
            self.assertEqual(status["settings"]["strategy"]["definition_id"], "sd-bootstrap-instance")

            close_runtime = runtime.get("aclose")
            if close_runtime is not None:
                await close_runtime()

    async def test_assistant_recording_adapter_uses_route_name_as_provider(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
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
            route_repo = runtime["model_route_repository"]
            await route_repo.create(
                route_name="assistant-route",
                provider_kind="anthropic",
                api_key="test-bootstrap-key",
                target_model="claude-route-model",
            )

            with patch("doyoutrade.bootstrap.build_model_adapter", return_value=_BootstrapStubAdapter()):
                adapter = await runtime["assistant_service"].model_adapter_factory("assistant-route")

            self.assertEqual(adapter._provider, "assistant-route")
            self.assertEqual(adapter._model, "claude-route-model")

            close_runtime = runtime.get("aclose")
            if close_runtime is not None:
                await close_runtime()

    async def test_build_runtime_can_skip_channel_startup_for_cli(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
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

            with patch("doyoutrade.assistant.channels.manager.ChannelManager.start_all") as start_all:
                with patch(
                    "doyoutrade.bootstrap._build_market_data_runtime",
                    new=_fake_market_data_runtime,
                ):
                    runtime = await build_platform_runtime(
                        app_cfg=cfg,
                        start_channels=False,
                    )
                await asyncio.sleep(0)

            start_all.assert_not_called()

            close_runtime = runtime.get("aclose")
            if close_runtime is not None:
                await close_runtime()

    async def test_runtime_restores_running_tasks_on_rebuild(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
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
            service = runtime["service"]
            route_repo = runtime["model_route_repository"]
            await route_repo.create(
                route_name="testp",
                provider_kind="anthropic",
                api_key="test-bootstrap-key",
                target_model="gpt-4o-mini",
            )
            catalog_repo = runtime["instrument_catalog_repository"]
            strategy_registry = runtime["strategy_registry_service"]
            await catalog_repo.upsert_rows(
                [
                    {
                        "symbol": "600000.SH",
                        "display_name": "PF Bank",
                        "market": "CN",
                        "instrument_type": "stock",
                        "is_tradable": True,
                        "last_sync_source": "test",
                        "last_sync_at": datetime(2026, 5, 2, 0, 0, 0),
                        "raw": {},
                    }
                ]
            )
            await strategy_registry.create_definition(
                StrategyDefinitionCreate(
                    definition_id="sd-bootstrap-rebuild",
                    name="Bootstrap Rebuild",
                    class_name="BootstrapRebuildStrategy",
                    source_code="\n".join(
                        [
                            "from doyoutrade.strategy_sdk import Strategy, Signal",
                            "",
                            "class BootstrapRebuildStrategy(Strategy):",
                            "    timeframe = \"1d\"",
                            "    startup_history = 1",
                            "",
                            "    def on_bar(self, df, ctx):",
                            "        return Signal.buy(tag=\"always_long\")",
                        ]
                    ),
                    api_version="v1",
                    parameter_schema={},
                    default_parameters={},
                    capabilities={},
                    provenance={"source": "test"},
                )
            )
            await _materialize_definition_version(
                runtime, "sd-bootstrap-rebuild", "BootstrapRebuildStrategy"
            )
            instance = await service.create_task(
                name="demo",
                template_id="single-agent-trend",
                settings=merge_task_settings(
                    {
                        "model_route_name": "testp",
                        "universe": ["600000.SH"],
                        "strategy": {"definition_id": "sd-bootstrap-rebuild"},
                    }
                ),
            )
            await service.start_task(instance.task_id)

            close_runtime = runtime.get("aclose")
            if close_runtime is not None:
                await close_runtime()

            with patch(
                "doyoutrade.bootstrap._build_market_data_runtime",
                new=_fake_market_data_runtime,
            ):
                rebuilt = await build_platform_runtime(app_cfg=cfg)
            rebuilt_service = rebuilt["service"]
            executed = await rebuilt_service.tick_once()
            status = await rebuilt_service.get_task_status(instance.task_id)

            self.assertEqual(executed, 1)
            self.assertEqual(status["status"], "running")

            rebuilt_close = rebuilt.get("aclose")
            if rebuilt_close is not None:
                await rebuilt_close()

    async def test_load_config_rejects_yaml_providers(self):
        with tempfile.TemporaryDirectory() as tempdir:
            config_path = Path(tempdir) / "config.yaml"
            db_path = Path(tempdir) / "runtime.db"
            config_path.write_text(
                f"""
providers:
  - name: testp
    provider_type: anthropic
    api_key: null
    model: gpt-4o-mini
model:
  provider: testp
database:
  url: sqlite+aiosqlite:///{db_path}
""".strip(),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "providers"):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
