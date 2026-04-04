from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from typing import List

from tradeclaw.observability import get_logger, get_tracer
from tradeclaw.domain.models import (
    AgentReview,
    CycleReport,
    OrderIntent,
    OrderProposal,
    RiskDecision,
)
from tradeclaw.execution.approval import ApprovalResult
from tradeclaw.execution.validator import OrderIntentValidator


logger = get_logger(__name__)
tracer = get_tracer(__name__)


PHASES: List[str] = [
    "load_context",
    "refresh_market_state",
    "refresh_portfolio_state",
    "build_universe",
    "run_signal_strategies",
    "run_agent_strategies",
    "build_order_intents",
    "run_risk_checks",
    "await_approval_if_needed",
    "dispatch_orders",
    "sync_fills_and_positions",
    "persist_trace_and_metrics",
]


@dataclass
class TradingWorker:
    data_provider: object
    universe_provider: object
    signal_strategy: object
    agent_strategy: object
    intent_builder: object
    intent_validator: object
    risk_engine: object
    approval_gate: object
    execution_adapter: object
    run_mode: str = "paper"
    trace_store: object = None
    last_run_id: str = ""

    async def run_cycle(self) -> CycleReport:
        """执行一轮完整交易循环（通常由调度器按 tick 触发）。

        数据流概览：事实数据只来自 data_provider / universe_provider → 规则产出候选单
        → Agent 审核 → 结构化 OrderIntent → 校验 → 风控 → 审批门 → 执行适配器。

        阶段名与 `PHASES` 及设计文档主循环一致，便于 trace / 监控对齐。
        """
        # --- 1. 本轮标识：每条 trace 挂在同一 run_id 下，便于按次排查 ---
        run_id = f"run-{uuid.uuid4()}"
        self.last_run_id = run_id
        with tracer.start_as_current_span("worker.run_cycle"):
            logger.info("worker cycle started run_id=%s run_mode=%s", run_id, self.run_mode)
            try:
                await self._record_phase(run_id, "load_context", {"status": "start"})

                # --- 2. 刷新市场与账户事实（价格、现金、权益、持仓列表）---
                # 具体来源由 data_provider 实现决定（mock / qmt-proxy 等），LLM 不应编造这些数值。
                with tracer.start_as_current_span("worker.phase.refresh_market_state"):
                    market_context = await _maybe_await(self.data_provider.get_market_context())
                    await self._record_phase(
                        run_id,
                        "refresh_market_state",
                        {"symbol_count": len(market_context.symbol_to_price)},
                    )

                with tracer.start_as_current_span("worker.phase.refresh_portfolio_state"):
                    account_snapshot = await _maybe_await(self.data_provider.get_account_snapshot())
                    await self._record_phase(
                        run_id,
                        "refresh_portfolio_state",
                        {"equity": account_snapshot.equity},
                    )
                    positions = await _maybe_await(self.data_provider.get_positions())

                # --- 3. 标的池：在事实数据之上决定本周期要考虑哪些代码 ---
                with tracer.start_as_current_span("worker.phase.build_universe"):
                    universe = await _maybe_await(
                        self.universe_provider.build_universe(market_context, account_snapshot, positions)
                    )
                    await self._record_phase(run_id, "build_universe", {"size": len(universe)})

                # --- 4. 规则层：生成「交易提案」OrderProposal（尚未带执行语义，偏信号/意图）---
                with tracer.start_as_current_span("worker.phase.run_signal_strategies"):
                    proposals = self.signal_strategy.generate(market_context, account_snapshot, positions, universe)
                    await self._record_phase(run_id, "run_signal_strategies", {"proposal_count": len(proposals)})

                # --- 5. Agent 层：对每条 proposal 给出是否批准、置信度与补充理由（可对接 LLM）---
                with tracer.start_as_current_span("worker.phase.run_agent_strategies"):
                    reviews = self.agent_strategy.review(proposals, market_context, account_snapshot, positions)
                    await self._record_phase(run_id, "run_agent_strategies", {"review_count": len(reviews)})

                # --- 6. 将 proposal + 审核结果合并为可校验的 OrderIntent（含 intent_id、参考价等）---
                with tracer.start_as_current_span("worker.phase.build_order_intents"):
                    intents = self._build_order_intents(proposals, reviews, market_context)
                    await self._record_phase(run_id, "build_order_intents", {"intent_count": len(intents)})

                # --- 7. 意图校验：字段合法、业务规则（如数量与金额互斥等），不通过则不会进入风控/下单 ---
                validator = self.intent_validator or OrderIntentValidator()
                valid_intents = []
                for intent in intents:
                    validation = validator.validate(intent)
                    if validation.ok:
                        valid_intents.append(intent)

                # --- 8. 风控：对每条合法 intent 给出 pass / veto；veto 的 intent 本周期不再审批/下单 ---
                with tracer.start_as_current_span("worker.phase.run_risk_checks"):
                    decisions = self.risk_engine.evaluate(valid_intents, account_snapshot, positions)
                    decision_by_id = {decision.intent_id: decision for decision in decisions}
                    await self._record_phase(run_id, "run_risk_checks", {"decision_count": len(decisions)})

                # --- 9. 逐单：风控通过 → 审批（可能同步通过、pending 排队、或拒绝）→ 提交执行适配器 ---
                submitted_count = 0
                vetoed_count = 0
                pending_approval_count = 0

                for intent in valid_intents:
                    decision = decision_by_id.get(
                        intent.intent_id,
                        RiskDecision(intent_id=intent.intent_id, action="pass"),
                    )
                    if decision.action == "veto":
                        vetoed_count += 1
                        logger.info("worker intent vetoed run_id=%s intent_id=%s", run_id, intent.intent_id)
                        continue

                    # live 等模式下可进入人工审批队列；paper / AutoApprovalGate 常直接 approved。
                    with tracer.start_as_current_span("worker.phase.await_approval_if_needed"):
                        approval = await _maybe_await(self._request_approval(intent, account_snapshot, market_context))
                        await self._record_phase(
                            run_id,
                            "await_approval_if_needed",
                            {"intent_id": intent.intent_id, "status": approval.status},
                        )
                    if approval.status == "pending":
                        pending_approval_count += 1
                        continue
                    if approval.status != "approved":
                        vetoed_count += 1
                        continue

                    # 纸面/仿真/真实路由由 execution_adapter 决定；此处只提交结构化 OrderIntent。
                    with tracer.start_as_current_span("worker.phase.dispatch_orders"):
                        await _maybe_await(self.execution_adapter.submit_intent(intent))
                        submitted_count += 1
                        await self._record_phase(
                            run_id,
                            "dispatch_orders",
                            {"intent_id": intent.intent_id, "status": "submitted"},
                        )

                # --- 10. 收尾 trace：当前实现未在此处拉最新成交/持仓，仅占位与设计阶段名一致 ---
                await self._record_phase(
                    run_id,
                    "sync_fills_and_positions",
                    {"submitted_count": submitted_count},
                )
                await self._record_phase(
                    run_id,
                    "persist_trace_and_metrics",
                    {
                        "submitted_count": submitted_count,
                        "vetoed_count": vetoed_count,
                        "pending_approval_count": pending_approval_count,
                    },
                )

                logger.info(
                    "worker cycle completed run_id=%s submitted_count=%s vetoed_count=%s pending_approval_count=%s",
                    run_id,
                    submitted_count,
                    vetoed_count,
                    pending_approval_count,
                )
                return CycleReport(
                    submitted_count=submitted_count,
                    vetoed_count=vetoed_count,
                    pending_approval_count=pending_approval_count,
                    completed_phases=list(PHASES),
                )
            except Exception:
                logger.exception("worker cycle failed run_id=%s", run_id)
                raise

    def _request_approval(self, intent, account_snapshot, market_context) -> ApprovalResult:
        if self.approval_gate is None:
            return ApprovalResult(status="approved", intent_id=intent.intent_id)
        return self.approval_gate.request(intent, account_snapshot, market_context, self.run_mode)

    def _build_order_intents(
        self,
        proposals: List[OrderProposal],
        reviews: List[AgentReview],
        market_context,
    ) -> List[OrderIntent]:
        if self.intent_builder is not None:
            return self.intent_builder.build(proposals, reviews, market_context)

        allowed_indexes = {review.proposal_index for review in reviews if review.approved}
        review_by_index = {review.proposal_index: review for review in reviews}

        intents: List[OrderIntent] = []
        for index, proposal in enumerate(proposals):
            if reviews and index not in allowed_indexes:
                continue

            review = review_by_index.get(index)
            rationale = proposal.rationale
            if review and review.rationale_appendix:
                rationale = f"{proposal.rationale}; {review.rationale_appendix}"

            reference_price = market_context.symbol_to_price.get(proposal.symbol, 0.0)
            intent = OrderIntent(
                intent_id=f"intent-{uuid.uuid4()}",
                symbol=proposal.symbol,
                side=proposal.side,
                quantity=proposal.quantity,
                amount=proposal.amount,
                order_type="market",
                tif="day",
                strategy_tag=proposal.strategy_tag,
                price_reference=reference_price,
                rationale=rationale,
            )
            intents.append(intent)

        return intents

    async def _append_trace(self, run_id: str, phase: str, payload: dict):
        if self.trace_store is None:
            return
        await _maybe_await(self.trace_store.append(run_id=run_id, phase=phase, payload=payload))

    async def _record_phase(self, run_id: str, phase: str, payload: dict):
        await self._append_trace(run_id, phase, payload)
        details = " ".join(f"{key}={value}" for key, value in payload.items())
        if details:
            logger.info("worker phase completed run_id=%s phase=%s %s", run_id, phase, details)
        else:
            logger.info("worker phase completed run_id=%s phase=%s", run_id, phase)

    async def aclose(self):
        for candidate in (self.data_provider, self.execution_adapter):
            close = getattr(candidate, "aclose", None)
            if close is not None:
                await _maybe_await(close())


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value
