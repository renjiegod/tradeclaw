from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from doyoutrade.assistant import AssistantService
from doyoutrade.assistant.repository import (
    SqlAlchemyAssistantRepository,
    SqlAlchemyAgentRepository,
    SqlAlchemyChannelRepository,
)
from doyoutrade.diagnostics import runtime_diag
from doyoutrade.config import AppConfig, ModelSettings, get_config
from doyoutrade.core.worker import TradingWorker
from doyoutrade.data.bars_cache_store import RepositoryBarsCacheStore
from doyoutrade.data.cached_bars import CachedBarsDataProvider
from doyoutrade.data.local_market_bars import LocalHistoricalBarsDataProvider
from doyoutrade.data.market_sync import MarketDataSyncService
from doyoutrade.data.account_resolution import (
    ResolvedAccount,
    register_default_account_resolver,
    resolved_account_from_record,
)
from doyoutrade.data.factory import build_trading_data_stack, resolve_effective_provider
from doyoutrade.account.store_reader import StoreBackedAccountReader
from doyoutrade.data.mock_provider import MockTradingDataProvider
from doyoutrade.execution.adapters import PaperExecutionAdapter, SimulatedBrokerAdapter
from doyoutrade.execution.approval import AutoApprovalGate, QueuedApprovalGate
from doyoutrade.execution.qmt_adapter import QmtExecutionAdapter
from doyoutrade.execution.risk import PassThroughRiskEngine
from doyoutrade.models.factory import build_model_adapter, wrap_with_recording
from doyoutrade.observability import get_logger, initialize_observability
from doyoutrade.observability.debug_span_export import (
    debug_span_queue_sink,
    register_span_persist_sink,
    start_debug_span_persist_worker,
    stop_debug_span_persist_worker,
)
from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.db import ensure_market_data_database_url, verify_market_schema
from doyoutrade.persistence.strategy_storage import StrategyStorage
from doyoutrade.persistence.repositories import (
    SqlAlchemyAccountRepository,
    SqlAlchemyApprovalRepository,
    SqlAlchemyAssistantLoadedSkillRepository,
    SqlAlchemyCachedBarsRepository,
    SqlAlchemyMarketBarsRepository,
    SqlAlchemyRunRepository,
    SqlAlchemyCycleRunRepository,
    SqlAlchemyDebugSessionRepository,
    SqlAlchemyDebugSessionSpanRepository,
    SqlAlchemyTaskRepository,
    SqlAlchemyTaskTriggerRepository,
    SqlAlchemyInstrumentCatalogRepository,
    SqlAlchemyModelInvocationRepository,
    SqlAlchemyModelRouteRepository,
    SqlAlchemyMonitorAlertRepository,
    SqlAlchemyDecisionSignalRepository,
    SqlAlchemyMonitorRuleRepository,
    SqlAlchemySystemStateRepository,
    SqlAlchemyTradeFillRepository,
    SqlAlchemyWatchlistRepository,
    create_model_invocation_recorder,
)
from doyoutrade.persistence.cached_bars_cleanup import purge_poisoned_cached_bars
from doyoutrade.persistence.adjust_poison_cleanup import purge_poisoned_qfq_rows
from doyoutrade.persistence.observability_ttl_prune import (
    ObservabilityPruneService,
    prune_observability_rows,
)
from doyoutrade.persistence.runtime_state import run_market_data_migrations, run_migrations
from doyoutrade.persistence.tick_session import TickSessionRepository
from doyoutrade.platform.service import TradingPlatformService
from doyoutrade.persistence import (
    SqlAlchemyStrategyDefinitionRepository,
)
from doyoutrade.runtime.cycle_task import CycleTaskConfig, DEFAULT_REACT_MAX_TURNS
from doyoutrade.runtime.scheduler import RuntimeScheduler
from doyoutrade.strategy_registry import StrategyRegistryService
from doyoutrade.strategy_runtime.compiler import StrategyCompiler
from doyoutrade.strategy_sdk import StrategyRunner
from doyoutrade.strategy_sdk.history_fetcher import BarsHistoryFetcher
from doyoutrade.strategy_sdk.watchlist_snapshot import WatchlistSnapshot
from doyoutrade.execution.position_manager import (
    PositionConstraints,
    PositionManager,
)
from doyoutrade.core.models import OrderIntent
from doyoutrade.core.signal_generator_protocol import SignalGenerationContext
from doyoutrade.debug import emit_debug_event

logger = get_logger(__name__)


@dataclass(frozen=True)
class StrategyRuntimeBinding:
    registry_service: StrategyRegistryService
    definition_repository: SqlAlchemyStrategyDefinitionRepository
    compiler: StrategyCompiler
    storage: StrategyStorage


def _synthetic_ledger_from_account_reader(account_reader) -> object | None:
    if isinstance(account_reader, StoreBackedAccountReader):
        store = account_reader._store
        if isinstance(store, MockTradingDataProvider):
            return store
    return None


