from __future__ import annotations

import os

from tradeclaw.channels.manager import ChannelManager
from tradeclaw.core.worker import TradingWorker
from tradeclaw.data.qmt_proxy import QmtLiveDataProvider, QmtUniverseProvider
from tradeclaw.data.qmt_proxy_client import QmtProxyRestClient
from tradeclaw.domain.models import AccountSnapshot, AgentReview, MarketContext, OrderProposal, PositionSnapshot
from tradeclaw.execution.adapters import PaperExecutionAdapter, SimulatedBrokerAdapter
from tradeclaw.execution.approval import AutoApprovalGate, QueuedApprovalGate
from tradeclaw.execution.risk import BasicRiskEngine, RiskConfig
from tradeclaw.persistence.trace_store import InMemoryTraceStore
from tradeclaw.platform.service import TradingPlatformService
from tradeclaw.runtime.scheduler import RuntimeScheduler


class _DemoDataProvider:
    def get_market_context(self):
        return MarketContext(symbol_to_price={"600000.SH": 10.0, "601318.SH": 50.0})

    def get_account_snapshot(self):
        return AccountSnapshot(cash=100000.0, equity=100000.0)

    def get_positions(self):
        return [PositionSnapshot(symbol="600000.SH", quantity=0, cost_price=0.0)]


class _DemoUniverseProvider:
    def build_universe(self, *_):
        return ["600000.SH", "601318.SH"]


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


def _resolve_symbols():
    raw = os.getenv("TRADECLAW_SYMBOLS", "600000.SH,601318.SH")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _build_data_context():
    base_url = os.getenv("TRADECLAW_QMT_BASE_URL")
    symbols = _resolve_symbols()
    if not base_url:
        return _DemoDataProvider(), _DemoUniverseProvider()

    token = os.getenv("TRADECLAW_QMT_TOKEN")
    timeout_seconds = float(os.getenv("TRADECLAW_QMT_TIMEOUT", "5"))
    client = QmtProxyRestClient(
        base_url=base_url,
        token=token,
        timeout_seconds=timeout_seconds,
    )
    data_provider = QmtLiveDataProvider(client=client, symbols=symbols)
    universe_provider = QmtUniverseProvider(symbols=symbols)
    return data_provider, universe_provider


def _build_worker_from_config(config, shared_approval_gate):
    risk_engine = BasicRiskEngine(
        RiskConfig(
            max_single_order_amount=20000.0,
            max_position_ratio=0.30,
        )
    )
    data_provider, universe_provider = _build_data_context()

    if config.mode == "backtest":
        execution = SimulatedBrokerAdapter()
        approval_gate = AutoApprovalGate()
    elif config.mode == "live":
        execution = PaperExecutionAdapter()
        approval_gate = shared_approval_gate
    else:
        execution = PaperExecutionAdapter()
        approval_gate = AutoApprovalGate()

    return TradingWorker(
        data_provider=data_provider,
        universe_provider=universe_provider,
        signal_strategy=_DemoSignalStrategy(),
        agent_strategy=_DemoAgentStrategy(),
        intent_builder=None,
        intent_validator=None,
        risk_engine=risk_engine,
        approval_gate=approval_gate,
        execution_adapter=execution,
        run_mode=config.mode,
        trace_store=InMemoryTraceStore(),
    )


def build_platform_runtime():
    approval_gate = QueuedApprovalGate(min_notional_for_approval=1000.0, timeout_seconds=300)
    scheduler = RuntimeScheduler()
    service = TradingPlatformService(
        scheduler=scheduler,
        worker_factory=lambda config: _build_worker_from_config(config, approval_gate),
    )
    channel_manager = ChannelManager(service=service, approval_gate=approval_gate)
    return {
        "scheduler": scheduler,
        "service": service,
        "approval_gate": approval_gate,
        "channel_manager": channel_manager,
    }
