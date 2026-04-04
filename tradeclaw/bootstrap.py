from __future__ import annotations

from tradeclaw.channels.manager import ChannelManager
from tradeclaw.config import AppConfig, get_config
from tradeclaw.core.worker import TradingWorker
from tradeclaw.data.factory import build_trading_data_stack, resolve_effective_provider
from tradeclaw.domain.models import AgentReview, OrderProposal
from tradeclaw.execution.adapters import PaperExecutionAdapter, SimulatedBrokerAdapter
from tradeclaw.execution.approval import AutoApprovalGate, QueuedApprovalGate
from tradeclaw.execution.risk import BasicRiskEngine, RiskConfig
from tradeclaw.models.factory import build_model_adapter
from tradeclaw.observability import initialize_observability
from tradeclaw.persistence.db import create_engine_and_session_factory, dispose_engine
from tradeclaw.persistence.repositories import (
    SqlAlchemyApprovalRepository,
    SqlAlchemyInstanceRepository,
    SqlAlchemySystemStateRepository,
    SqlAlchemyTraceEventRepository,
)
from tradeclaw.persistence.runtime_state import create_trace_store, run_migrations
from tradeclaw.platform.service import TradingPlatformService
from tradeclaw.runtime.scheduler import RuntimeScheduler
from tradeclaw.strategies.agent import LangChainAgentStrategy


class _DemoSignalStrategy:
    def generate(self, market_context, account_snapshot, positions, universe):
        if not universe:
            return []
        symbol = universe[0]
        return [
            OrderProposal(
                symbol=symbol,
                side="buy",
                quantity=100,
                strategy_tag="demo-signal",
                rationale="demo strategy generated one candidate",
            )
        ]


class _DemoAgentStrategy:
    def review(self, proposals, *_):
        reviews = []
        for index, _ in enumerate(proposals):
            reviews.append(
                AgentReview(
                    proposal_index=index,
                    confidence=0.75,
                    approved=True,
                    rationale_appendix="demo agent approved",
                )
            )
        return reviews


def _build_agent_strategy(app_cfg: AppConfig):
    provider = app_cfg.model.provider.strip().lower()
    if provider == "demo":
        return _DemoAgentStrategy()
    adapter = build_model_adapter(app_cfg.model)
    return LangChainAgentStrategy(adapter=adapter)


def _build_worker_from_config(instance_config, shared_approval_gate, trace_repository, app_cfg: AppConfig):
    risk_engine = BasicRiskEngine(
        RiskConfig(
            max_single_order_amount=app_cfg.risk.max_single_order_amount,
            max_position_ratio=app_cfg.risk.max_position_ratio,
        )
    )
    effective = resolve_effective_provider(instance_config.data_provider, app_cfg.data.default_provider)
    data_provider, universe_provider = build_trading_data_stack(effective, app_cfg.data)
    agent_strategy = _build_agent_strategy(app_cfg)

    if instance_config.mode == "backtest":
        execution = SimulatedBrokerAdapter()
        approval_gate = AutoApprovalGate()
    elif instance_config.mode == "live":
        execution = PaperExecutionAdapter()
        approval_gate = shared_approval_gate
    else:
        execution = PaperExecutionAdapter()
        approval_gate = AutoApprovalGate()

    return TradingWorker(
        data_provider=data_provider,
        universe_provider=universe_provider,
        signal_strategy=_DemoSignalStrategy(),
        agent_strategy=agent_strategy,
        intent_builder=None,
        intent_validator=None,
        risk_engine=risk_engine,
        approval_gate=approval_gate,
        execution_adapter=execution,
        run_mode=instance_config.mode,
        trace_store=create_trace_store(trace_repository),
    )


async def build_platform_runtime(app_cfg: AppConfig | None = None):
    cfg = app_cfg or get_config()
    initialize_observability(
        service_name=cfg.observability.service_name,
        log_level=cfg.observability.log_level,
        tracing_enabled=cfg.observability.tracing_enabled,
        console_enabled=cfg.observability.console_enabled,
    )
    _validate_model_config(cfg)
    await run_migrations(cfg.database.url)
    engine, session_factory = create_engine_and_session_factory(
        cfg.database.url,
        echo=cfg.database.echo,
        pool_pre_ping=cfg.database.pool_pre_ping,
    )
    approval_repository = SqlAlchemyApprovalRepository(session_factory)
    instance_repository = SqlAlchemyInstanceRepository(session_factory)
    system_state_repository = SqlAlchemySystemStateRepository(session_factory)
    trace_repository = SqlAlchemyTraceEventRepository(session_factory)
    approval_gate = QueuedApprovalGate(
        approval_repository=approval_repository,
        min_notional_for_approval=cfg.approval.min_notional_for_approval,
        timeout_seconds=cfg.approval.timeout_seconds,
    )
    scheduler = RuntimeScheduler()
    service = TradingPlatformService(
        scheduler=scheduler,
        worker_factory=lambda instance_config: _build_worker_from_config(
            instance_config, approval_gate, trace_repository, cfg
        ),
        instance_repository=instance_repository,
        system_state_repository=system_state_repository,
        default_data_provider=cfg.data.default_provider,
    )
    await service.restore_instances()
    channel_manager = ChannelManager(service=service, approval_gate=approval_gate)

    async def aclose():
        await service.aclose()
        await dispose_engine(engine)

    return {
        "engine": engine,
        "session_factory": session_factory,
        "instance_repository": instance_repository,
        "system_state_repository": system_state_repository,
        "approval_repository": approval_repository,
        "trace_repository": trace_repository,
        "scheduler": scheduler,
        "service": service,
        "approval_gate": approval_gate,
        "channel_manager": channel_manager,
        "aclose": aclose,
    }


def _validate_model_config(app_cfg: AppConfig):
    provider = app_cfg.model.provider.strip().lower()
    if provider == "demo":
        return
    _build_agent_strategy(app_cfg)