class StrategyConfigurationError(Exception):
    """Raised when a strategy instance cannot be configured for execution.

    ``error_code`` is a stable token emitted into debug events so the
    cycle's failure mode is visible in structured form (not just free text).
    """

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class InstanceSignalGenerator:
    """Loads a strategy from disk and runs one cycle of the
    new :class:`Strategy` pipeline (informative prefetch → populate_indicators
    → on_bar → PositionManager.compute_intents).

    Implements :class:`SignalGeneratorProtocol`. Parameters declared as
    ``IntParameter`` / ``DecimalParameter`` etc. class attributes bind via
    :class:`StrategyRunner` descriptor binding (no ``__init__`` kwargs).

    Strategy code is loaded from disk via
    ``storage.version_dir(definition_id, code_version)`` rather than
    from the DB ``source_code`` column — the latter was removed in the
    strategy-as-files refactor (Task 2+).

    **Version pinning**: call ``await pin_code_version()`` at cycle-start
    (before ``generate_intents``) to resolve and store the version atomically.
    The worker stores the pinned values in ``cycle_runs`` so that a concurrent
    assistant edit bumping ``current_version`` does not affect the in-flight
    cycle.  Once pinned, ``generate_intents`` uses the stored ``_pinned_version``
    and ``_pinned_code_hash`` rather than re-reading from the DB.

    **Test escape hatch**: subclasses (or test-only instances) may set the class
    attribute ``_require_pin = False`` to allow calling ``generate_intents``
    without a prior ``pin_code_version()``.  In that case the method falls back
    to ``definition.current_version`` with a ``logger.warning``.  Production code
    MUST leave ``_require_pin = True`` (the default).
    """

    # Flip to False in isolated test subclasses only.  Never override in production.
    _require_pin: bool = True

    def __init__(
        self,
        config: CycleTaskConfig,
        definition_repository: SqlAlchemyStrategyDefinitionRepository,
        compiler: StrategyCompiler,
        storage: "StrategyStorage",
        data_provider=None,
        *,
        position_constraints: PositionConstraints | None = None,
        watchlist_repository=None,
    ) -> None:
        self._config = config
        self._definition_repository = definition_repository
        self._compiler = compiler
        self._storage = storage
        self.data_provider = data_provider
        self._position_constraints = position_constraints or _position_constraints_from_config(config)
        # Mechanism (B) of the watchlist→strategy contract: a per-worker frozen
        # snapshot (symbol→tags) backing ``ctx.dp.watchlist_symbols(tag=...)``.
        # Built lazily once on the first cycle and reused for the worker's life
        # (deterministic per backtest run; refreshed when the worker is rebuilt
        # on task reload/restart — same freshness semantics as the universe).
        self._watchlist_repository = watchlist_repository
        self._watchlist_snapshot: WatchlistSnapshot | None = None
        # Set by pin_code_version(); None means "resolve from DB on each call".
        self._pinned_version: str | None = None
        self._pinned_code_hash: str | None = None
        # Memoize successful compiles keyed by (definition_id, code_version).
        # ``validate_directory`` re-parses + smoke-tests the strategy source
        # (running populate_indicators / on_bar against synthetic regimes),
        # which dominated per-bar cost in backtests because generate_intents
        # ran it on EVERY bar. The versioned source dir is immutable for a
        # given (definition_id, code_version), so the artifact is safe to
        # reuse across cycles; a new pinned version is a different key.
        self._compile_cache: dict[tuple[str, str], Any] = {}

    def _binding_definition_id(self) -> str:
        """Return the bound ``definition_id`` or raise ``strategy_definition_missing``.

        StrategyInstance / ``si-`` bindings were removed; the runtime resolves
        the strategy purely from ``settings.strategy.definition_id``. A missing
        binding is a hard error (no silent fallback) so the failure mode is
        visible at cycle-start.
        """
        definition_id = self._config.strategy_definition_id.strip()
        if not definition_id:
            raise StrategyConfigurationError(
                "strategy binding requires a definition_id; none is set on "
                "settings.strategy.definition_id",
                error_code="strategy_definition_missing",
            )
        return definition_id

    async def _resolve_strategy_binding(self) -> tuple[Any, dict[str, Any], str]:
        """Return ``(definition, parameters, definition_id)``.

        Parameters = ``definition.default_parameters_json`` merged with the
        task's ``parameter_overrides`` (overrides win).
        """
        definition_id = self._binding_definition_id()
        definition = await self._definition_repository.get_definition(definition_id)
        parameters: dict[str, Any] = {}
        default_parameters = definition.default_parameters_json
        if isinstance(default_parameters, dict):
            parameters.update(default_parameters)
        parameters.update(self._config.strategy_parameter_overrides)
        return definition, parameters, definition.definition_id

    async def pin_code_version(self) -> tuple[str, str | None]:
        """Resolve and store the strategy version for this cycle.

        Must be called at cycle-start, before ``generate_intents``, so the
        worker can write ``(code_version, code_hash)`` into ``cycle_runs``
        before any step that might fail — guaranteeing the persisted record
        reflects what was actually compiled.

        Returns:
            ``(version_label, code_hash)`` — e.g. ``("v0001-abc123ef", "abc123ef")``.

        Raises:
            :class:`StrategyConfigurationError` with ``error_code="strategy_no_current_version"``
            when the definition has no finalized version yet.
        """
        definition, _parameters, bound_definition_id = (
            await self._resolve_strategy_binding()
        )

        if definition.current_version is None:
            await emit_debug_event(
                "strategy_no_current_version",
                {
                    "error_code": "strategy_no_current_version",
                    "strategy_definition_id": bound_definition_id,
                    "hint": (
                        "run finalize_strategy_authoring to promote a draft "
                        "before starting a live or backtest cycle"
                    ),
                },
            )
            raise StrategyConfigurationError(
                f"strategy definition {definition.definition_id} has no finalized "
                "version yet; run finalize_strategy_authoring first",
                error_code="strategy_no_current_version",
            )

        self._pinned_version = definition.current_version
        self._pinned_code_hash = definition.code_hash
        logger.info(
            "instance_signal_generator pinned version definition_id=%s "
            "code_version=%s code_hash=%s",
            bound_definition_id,
            self._pinned_version,
            self._pinned_code_hash,
        )
        return self._pinned_version, self._pinned_code_hash

    async def generate_intents(
        self, ctx: SignalGenerationContext
    ) -> list[OrderIntent]:
        definition, parameters, bound_definition_id = (
            await self._resolve_strategy_binding()
        )

        # Use the pinned version if set by pin_code_version().  In production
        # (``_require_pin = True``) calling generate_intents without a prior
        # pin_code_version() is a hard error — falling back to
        # definition.current_version would silently defeat the version-pin safety
        # guarantee (§错误可见性).  Tests may set ``_require_pin = False`` on a
        # subclass to exercise generate_intents in isolation.
        if self._pinned_version is None:
            if self._require_pin:
                logger.error(
                    "InstanceSignalGenerator.generate_intents called without "
                    "prior pin_code_version() — refusing to fall back to "
                    "definition.current_version (would defeat the version-pin "
                    "safety guarantee) definition_id=%s",
                    bound_definition_id,
                )
                raise StrategyConfigurationError(
                    "InstanceSignalGenerator must be pinned via "
                    "pin_code_version() before generate_intents()",
                    error_code="strategy_version_not_pinned",
                )
            logger.warning(
                "InstanceSignalGenerator: pin missing, falling back to "
                "definition.current_version — only safe in isolated tests "
                "definition_id=%s",
                bound_definition_id,
            )
            code_version = definition.current_version
        else:
            code_version = self._pinned_version

        if code_version is None:
            await emit_debug_event(
                "strategy_no_current_version",
                {
                    "error_code": "strategy_no_current_version",
                    "strategy_definition_id": bound_definition_id,
                    "hint": (
                        "run finalize_strategy_authoring to promote a draft "
                        "before starting a live or backtest cycle"
                    ),
                },
            )
            raise StrategyConfigurationError(
                f"strategy definition {definition.definition_id} has no finalized "
                "version yet; run finalize_strategy_authoring first",
                error_code="strategy_no_current_version",
            )

        code_root = self._storage.version_dir(
            definition.definition_id, code_version
        )
        cache_key = (definition.definition_id, code_version)
        compile_result = self._compile_cache.get(cache_key)
        if compile_result is None:
            compile_result = self._compiler.validate_directory(code_root)
            if not compile_result.success or compile_result.artifact is None:
                # Don't cache failures — a later retry should re-compile and
                # re-surface the (possibly transient) error.
                raise ValueError(
                    f"failed to compile strategy definition {definition.definition_id} "
                    f"(version={code_version}): "
                    f"{'; '.join(compile_result.errors)}"
                )
            self._compile_cache[cache_key] = compile_result
        strategy_class = compile_result.artifact.strategy_class
        class_name = compile_result.artifact.class_name

        # New Strategy contract: no __init__ kwargs. Parameters bind via
        # descriptor objects (IntParameter etc.) at runner setup time.
        # The runner emits a structured debug event when supplied
        # parameters don't correspond to any declared descriptor, so the
        # "I added a knob but forgot to declare it" bug surface remains
        # visible.
        try:
            strategy = strategy_class()
        except Exception as exc:
            raise ValueError(
                f"strategy {class_name!r}() raised during "
                f"instantiation: {exc}. The new Strategy API expects "
                "zero-arg constructors; tunable parameters bind via "
                "IntParameter / DecimalParameter etc. class attributes."
            ) from exc

        position_manager = PositionManager(
            constraints=self._position_constraints,
            strategy_tag=class_name,
        )
        history_fetcher = BarsHistoryFetcher(data_provider=self.data_provider)
        if self._watchlist_snapshot is None and self._watchlist_repository is not None:
            # One read per worker life (memoized). Keeps ctx.dp.watchlist_symbols
            # deterministic within a backtest run and free of live DB I/O inside
            # the sandboxed strategy body.
            snapshot_map = await self._watchlist_repository.snapshot()
            self._watchlist_snapshot = WatchlistSnapshot.from_mapping(snapshot_map)
        runner = StrategyRunner(
            strategy=strategy,
            position_manager=position_manager,
            history_fetcher=history_fetcher,
            parameters=parameters,
            watchlist_snapshot=self._watchlist_snapshot,
        )
        await emit_debug_event(
            "strategy_definition_execution",
            {
                "strategy_definition_id": definition.definition_id,
                "strategy_execution_profile": self._config.strategy_execution_profile,
                "trace": {
                    "definition_id": definition.definition_id,
                    "code_version": code_version,
                    "pinned": self._pinned_version is not None,
                    "class_name": class_name,
                    "code_hash": self._pinned_code_hash if self._pinned_version is not None else definition.code_hash,
                },
            },
        )
        return await runner.generate_intents(ctx)


