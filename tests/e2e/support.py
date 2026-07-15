from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import yaml

from doyoutrade.bootstrap import build_platform_runtime
from doyoutrade.config import AppConfig, load_config
from doyoutrade.models.base import ModelRequest, ModelResponse
from doyoutrade.observability import reset_observability
from doyoutrade.persistence.errors import RecordNotFoundError
from doyoutrade.runtime.cycle_task import merge_task_settings
from doyoutrade.strategy_registry import StrategyDefinitionCreate


class E2EModelMode(StrEnum):
    STUB = "stub"
    REAL = "real"


TERMINAL_SESSION_STATUSES = frozenset({"completed", "finished", "failed"})


def e2e_enabled() -> bool:
    return os.environ.get("DOYOUTRADE_E2E", "").strip().lower() in {"1", "true", "yes", "on"}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_e2e_config_path() -> Path:
    return repo_root() / "tests" / "e2e" / "config.yaml"


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"E2E config root must be a mapping: {path}")
    return value


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _profile_overrides(profile: str, tempdir: Path) -> dict[str, Any]:
    normalized = profile.strip().lower() or "local"
    if normalized == "isolated":
        return {
            "data": {
                "default_provider": "mock",
                "qmt": {
                    "account_mode": "mock",
                    "base_url": None,
                    "token": None,
                    "session_id": None,
                    "account_id": None,
                    "mock_account": {
                        "cash": 100000,
                        "equity": 100000,
                        "positions": [
                            {
                                "symbol": "600000.SH",
                                "quantity": 0,
                                "cost_price": 0,
                            },
                        ],
                    },
                },
            },
            "database": {
                "url": f"sqlite+aiosqlite:///{tempdir / 'doyoutrade-e2e.db'}",
                "echo": False,
                "pool_pre_ping": True,
            },
            "market_data": {
                "database_url": f"sqlite+aiosqlite:///{tempdir / 'doyoutrade-e2e-market.db'}",
                "default_provider": "mock",
                "sync_on_startup": False,
            },
            "observability": {
                "tracing_enabled": True,
                "console_enabled": False,
            },
        }
    if normalized == "local":
        return {
            "data": {
                "default_provider": "mock",
                "qmt": {
                    "account_mode": "mock",
                    "base_url": None,
                    "token": None,
                    "session_id": None,
                    "account_id": None,
                    "mock_account": {
                        "cash": 100000,
                        "equity": 100000,
                        "positions": [
                            {
                                "symbol": "600000.SH",
                                "quantity": 0,
                                "cost_price": 0,
                            },
                        ],
                    },
                },
            },
            "observability": {
                "tracing_enabled": True,
                "console_enabled": False,
            },
        }
    if normalized == "live":
        return {}
    raise ValueError(f"unknown DOYOUTRADE_E2E_PROFILE: {profile!r}")


def _strip_e2e_section(raw: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in raw.items() if key != "e2e"}


