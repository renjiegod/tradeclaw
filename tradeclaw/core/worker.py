from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass
from typing import List

from tradeclaw.domain.models import (
    AgentReview,
    CycleReport,
    OrderIntent,
    OrderProposal,
    RiskDecision,
)
from tradeclaw.execution.approval import ApprovalResult
from tradeclaw.execution.validator import OrderIntentValidator


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
        run_id = f"run-{uuid.uuid4()}"
        self.last_run_id = run_id
        self._append_trace(run_id, "load_context", {"status": "start"})

        market_context = await _maybe_await(self.data_provider.get_market_context())
        self._append_trace(run_id, "refresh_market_state", {"symbol_count": len(market_context.symbol_to_price)})
        account_snapshot = await _maybe_await(self.data_provider.get_account_snapshot())
        self._append_trace(run_id, "refresh_portfolio_state", {"equity": account_snapshot.equity})
        positions = await _maybe_await(self.data_provider.get_positions())
        universe = await _maybe_await(
            self.universe_provider.build_universe(market_context, account_snapshot, positions)
        )
        self._append_trace(run_id, "build_universe", {"size": len(universe)})

        proposals = self.signal_strategy.generate(market_context, account_snapshot, positions, universe)
        self._append_trace(run_id, "run_signal_strategies", {"proposal_count": len(proposals)})
        reviews = self.agent_strategy.review(proposals, market_context, account_snapshot, positions)
        self._append_trace(run_id, "run_agent_strategies", {"review_count": len(reviews)})
        intents = self._build_order_intents(proposals, reviews, market_context)
        self._append_trace(run_id, "build_order_intents", {"intent_count": len(intents)})

        validator = self.intent_validator or OrderIntentValidator()
        valid_intents = []
        for intent in intents:
            validation = validator.validate(intent)
            if validation.ok:
                valid_intents.append(intent)

        decisions = self.risk_engine.evaluate(valid_intents, account_snapshot, positions)
        decision_by_id = {decision.intent_id: decision for decision in decisions}
        self._append_trace(run_id, "run_risk_checks", {"decision_count": len(decisions)})

        submitted_count = 0
        vetoed_count = 0
        pending_approval_count = 0

        for intent in valid_intents:
            decision = decision_by_id.get(intent.intent_id, RiskDecision(intent_id=intent.intent_id, action="pass"))
            if decision.action == "veto":
                vetoed_count += 1
                continue

            approval = await _maybe_await(self._request_approval(intent, account_snapshot, market_context))
            self._append_trace(
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

            await _maybe_await(self.execution_adapter.submit_intent(intent))
            submitted_count += 1
            self._append_trace(
                run_id,
                "dispatch_orders",
                {"intent_id": intent.intent_id, "status": "submitted"},
            )

        self._append_trace(
            run_id,
            "sync_fills_and_positions",
            {"submitted_count": submitted_count},
        )
        self._append_trace(
            run_id,
            "persist_trace_and_metrics",
            {
                "submitted_count": submitted_count,
                "vetoed_count": vetoed_count,
                "pending_approval_count": pending_approval_count,
            },
        )

        return CycleReport(
            submitted_count=submitted_count,
            vetoed_count=vetoed_count,
            pending_approval_count=pending_approval_count,
            completed_phases=list(PHASES),
        )

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

    def _append_trace(self, run_id: str, phase: str, payload: dict):
        if self.trace_store is None:
            return
        self.trace_store.append(run_id=run_id, phase=phase, payload=payload)

    async def aclose(self):
        for candidate in (self.data_provider, self.execution_adapter):
            close = getattr(candidate, "aclose", None)
            if close is not None:
                await _maybe_await(close())


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value