def _position_constraints_from_config(config: CycleTaskConfig) -> PositionConstraints:
    """Map :class:`CycleTaskConfig` risk/sizing knobs to :class:`PositionConstraints`."""
    return PositionConstraints(
        equity_fraction=float(config.review_equity_fraction),
        max_single_order_amount=(
            float(config.max_single_order_amount)
            if config.max_single_order_amount is not None
            else None
        ),
        max_position_ratio=float(config.max_position_ratio),
        lot_size=int(config.lot_size),
        rebalance_hysteresis_lots=int(config.rebalance_hysteresis_lots),
        max_task_position_amount=(
            float(config.max_task_position_amount)
            if config.max_task_position_amount is not None
            else None
        ),
        max_task_position_ratio=(
            float(config.max_task_position_ratio)
            if config.max_task_position_ratio is not None
            else None
        ),
    )


def _build_signal_generator(
    instance_config: CycleTaskConfig | None,
    data_provider,
    *,
    strategy_runtime: StrategyRuntimeBinding | None,
    watchlist_repository=None,
) -> InstanceSignalGenerator:
    if instance_config is None:
        raise ValueError("task config is required for task execution")
    if not instance_config.strategy_definition_id.strip():
        # Binding is resolved purely from settings.strategy.definition_id now;
        # a missing definition_id is a hard, visible failure (§错误可见性).
        raise StrategyConfigurationError(
            "strategy binding requires a definition_id; none is set on "
            "settings.strategy.definition_id",
            error_code="strategy_definition_missing",
        )
    if strategy_runtime is None:
        raise ValueError("strategy runtime is not configured")
    return InstanceSignalGenerator(
        config=instance_config,
        definition_repository=strategy_runtime.definition_repository,
        compiler=strategy_runtime.compiler,
        storage=strategy_runtime.storage,
        data_provider=data_provider,
        watchlist_repository=watchlist_repository,
    )


def _build_live_execution_adapter(resolved_account, account_reader):
    """Resolve the execution adapter for a ``live`` task.

    A live account with a real QMT trading connection submits real broker orders
    via :class:`QmtExecutionAdapter`; everything else (mock account, or no
    connection) falls back to :class:`PaperExecutionAdapter` (paper fill on the
    live snapshot). Both implement the same ``ExecutionAdapterProtocol`` and are
    reached only through ``TradingWorker._dispatch_approved_intent`` after the
    SAME approval gate, so mock and qmt never diverge on the approval path
    (CLAUDE.md consistency contract).
    """
    client = getattr(account_reader, "client", None)
    if (
        getattr(resolved_account, "mode", None) == "live"
        and client is not None
        and getattr(resolved_account, "has_connection", False)
    ):
        return QmtExecutionAdapter(client)
    return PaperExecutionAdapter()


def requires_human_approval(instance_config, resolved_account) -> bool:
    """Whether order intents from this run must pass the human approval gate.

    The single, central judge for gate selection. Keyed to *live trading*
    (orders that can reach a real broker), NOT to the account being mock vs
    live: a mock account running in live mode rehearses the SAME approval path
    a qmt account takes for real money. That is the consistency contract
    (CLAUDE.md) — it eliminates the "mock works but qmt diverges" bug class by
    making the approval decision independent of which execution adapter is
    behind it. ``backtest`` / ``paper`` never gate.
    """
    return getattr(instance_config, "mode", None) == "live"