def _resolve_config_overlay_path() -> Path | None:
    raw = os.environ.get("DOYOUTRADE_E2E_CONFIG", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    path = default_e2e_config_path()
    return path if path.is_file() else None


def _strategy_class_name(prefix: str) -> str:
    parts = [chunk for chunk in prefix.replace("-", "_").split("_") if chunk]
    return "".join(part[:1].upper() + part[1:] for part in parts) + "InstanceStrategy"


@dataclass(frozen=True)
class E2EConfigBundle:
    app_config: AppConfig
    root_config_path: Path
    merged_config_path: Path
    e2e_settings: dict[str, Any]


def load_e2e_config(*, profile: str | None = None, tempdir: Path | None = None) -> E2EConfigBundle:
    base_dir = tempdir or Path(tempfile.mkdtemp(prefix="doyoutrade-e2e-config-"))
    base_dir.mkdir(parents=True, exist_ok=True)
    root_config = repo_root() / "config.yaml"
    if not root_config.is_file():
        raise FileNotFoundError(f"repo-root config.yaml not found: {root_config}")

    root_raw = _load_yaml_mapping(root_config)
    overlay_path = _resolve_config_overlay_path()
    overlay_raw = _load_yaml_mapping(overlay_path) if overlay_path is not None else {}
    selected_profile = profile or os.environ.get("DOYOUTRADE_E2E_PROFILE", "local")
    merged_raw = _deep_merge(root_raw, _strip_e2e_section(overlay_raw))
    merged_raw = _deep_merge(merged_raw, _profile_overrides(selected_profile, base_dir))

    merged_path = base_dir / "config.yaml"
    with merged_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(merged_raw, handle, sort_keys=False, allow_unicode=True)
    return E2EConfigBundle(
        app_config=load_config(merged_path),
        root_config_path=root_config,
        merged_config_path=merged_path,
        e2e_settings=dict(overlay_raw.get("e2e") or {}) if isinstance(overlay_raw.get("e2e"), dict) else {},
    )


class StubModelAdapter:
    _native_tool_provider = "anthropic"

    def generate(self, _request: ModelRequest) -> ModelResponse:
        return self._response()

    async def chat_ainvoke(self, _messages: list[Any], *, tools: list[dict[str, Any]] | None = None) -> ModelResponse:
        return self._response()

    def _response(self) -> ModelResponse:
        text = (
            '{"proposals": [{"symbol": "600000.SH", "side": "long", '
            '"strategy_tag": "e2e-stub", "rationale": "E2E deterministic signal"}]}'
        )
        return ModelResponse(
            text=text,
            raw=SimpleNamespace(
                content=text,
                tool_calls=[],
                usage_metadata={
                    "input_tokens": 10,
                    "output_tokens": 8,
                    "total_tokens": 18,
                },
                response_metadata={"time_to_first_token_ms": 1},
            ),
        )


@dataclass
class E2ERuntimeContext:
    app_config: AppConfig
    root_config_path: Path
    merged_config_path: Path
    e2e_settings: dict[str, Any]
    runtime: dict[str, Any]
    model_route_name: str
    created_task_ids: set[str]

    @property
    def service(self):
        return self.runtime["service"]

    async def create_agent_task(
        self,
        *,
        mode: str,
        status: str = "configured",
        data_provider: str | None = None,
    ):
        task = await self._create_definition_task(
            mode=mode,
            name_prefix="e2e-agent",
            data_provider=data_provider,
        )
        self.created_task_ids.add(task.task_id)
        if status == "running":
            await self.service.start_task(task.task_id)
        elif status != "configured":
            raise ValueError(f"unsupported E2E task status: {status}")
        return task

    async def _create_definition_task(
        self,
        *,
        mode: str,
        name_prefix: str,
        data_provider: str | None = None,
        universe_override: list[str] | None = None,
    ):
        task_cfg = dict(self.e2e_settings.get("task") or {}) if isinstance(self.e2e_settings.get("task"), dict) else {}
        raw_symbols = self.e2e_settings.get("symbols")
        symbols = (
            [str(item).strip() for item in raw_symbols if str(item).strip()]
            if isinstance(raw_symbols, list) and raw_symbols
            else ["600000.SH"]
        )
        symbol = symbols[0] if symbols else "600000.SH"
        # The persisted universe may be a watchlist-tag reference (e.g.
        # ["@watchlist:核心池"]); it is resolved to concrete symbols at worker
        # assembly. The strategy source still keys off ``symbol`` so the seeded
        # watchlist symbol triggers the buy branch.
        task_universe = universe_override if universe_override is not None else symbols
        strategy_registry = self.runtime["strategy_registry_service"]
        definition_repo = self.runtime["strategy_definition_repository"]
        storage = self.runtime["strategy_storage"]
        definition_id = f"sd-{name_prefix}-{uuid.uuid4().hex[:8]}"
        source_code = "\n".join(
            [
                "from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal",
                "",
                "class Strategy(BaseStrategy):",
                "    timeframe = \"1d\"",
                "    startup_history = 1",
                "",
                "    def on_bar(self, df, ctx):",
                f"        if ctx.symbol == '{symbol}' or '{symbol}' in ctx.universe:",
                "            return Signal.buy(tag=\"e2e_target_symbol\")",
                "        return Signal.buy(tag=\"e2e_universe_fallback\")",
            ]
        )
        await strategy_registry.create_definition(
            StrategyDefinitionCreate(
                definition_id=definition_id,
                name=f"{name_prefix.title()} Strategy",
                class_name="Strategy",
                source_code=source_code,
                api_version="v1",
                parameter_schema={},
                default_parameters={},
                capabilities={},
                provenance={"source": "e2e"},
            )
        )
        session_id = f"sess-{name_prefix}-{uuid.uuid4().hex[:8]}"
        draft = storage.open_draft(definition_id, session_id, base_version=None)
        (draft / "strategy.py").write_text(source_code, encoding="utf-8")
        version_label, code_hash = storage.finalize_draft(definition_id, session_id)
        await definition_repo.update_definition(
            definition_id,
            current_version=version_label,
            code_hash=code_hash,
            status="active",
        )
        effective_data_provider = data_provider or str(task_cfg.get("data_provider") or "mock")
        return await self.service.create_task(
            name=f"{name_prefix}-{uuid.uuid4().hex[:8]}",
            mode=mode,
            data_provider=effective_data_provider,
            settings=merge_task_settings(
                {
                    "model_route_name": self.model_route_name,
                    "universe": task_universe,
                    "strategy": {"definition_id": definition_id},
                }
            ),
        )

    async def create_definition_backtest_task(
        self,
        *,
        data_provider: str | None = None,
        universe_override: list[str] | None = None,
    ):
        task = await self._create_definition_task(
            mode="backtest",
            name_prefix="e2e-definition-backtest",
            data_provider=data_provider,
            universe_override=universe_override,
        )
        self.created_task_ids.add(task.task_id)
        return task

    async def start_backtest_and_wait(
        self,
        task_id: str,
        *,
        range_start: str = "2026-01-01",
        range_end: str = "2026-01-10",
        market_profile: str | None = None,
        bar_interval: str | None = None,
        timeout_seconds: float = 30.0,
        debug_enabled: bool = True,
    ):
        """Start a backtest job and wait for it to complete."""
        run_row = await self.service.start_backtest_job(
            identifier=task_id,
            range_start=range_start,
            range_end=range_end,
            market_profile=market_profile,
            bar_interval=bar_interval,
            debug_enabled=debug_enabled,
        )
        job_id = run_row["run_id"]
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            run = await self.service.get_backtest_job(task_id, job_id)
            status = run.get("status", "")
            if status in TERMINAL_SESSION_STATUSES:
                await wait_for_model_invocation_tasks()
                return run
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"backtest job did not finish in time: {job_id}")
            await asyncio.sleep(0.2)

    async def list_cycle_runs(self, task_id: str, **filters):
        await wait_for_model_invocation_tasks()
        return await self.service.list_cycle_runs(task_id, **filters)

    async def stop_agent_task(self, task_id: str) -> None:
        """Stop a live/trading agent task by task_id."""
        await self.service.stop_task(task_id)

    async def stop_backtest_job(self, task_id: str, run_id: str) -> dict[str, Any] | None:
        """Stop a backtest job by task_id and run_id. Returns None if already finished."""
        try:
            return await self.service.stop_backtest_job(task_id, run_id)
        except RuntimeError as e:
            if "already finished" in str(e):
                return None
            raise