def _build_worker_from_config(
    instance_config,
    shared_approval_gate,
    app_cfg: AppConfig,
    model_recorder=None,
    *,
    cycle_run_repository=None,
    trade_fill_repository=None,
    model_settings: ModelSettings,
    resolved_account: ResolvedAccount,
    strategy_runtime: StrategyRuntimeBinding | None = None,
    cached_bars_repository=None,
    market_bars_repository=None,
    account_repository=None,
    watchlist_repository=None,
):
    risk_engine = PassThroughRiskEngine()
    effective = resolve_effective_provider(instance_config.data_provider, app_cfg.data.default_provider)
    logger.info(
        "worker build: provider=%s account_id=%s account_mode=%s has_connection=%s",
        effective,
        resolved_account.account_id or "(none)",
        resolved_account.mode,
        resolved_account.has_connection,
    )
    # session_id refresh write-back: live accounts that reconnect persist their
    # new trading session id back onto the account row (replaces the old
    # config.yaml persist_qmt_session_id path).
    session_persist = (
        account_repository.update_session_id if account_repository is not None else None
    )
    data_cache_policy = getattr(instance_config, "data_cache", None)
    data_provider, universe_provider, account_reader = build_trading_data_stack(
        effective,
        app_cfg.data,
        symbols=list(instance_config.universe),
        account=resolved_account,
        session_persist=session_persist,
        # Task-level backfill source order (data_cache.source_priority) overrides
        # the default auto chain when configured; None reproduces _AUTO_PRIORITY.
        source_priority=(
            data_cache_policy.source_priority if data_cache_policy is not None else None
        ),
    )
    if instance_config.mode == "backtest" and not isinstance(
        account_reader, StoreBackedAccountReader
    ):
        # Backtest must never read live broker positions. The data provider
        # (QMT historical / akshare / baostock) keeps supplying bars, but the
        # account snapshot has to come from the in-memory simulated ledger so
        # the SimulatedBrokerAdapter can apply fills against it.
        backtest_store = MockTradingDataProvider()
        account_reader = StoreBackedAccountReader(backtest_store)
    if market_bars_repository is not None:
        data_provider = LocalHistoricalBarsDataProvider(
            market_bars_repository,
            data_provider,
            provider=app_cfg.market_data.default_provider,
            adjust=data_provider.capabilities.default_adjust,  # 使用 provider 的默认 adjust
            # The task's data_cache policy drives local-first / auto-backfill and
            # the write-time continuity gate. None → defaults (legacy behaviour +
            # always-on continuity).
            policy=data_cache_policy,
        )
    if instance_config.mode == "live":
        cache_store = (
            RepositoryBarsCacheStore(cached_bars_repository)
            if cached_bars_repository is not None
            else None
        )
        data_provider = CachedBarsDataProvider(
            data_provider,
            scope="live",
            store=cache_store,
        )

    signal_generator = _build_signal_generator(
        instance_config,
        data_provider,
        strategy_runtime=strategy_runtime,
        watchlist_repository=watchlist_repository,
    )

    # Execution adapter selection (mock/qmt/backtest) is INDEPENDENT of approval
    # gate selection — see requires_human_approval. The live branch may resolve
    # to a real QmtExecutionAdapter (live account with a QMT connection) or the
    # PaperExecutionAdapter (mock account / no connection); either way the gate
    # is chosen by requires_human_approval so both rehearse the same approval.
    if instance_config.mode == "backtest":
        execution = SimulatedBrokerAdapter(
            ledger=_synthetic_ledger_from_account_reader(account_reader)
        )
    elif instance_config.mode == "live":
        execution = _build_live_execution_adapter(resolved_account, account_reader)
    else:
        execution = PaperExecutionAdapter(
            ledger=_synthetic_ledger_from_account_reader(account_reader)
        )

    approval_gate = (
        shared_approval_gate
        if requires_human_approval(instance_config, resolved_account)
        else AutoApprovalGate()
    )

    # Optional portfolio circuit breaker (None when no settings.protection →
    # default-off, no new phase behavior). Built once per worker so its equity
    # peak accumulates across cycles within a run.
    from doyoutrade.execution.protection import protection_engine_from_config

    protection_engine = protection_engine_from_config(
        getattr(instance_config, "protection_config", None)
    )

    return TradingWorker(
        data_provider=data_provider,
        account_reader=account_reader,
        universe_provider=universe_provider,
        signal_generator=signal_generator,
        risk_engine=risk_engine,
        execution_adapter=execution,
        run_mode=instance_config.mode,
        approval_gate=approval_gate,
        intent_validator=None,
        cycle_run_repository=cycle_run_repository,
        trade_fill_repository=trade_fill_repository,
        protection_engine=protection_engine,
        account_id=resolved_account.account_id or "",
    )


async def _build_quote_stream_service(account_repository, data_cfg=None):
    """Construct the realtime quote stream service from the default account's
    QMT proxy connection, with a non-QMT polling fallback chain for when no
    QMT account is connected (e.g. after QMT is banned).

    The service runs in **dynamic mode**: it holds the default-account
    resolver + a connection factory and re-resolves on every client register
    / subscription change, on a slow background poll, and when the account
    CRUD API calls ``refresh()``. So configuring / changing the default QMT
    account at runtime reconnects live quotes without a server restart.

    When no account repository is wired the service falls back to a
    permanently-disconnected static build — it still accepts WS clients and
    serves ``qmt_disconnected`` status frames + ``—`` placeholders rather
    than failing. The unavailability is therefore visible to the operator,
    never silently swallowed (CLAUDE.md §错误可见性).
    """
    from doyoutrade.data.quote_stream import QuoteStreamService
    from doyoutrade.data.mootdx_provider import MootdxRealtimeQuoteProvider
    from doyoutrade.data.akshare_provider import AkshareRealtimeQuoteProvider
    from doyoutrade.data.fallback_provider import FallbackRealtimeQuoteProvider

    # Polling fallback chain for realtime quotes when no qmt account is
    # connected: mootdx (通达信, per-symbol L1) first, akshare (em -> sina ->
    # tencent cascade) second for whatever mootdx leaves unanswered. Neither
    # constructor touches the network (lazy clients); a missing install /
    # upstream failure on one leg degrades visibly (debug event + warning)
    # and the chain tries the next, rather than blanking the watchlist.
    mootdx_fallback = FallbackRealtimeQuoteProvider(
        [MootdxRealtimeQuoteProvider(), AkshareRealtimeQuoteProvider()]
    )

    if account_repository is None:
        logger.info(
            "quote stream: no account repository; serving realtime quotes via "
            "mootdx/akshare polling fallback chain"
        )
        return QuoteStreamService(
            quote_provider=mootdx_fallback, ws_subscribe=None, has_connection=True
        )

    async def _resolver():
        return await account_repository.get_default_account()

    def _factory(account):
        from doyoutrade.data.qmt_proxy import QmtRealtimeQuoteProvider
        from doyoutrade.infra.qmt import create_qmt_proxy_rest_client
        from doyoutrade.infra.qmt_proxy_client import QmtProxyWsClient

        rest_client = create_qmt_proxy_rest_client(account)
        ws_client = QmtProxyWsClient(base_url=account.base_url, token=account.token)
        provider = QmtRealtimeQuoteProvider(rest_client)
        logger.info(
            "quote stream: building connection via qmt-proxy base_url=%s terminal=%s",
            account.base_url,
            account.qmt_terminal_id,
        )
        return provider, (lambda symbols: ws_client.subscribe_quotes(symbols)), ws_client.aclose

    # Suspension (停牌) overlay source: the same akshare event provider that
    # backs ``data events`` / ``stock screen --exclude-suspended``. Reduced to a
    # ``(symbols, asof) -> frozenset[suspended_code]`` callback. The service
    # only invokes it off its slow background loop (never per tick), so the
    # akshare round-trip never sits on the quote hot path. ``data_cfg`` may be
    # ``None`` (akshare provider ignores it today) — overlay then still works.
    async def _suspension_provider(symbols, asof):
        if not symbols:
            return frozenset()
        from doyoutrade.data.factory import build_event_provider

        event_provider = build_event_provider("auto", data_cfg)
        events = await event_provider.get_events_batch(list(symbols), asof=asof)
        return frozenset(
            code
            for code, items in events.items()
            if any(getattr(it, "event_type", None) == "suspension" for it in items)
        )

    return QuoteStreamService(
        account_resolver=_resolver,
        connection_factory=_factory,
        suspension_provider=_suspension_provider,
        fallback_provider=mootdx_fallback,
    )


async def _build_market_data_runtime(
    cfg: AppConfig,
    instrument_catalog_repository,
    *,
    migrate: bool,
    watchlist_repository=None,
):
    market_url = ensure_market_data_database_url(cfg.market_data.database_url)
    market_backend = (
        "timescaledb" if market_url.drivername == "postgresql+asyncpg" else "sqlite"
    )
    if migrate:
        await run_market_data_migrations(cfg.market_data.database_url)
    market_engine = None
    try:
        market_engine, market_session_factory = create_engine_and_session_factory(
            cfg.market_data.database_url,
            echo=cfg.database.echo,
            pool_pre_ping=cfg.database.pool_pre_ping,
        )
        async with market_engine.begin() as conn:
            await verify_market_schema(conn, drivername=market_url.drivername)
    except Exception:
        if market_engine is not None:
            await dispose_engine(market_engine)
        raise
    logger.info(
        "market_data storage backend=%s driver=%s database=%s",
        market_backend,
        market_url.drivername,
        market_url.database,
    )
    if market_backend == "sqlite" and cfg.market_data.sync_full_market:
        logger.warning(
            "market_data sync_full_market=true on the SQLite backend: full A-share "
            "sync works but is slow on SQLite; consider PostgreSQL + TimescaleDB "
            "(market_data.database_url) for full-market history"
        )
    await emit_debug_event(
        "market_data.startup.validated",
        {
            "database_url_driver": market_url.drivername,
            "backend": market_backend,
            "sync_full_market": cfg.market_data.sync_full_market,
        },
    )
    market_repository = SqlAlchemyMarketBarsRepository(market_session_factory)

    def _provider_factory():
        data_provider, _universe_provider, _account_reader = build_trading_data_stack(
            cfg.market_data.default_provider,
            cfg.data,
            symbols=[],
        )
        return data_provider

    bootstrap_provider = _provider_factory()
    sync_service = MarketDataSyncService(
        market_repository=market_repository,
        instrument_catalog_repository=instrument_catalog_repository,
        provider_factory=_provider_factory,
        intervals=cfg.market_data.enabled_intervals,
        lookback_years=cfg.market_data.lookback_years,
        provider=cfg.market_data.default_provider,
        adjust=bootstrap_provider.capabilities.default_adjust,
        concurrency=cfg.market_data.sync_concurrency,
        rate_limit_per_second=cfg.market_data.provider_rate_limit_per_second,
        watchlist_repository=watchlist_repository,
        sync_full_market=cfg.market_data.sync_full_market,
    )
    return market_engine, market_repository, sync_service