class E2ERuntimeManager(AbstractAsyncContextManager[E2ERuntimeContext]):
    def __init__(self, *, profile: str | None, model_mode: E2EModelMode):
        self.profile = profile
        self.model_mode = model_mode
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._patcher = None
        self._home_patcher = None
        self._runtime: dict[str, Any] | None = None
        self._context: E2ERuntimeContext | None = None

    async def __aenter__(self) -> E2ERuntimeContext:
        try:
            self._tempdir = tempfile.TemporaryDirectory(prefix="doyoutrade-e2e-")
            tempdir_name = str(self._tempdir.name)
            bundle = load_e2e_config(profile=self.profile, tempdir=Path(tempdir_name))
            if self.model_mode == E2EModelMode.STUB:
                self._patcher = patch("doyoutrade.bootstrap.build_model_adapter", return_value=StubModelAdapter())
                self._patcher.start()
            # The isolated profile points market_data.database_url at a
            # tempdir SQLite file, so the real market-data runtime (migrations,
            # schema verification, repository) runs end-to-end without
            # requiring PostgreSQL/TimescaleDB.
            # Redirect DOYOUTRADE_HOME to the tempdir so that StrategyStorage
            # gets a fresh root on every test run instead of sharing the
            # developer's ~/.doyoutrade/strategies directory.
            self._home_patcher = patch.dict(os.environ, {"DOYOUTRADE_HOME": tempdir_name})
            self._home_patcher.start()
            self._runtime = await build_platform_runtime(app_cfg=bundle.app_config)
            # Accounts now live in the DB (config.data.qmt was removed). Seed a
            # default mock account so live-mode tasks resolve an account; the
            # isolated profile uses the mock data provider, so no real terminal.
            account_repo = self._runtime.get("account_repository")
            if account_repo is not None:
                acct = await account_repo.upsert_account(
                    {"name": "e2e-default", "mode": "mock", "base_url": ""}
                )
                await account_repo.set_default(acct["id"])
            await seed_e2e_instrument_catalog(self._runtime, bundle.e2e_settings)
            route_name = await ensure_e2e_model_route(self._runtime, bundle.e2e_settings, self.model_mode)
            self._context = E2ERuntimeContext(
                app_config=bundle.app_config,
                root_config_path=bundle.root_config_path,
                merged_config_path=bundle.merged_config_path,
                e2e_settings=bundle.e2e_settings,
                runtime=self._runtime,
                model_route_name=route_name,
                created_task_ids=set(),
            )
            return self._context
        except Exception:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if self._runtime is not None:
                service = self._runtime.get("service")
                if service is not None and hasattr(service, "delete_task"):
                    tracked_task_ids = list(self._context.created_task_ids) if self._context is not None else []
                    for task_id in tracked_task_ids:
                        try:
                            await service.delete_task(task_id)
                        except RecordNotFoundError:
                            continue
                aclose = self._runtime.get("aclose")
                if aclose is not None:
                    await aclose()
        finally:
            if self._patcher is not None:
                self._patcher.stop()
            if self._home_patcher is not None:
                self._home_patcher.stop()
            reset_observability()
            if self._tempdir is not None:
                self._tempdir.cleanup()
            self._context = None