async def build_platform_runtime(
    app_cfg: AppConfig | None = None,
    *,
    migrate: bool = True,
    start_channels: bool = True,
):
    """Build the platform runtime.

    ``migrate`` defaults to True so the API server, e2e tests, and any
    other "long-running" host that owns the DB lifecycle keep the
    auto-migrate behavior. Short-lived CLI subprocesses (``doyoutrade-cli
    *``) pass ``migrate=False`` — they expect the operator to have run
    ``make migrate`` / ``make backend`` first, and skipping alembic
    avoids (a) the per-invocation cost on a hot path, (b) ``fileConfig``
    in ``alembic/env.py`` clobbering the structured logging handler
    installed by ``initialize_observability``, and (c) stray
    ``INFO [alembic.runtime.migration] ...`` lines on stderr polluting
    the CLI envelope.

    ``start_channels`` defaults to True for API/server hosts. CLI
    subprocesses pass False so command execution does not open unrelated
    Feishu/Lark WebSocket connections or leak their SDK logs into the
    command result.
    """

    cfg = app_cfg or get_config()
    initialize_observability(
        service_name=cfg.observability.service_name,
        log_level=cfg.observability.log_level,
        tracing_enabled=cfg.observability.tracing_enabled,
        console_enabled=cfg.observability.console_enabled,
    )
    if migrate:
        await run_migrations(cfg.database.url)
    engine, session_factory = create_engine_and_session_factory(
        cfg.database.url,
        echo=cfg.database.echo,
        pool_pre_ping=cfg.database.pool_pre_ping,
    )
    approval_repository = SqlAlchemyApprovalRepository(session_factory)
    task_repository = SqlAlchemyTaskRepository(session_factory)
    task_trigger_repository = SqlAlchemyTaskTriggerRepository(session_factory)
    monitor_rule_repository = SqlAlchemyMonitorRuleRepository(session_factory)
    decision_signal_repository = SqlAlchemyDecisionSignalRepository(session_factory)
    monitor_alert_repository = SqlAlchemyMonitorAlertRepository(session_factory)
    account_repository = SqlAlchemyAccountRepository(session_factory)
    # Stateless data tools (data run / screen / sector / fundamentals) resolve
    # the default market account through this global resolver (mirrors the
    # get_config() global) since they hold no repository handle.
    register_default_account_resolver(account_repository.get_default_account)
    instrument_catalog_repository = SqlAlchemyInstrumentCatalogRepository(session_factory)
    watchlist_repository = SqlAlchemyWatchlistRepository(session_factory)
    system_state_repository = SqlAlchemySystemStateRepository(session_factory)
    debug_session_repository = SqlAlchemyDebugSessionRepository(session_factory)
    debug_session_span_repository = SqlAlchemyDebugSessionSpanRepository(session_factory)
    model_invocation_repository = SqlAlchemyModelInvocationRepository(session_factory)
    runtime_diag("bootstrap: model_invocation_repository ok")
    assistant_repository = SqlAlchemyAssistantRepository(session_factory)
    # Persistent storage for SKILL.md content loaded via load_skill — used by
    # the assistant service to rebuild a ``<system-reminder>`` after context
    # compaction folds the original tool_result blocks away (T2 wires this
    # into LoadSkillTool, T3 into the reminder constructor).
    assistant_loaded_skill_repository = SqlAlchemyAssistantLoadedSkillRepository(
        session_factory
    )
    cycle_run_repository = SqlAlchemyCycleRunRepository(session_factory)
    run_repository = SqlAlchemyRunRepository(session_factory)
    trade_fill_repository = SqlAlchemyTradeFillRepository(session_factory)
    model_route_repository = SqlAlchemyModelRouteRepository(session_factory)
    strategy_definition_repository = SqlAlchemyStrategyDefinitionRepository(session_factory)
    cached_bars_repository = SqlAlchemyCachedBarsRepository(session_factory)
    try:
        market_engine, market_bars_repository, market_sync_service = await _build_market_data_runtime(
            cfg,
            instrument_catalog_repository,
            migrate=migrate,
            watchlist_repository=watchlist_repository,
        )
    except Exception:
        await dispose_engine(engine)
        raise
    model_recorder = create_model_invocation_recorder(model_invocation_repository)
    approval_gate = QueuedApprovalGate(approval_repository=approval_repository)
    tick_session_repository = TickSessionRepository(
        debug_session_repo=debug_session_repository,
        debug_session_span_repo=debug_session_span_repository,
        task_repository=task_repository,
    )
    runtime_diag("bootstrap: tick_session_repository + approval_gate ok")

    if cfg.observability.tracing_enabled:
        await start_debug_span_persist_worker(debug_session_span_repository.append_span)
        register_span_persist_sink(debug_span_queue_sink)
    else:
        register_span_persist_sink(None)

    scheduler = RuntimeScheduler()

    # StrategyStorage root: use DOYOUTRADE_HOME env var if set, otherwise
    # default to ~/.doyoutrade. The "strategies" sub-directory mirrors the
    # plan's recommended layout and is stable across restarts.
    _strategies_root = (
        Path(os.getenv("DOYOUTRADE_HOME", str(Path.home() / ".doyoutrade"))).expanduser()
        / "strategies"
    )
    strategy_storage = StrategyStorage(_strategies_root)

    strategy_runtime = StrategyRuntimeBinding(
        registry_service=StrategyRegistryService(strategy_definition_repository),
        definition_repository=strategy_definition_repository,
        compiler=StrategyCompiler(),
        storage=strategy_storage,
    )

    runtime_diag("bootstrap: creating TradingPlatformService")
    service = TradingPlatformService(
        scheduler=scheduler,
        app_cfg=cfg,
        worker_factory=lambda instance_config, model_settings, resolved_account: _build_worker_from_config(
            instance_config,
            approval_gate,
            cfg,
            model_recorder=model_recorder,
            cycle_run_repository=cycle_run_repository,
            trade_fill_repository=trade_fill_repository,
            model_settings=model_settings,
            resolved_account=resolved_account,
            strategy_runtime=strategy_runtime,
            cached_bars_repository=cached_bars_repository,
            market_bars_repository=market_bars_repository,
            account_repository=account_repository,
            watchlist_repository=watchlist_repository,
        ),
        task_repository=task_repository,
        account_repository=account_repository,
        watchlist_repository=watchlist_repository,
        system_state_repository=system_state_repository,
        debug_session_repository=debug_session_repository,
        debug_session_span_repository=debug_session_span_repository,
        model_invocation_repository=model_invocation_repository,
        cycle_run_repository=cycle_run_repository,
        run_repository=run_repository,
        trade_fill_repository=trade_fill_repository,
        tick_session_repository=tick_session_repository,
        task_trigger_repository=task_trigger_repository,
        monitor_rule_repository=monitor_rule_repository,
        monitor_alert_repository=monitor_alert_repository,
        decision_signal_repository=decision_signal_repository,
        default_data_provider=cfg.data.default_provider,
        instrument_catalog_repository=instrument_catalog_repository,
        app_data_settings=cfg.data,
        model_route_repository=model_route_repository,
        strategy_runtime=strategy_runtime,
        cached_bars_repository=cached_bars_repository,
        market_bars_repository=market_bars_repository,
    )

    if cached_bars_repository is not None:
        try:
            purge_result = await purge_poisoned_cached_bars(
                cached_bars_repository.session_factory
            )
            if purge_result["poisoned_rows"]:
                logger.info(
                    "cached_bars legacy poison purge complete poisoned_rows=%s "
                    "deleted_bars=%s deleted_ranges=%s",
                    purge_result["poisoned_rows"],
                    purge_result["deleted_bars"],
                    purge_result["deleted_ranges"],
                )
        except Exception as exc:
            logger.warning(
                "cached_bars poison purge failed error_type=%s error=%s",
                type(exc).__name__,
                exc,
            )
    if cached_bars_repository is not None and market_bars_repository is not None:
        try:
            qfq_purge_result = await purge_poisoned_qfq_rows(
                cached_bars_repository.session_factory,
                market_bars_repository.session_factory,
            )
            if qfq_purge_result["poisoned_keys"]:
                logger.info(
                    "qfq poison purge complete poisoned_keys=%s poisoned_symbols=%s "
                    "deleted_market_bars=%s deleted_market_sync_state=%s "
                    "deleted_cached_bars=%s deleted_cached_ranges=%s",
                    qfq_purge_result["poisoned_keys"],
                    qfq_purge_result["poisoned_symbols"],
                    qfq_purge_result["deleted_market_bars"],
                    qfq_purge_result["deleted_market_sync_state"],
                    qfq_purge_result["deleted_cached_bars"],
                    qfq_purge_result["deleted_cached_ranges"],
                )
        except Exception as exc:
            logger.warning(
                "qfq poison purge failed error_type=%s error=%s",
                type(exc).__name__,
                exc,
            )

    # Observability TTL: one-shot sweep at startup so accumulated rows are
    # trimmed even on a host that rarely restarts (the recurring loop below
    # owns steady-state). cycle_runs / trade_fills / runs are never pruned.
    if cfg.retention.enabled and cfg.retention.prune_on_startup:
        try:
            prune_counts = await prune_observability_rows(
                session_factory, ttl_days=cfg.retention.observability_ttl_days
            )
            if any(prune_counts.values()):
                logger.info(
                    "observability ttl prune (startup) complete ttl_days=%s deleted_total=%s",
                    cfg.retention.observability_ttl_days,
                    sum(prune_counts.values()),
                )
        except Exception as exc:
            logger.warning(
                "observability ttl prune (startup) failed error_type=%s error=%s",
                type(exc).__name__,
                exc,
            )

    # Recurring observability TTL prune. Started/stopped by the API server
    # lifecycle (doyoutrade/api/server.py) alongside the cron manager and the
    # job-watch service.
    observability_prune_service = (
        ObservabilityPruneService(
            session_factory=session_factory,
            ttl_days=cfg.retention.observability_ttl_days,
            interval_hours=cfg.retention.prune_interval_hours,
        )
        if cfg.retention.enabled
        else None
    )

    if cfg.market_data.sync_on_startup:
        await market_sync_service.start()

    quote_stream_service = await _build_quote_stream_service(account_repository, cfg.data)
    await quote_stream_service.start()

    async def _build_assistant_adapter(model_route_name: str | None):
        from doyoutrade.config import default_model_route_baseline
        from doyoutrade.models.route_resolution import resolve_model_settings

        route = (model_route_name or "").strip()
        if route:
            model_settings = await resolve_model_settings(
                route_name=route,
                route_repository=model_route_repository,
            )
        else:
            model_settings = default_model_route_baseline()
        adapter = build_model_adapter(model_settings)
        return wrap_with_recording(
            adapter,
            provider=model_settings.provider,
            provider_kind=model_settings.provider_kind,
            model=model_settings.model,
            recorder=model_recorder,
        )

    # Agent repository — used to load agent templates and seed default agent
    agent_repository = SqlAlchemyAgentRepository(session_factory)
    channel_repository = SqlAlchemyChannelRepository(session_factory)

    from doyoutrade.persistence.job_watches import SqlAlchemyAssistantJobWatchRepository

    assistant_job_watch_repository = SqlAlchemyAssistantJobWatchRepository(session_factory)

    assistant_service = AssistantService(
        assistant_repository,
        agent_repository=agent_repository,
        platform_service=service,
        strategy_registry_service=strategy_runtime.registry_service,
        strategy_definition_repository=strategy_definition_repository,
        model_adapter_factory=_build_assistant_adapter,
        loaded_skill_repository=assistant_loaded_skill_repository,
        job_watch_repository=assistant_job_watch_repository,
        run_repository=run_repository,
        decision_signal_repository=decision_signal_repository,
        instrument_catalog_repository=instrument_catalog_repository,
    )
    assistant_service.channel_repo = channel_repository

    # Job-watch wake-ups: polls assistant_job_watches against the run
    # repository and pushes a [job-completed] composition into the
    # originating session. Started/stopped by the API server lifecycle
    # (doyoutrade/api/server.py) alongside the cron manager.
    from doyoutrade.assistant.job_watcher import JobWatchService

    job_watch_service = JobWatchService(
        watch_repository=assistant_job_watch_repository,
        run_repository=run_repository,
        assistant_service=assistant_service,
    )

    # Pin the code-fixed builtin main agent (默认智能体). Idempotent: inserts it
    # if missing, otherwise re-pins its code-controlled identity (name / prompt
    # template / flags) while preserving the operator's editable knobs
    # (model_route_name / context_compaction / max_turns). Its system prompt is
    # the authoritative main_agent.j2 template; its skills (all enabled) and tools
    # (full in-process registry) are expanded in code at session load — see
    # doyoutrade/assistant/main_agent.py.
    main_agent = await agent_repository.ensure_main_agent()
    if not main_agent or not main_agent.get("id"):
        raise RuntimeError("ensure_main_agent did not produce the fixed main agent row")

    # Pin the code-fixed signal-card composer agent (信号卡片撰写器). Same
    # idempotent contract as the main agent: inserts if missing, otherwise
    # re-pins its code-controlled identity. It carries NO tools / NO skills —
    # its only job is narrating a prose trigger fire from a cycle digest into a
    # fixed-shape push card (see doyoutrade/assistant/signal_composer_agent.py).
    # Made the default composer for prose trigger delivery in
    # doyoutrade/runtime/trigger_delivery.py to keep the compose turn off the
    # main agent's full CLI/cron/skill surface (noise reduction + deterministic
    # card title/shape).
    signal_composer = await agent_repository.ensure_signal_composer_agent()
    if not signal_composer or not signal_composer.get("id"):
        raise RuntimeError(
            "ensure_signal_composer_agent did not produce the fixed composer agent row"
        )

    # Channel manager — initialized from persisted channel rows.
    from doyoutrade.assistant.channels import (
        ChannelManager,
        DingtalkChannel,
        EmailChannel,
        FeishuChannel,
        HttpChannel,
        SlackChannel,
        TelegramChannel,
        WecomChannel,
    )

    channel_manager = ChannelManager(assistant_service)

    for channel_row in await channel_repository.list_channels(enabled=True, include_secrets=True):
        channel_type = channel_row["type"]
        config = dict(channel_row.get("config") or {})
        secrets = dict(channel_row.get("secrets") or {})
        if channel_type == "feishu":
            channel = FeishuChannel(
                assistant_service=assistant_service,
                channel_id=channel_row["id"],
                app_id=str(config.get("app_id") or ""),
                app_secret=str(secrets.get("app_secret") or ""),
                encrypt_key=str(secrets.get("encrypt_key") or ""),
                verification_token=str(secrets.get("verification_token") or ""),
                domain=str(config.get("domain") or "feishu"),
                # Execution-side trade-approval gate so the channel's
                # ``trade_approval_resolve`` card action can approve/reject a
                # pending LIVE order. Same shared QueuedApprovalGate the worker
                # and scheduler resume sweep use (built above at this scope).
                trade_approval_gate=approval_gate,
            )
        elif channel_type == "http":
            channel = HttpChannel(
                assistant_service=assistant_service,
                channel_id=channel_row["id"],
            )
        elif channel_type == "email":
            channel = EmailChannel(
                assistant_service=assistant_service,
                channel_id=channel_row["id"],
                smtp_host=str(config.get("smtp_host") or ""),
                smtp_port=int(config.get("smtp_port") or 465),
                use_tls=bool(config.get("use_tls", True)),
                use_starttls=bool(config.get("use_starttls", False)),
                username=str(secrets.get("username") or ""),
                password=str(secrets.get("password") or ""),
                from_addr=str(config.get("from_addr") or ""),
                to_addrs=list(config.get("to_addrs") or []),
                subject_prefix=str(config.get("subject_prefix") or "[Doyoutrade]"),
            )
        elif channel_type == "wecom":
            channel = WecomChannel(
                assistant_service=assistant_service,
                channel_id=channel_row["id"],
                webhook_url=str(secrets.get("webhook_url") or ""),
                msg_type=str(config.get("msg_type") or "markdown"),
            )
        elif channel_type == "dingtalk":
            channel = DingtalkChannel(
                assistant_service=assistant_service,
                channel_id=channel_row["id"],
                webhook_url=str(secrets.get("webhook_url") or ""),
                sign_secret=str(secrets.get("sign_secret") or ""),
                msg_type=str(config.get("msg_type") or "markdown"),
            )
        elif channel_type == "telegram":
            channel = TelegramChannel(
                assistant_service=assistant_service,
                channel_id=channel_row["id"],
                bot_token=str(secrets.get("bot_token") or ""),
                chat_id=str(config.get("chat_id") or ""),
                message_thread_id=str(config.get("message_thread_id") or ""),
                api_base=str(config.get("api_base") or "https://api.telegram.org"),
            )
        elif channel_type == "slack":
            channel = SlackChannel(
                assistant_service=assistant_service,
                channel_id=channel_row["id"],
                webhook_url=str(secrets.get("webhook_url") or ""),
                bot_token=str(secrets.get("bot_token") or ""),
                slack_channel_id=str(config.get("channel_id") or ""),
                api_base=str(config.get("api_base") or "https://slack.com/api"),
            )
        else:
            logger.info("skipping unsupported channel type=%s id=%s", channel_type, channel_row["id"])
            continue
        channel._manager = channel_manager
        channel_manager.register(channel, agent_id=channel_row["agent_id"])

    # Attach the live ChannelManager to ``assistant_service`` so downstream
    # helpers (notably ``cron_executors._deliver``) can forward a persisted
    # assistant message to the originating channel without re-instantiating
    # channels or threading the manager through every executor. Mirrors the
    # ``channel_repo`` attachment above; ``channel_repo`` exposes DB rows,
    # while ``channel_manager`` exposes the running BaseChannel instances.
    assistant_service.channel_manager = channel_manager

    # 后台启动所有 Channel，完成后更新 DB 状态
    async def _start_channels_and_update_status() -> None:
        results = await channel_manager.start_all()
        for cid, exc in results.items():
            if exc is not None:
                logger.warning("channel start failed channel_id=%s exc=%s", cid, exc)
                await channel_repository.update_status(cid, status="stopped", last_error=str(exc))
            else:
                logger.info("channel started channel_id=%s", cid)
                await channel_repository.update_status(cid, status="running")

    if start_channels:
        asyncio.create_task(_start_channels_and_update_status())

    runtime_diag("bootstrap: restore_tasks start")
    await service.restore_tasks()
    runtime_diag("bootstrap: restore_tasks done")
    await service.restore_backtest_jobs()
    runtime_diag("bootstrap: restore_backtest_jobs done")
    async def aclose():
        await service.aclose()
        await quote_stream_service.aclose()
        await market_sync_service.aclose()
        await dispose_engine(market_engine)
        await stop_debug_span_persist_worker()
        register_span_persist_sink(None)
        await dispose_engine(engine)

    return {
        "engine": engine,
        "session_factory": session_factory,
        "task_repository": task_repository,
        "task_trigger_repository": task_trigger_repository,
        "monitor_rule_repository": monitor_rule_repository,
        "decision_signal_repository": decision_signal_repository,
        "monitor_alert_repository": monitor_alert_repository,
        "account_repository": account_repository,
        "watchlist_repository": watchlist_repository,
        "system_state_repository": system_state_repository,
        "instrument_catalog_repository": instrument_catalog_repository,
        "approval_repository": approval_repository,
        "debug_session_repository": debug_session_repository,
        "debug_session_span_repository": debug_session_span_repository,
        "tick_session_repository": tick_session_repository,
        "model_invocation_repository": model_invocation_repository,
        "cycle_run_repository": cycle_run_repository,
        "run_repository": run_repository,
        "trade_fill_repository": trade_fill_repository,
        "model_route_repository": model_route_repository,
        "assistant_repository": assistant_repository,
        "assistant_loaded_skill_repository": assistant_loaded_skill_repository,
        "assistant_service": assistant_service,
        "assistant_job_watch_repository": assistant_job_watch_repository,
        "job_watch_service": job_watch_service,
        "observability_prune_service": observability_prune_service,
        "channel_repository": channel_repository,
        "channel_manager": channel_manager,
        "scheduler": scheduler,
        "service": service,
        "strategy_registry_service": strategy_runtime.registry_service,
        "strategy_definition_repository": strategy_definition_repository,
        "strategy_storage": strategy_runtime.storage,
        "compiler": strategy_runtime.compiler,
        "market_bars_repository": market_bars_repository,
        "market_sync_service": market_sync_service,
        "quote_stream_service": quote_stream_service,
        "approval_gate": approval_gate,
        "aclose": aclose,
    }