def build_e2e_runtime(
    *,
    profile: str | None = None,
    model_mode: E2EModelMode = E2EModelMode.STUB,
) -> E2ERuntimeManager:
    return E2ERuntimeManager(profile=profile, model_mode=model_mode)


async def ensure_e2e_model_route(
    runtime: dict[str, Any],
    e2e_settings: dict[str, Any],
    model_mode: E2EModelMode,
) -> str:
    model_cfg = dict(e2e_settings.get("model") or {}) if isinstance(e2e_settings.get("model"), dict) else {}
    route_name = str(model_cfg.get("route_name") or "e2e-stub-route").strip()
    provider_kind = str(model_cfg.get("provider_kind") or "anthropic").strip()
    target_model = str(model_cfg.get("target_model") or "e2e-stub-model").strip()
    api_key = str(model_cfg.get("api_key") or "e2e-stub-key").strip()
    base_url = model_cfg.get("base_url")
    extra_config = model_cfg.get("extra_config") if isinstance(model_cfg.get("extra_config"), dict) else {}
    route_settings = model_cfg.get("route_settings") if isinstance(model_cfg.get("route_settings"), dict) else {}
    # Former provider.extra_config and route.settings are now a single settings layer.
    settings = {**extra_config, **route_settings} or None

    if model_mode == E2EModelMode.REAL and not model_cfg:
        raise RuntimeError(
            "real E2E model mode requires e2e.model in DOYOUTRADE_E2E_CONFIG or tests/e2e/config.yaml"
        )

    route_repo = runtime["model_route_repository"]

    # Idempotent: reuse existing route if it already exists in the shared DB.
    try:
        await route_repo.get_by_route_name(route_name)
    except RecordNotFoundError:
        await route_repo.create(
            route_name=route_name,
            provider_kind=provider_kind,
            api_key=api_key,
            base_url=str(base_url) if base_url else None,
            target_model=target_model,
            settings=settings,
        )

    return route_name


async def seed_e2e_instrument_catalog(
    runtime: dict[str, Any],
    e2e_settings: dict[str, Any],
) -> None:
    symbols = ["600000.SH"]
    raw_symbols = e2e_settings.get("symbols")
    if isinstance(raw_symbols, list) and raw_symbols:
        symbols = [str(item).strip() for item in raw_symbols if str(item).strip()]
    rows = [
        {
            "symbol": symbol,
            "display_name": symbol,
            "market": symbol.split(".")[-1] if "." in symbol else None,
            "instrument_type": "stock",
            "is_tradable": True,
            "last_sync_source": "e2e",
            "raw": {"source": "tests/e2e"},
        }
        for symbol in symbols
    ]
    await runtime["service"].instrument_catalog_repository.upsert_rows(rows)


async def wait_for_debug_session_terminal(
    service,
    task_id: str,
    session_id: str,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        session = await service.get_debug_session(task_id, session_id)
        if session["status"] in TERMINAL_SESSION_STATUSES:
            await wait_for_model_invocation_tasks()
            return await service.get_debug_session(task_id, session_id)
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"debug session did not finish: {session_id}")
        await asyncio.sleep(0.05)


async def wait_for_model_invocation_tasks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0.05)
