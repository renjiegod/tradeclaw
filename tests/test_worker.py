import unittest

from tradeclaw.core.worker import TradingWorker
from tradeclaw.domain.models import (
    AccountSnapshot,
    AgentReview,
    MarketContext,
    OrderIntent,
    OrderProposal,
    PositionSnapshot,
    RiskDecision,
)
from tradeclaw.execution.approval import ApprovalResult


class _StaticDataProvider:
    def get_market_context(self):
        return MarketContext(symbol_to_price={"600000.SH": 10.0})

    def get_account_snapshot(self):
        return AccountSnapshot(cash=100000.0, equity=100000.0)

    def get_positions(self):
        return [PositionSnapshot(symbol="600000.SH", quantity=0, cost_price=0.0)]


class _StaticUniverseProvider:
    def build_universe(self, *_):
        return ["600000.SH"]


class _SignalStrategy:
    def generate(self, *_):
        return [
            OrderProposal(
                symbol="600000.SH",
                side="buy",
                quantity=100,
                strategy_tag="ma-cross",
                rationale="fast ma crossed over slow ma",
            )
        ]


class _AgentStrategy:
    def review(self, proposals, *_):
        return [
            AgentReview(
                proposal_index=0,
                confidence=0.8,
                approved=True,
                rationale_appendix="news sentiment neutral",
            )
        ]


class _PassRisk:
    def evaluate(self, intents, *_):
        return [RiskDecision(intent_id=intents[0].intent_id, action="pass")]


class _VetoRisk:
    def evaluate(self, intents, *_):
        return [RiskDecision(intent_id=intents[0].intent_id, action="veto", reason="max position")]


class _ApprovalPass:
    def request(self, intent, *_):
        return ApprovalResult(status="approved", intent_id=intent.intent_id)


class _ApprovalPending:
    def request(self, intent, *_):
        return ApprovalResult(status="pending", intent_id=intent.intent_id)


class _ExecutionRecorder:
    def __init__(self):
        self.submitted = []

    def submit_intent(self, intent):
        self.submitted.append(intent)


class TradingWorkerTests(unittest.TestCase):
    def _build_worker(self, risk_engine, approval_gate):
        execution = _ExecutionRecorder()
        worker = TradingWorker(
            data_provider=_StaticDataProvider(),
            universe_provider=_StaticUniverseProvider(),
            signal_strategy=_SignalStrategy(),
            agent_strategy=_AgentStrategy(),
            intent_builder=None,
            intent_validator=None,
            risk_engine=risk_engine,
            approval_gate=approval_gate,
            execution_adapter=execution,
        )
        return worker, execution

    def test_dispatches_order_after_risk_and_approval(self):
        worker, execution = self._build_worker(_PassRisk(), _ApprovalPass())

        report = worker.run_cycle()

        self.assertEqual(len(execution.submitted), 1)
        self.assertEqual(execution.submitted[0].symbol, "600000.SH")
        self.assertEqual(report.submitted_count, 1)
        self.assertIn("dispatch_orders", report.completed_phases)

    def test_blocks_order_when_risk_vetoes(self):
        worker, execution = self._build_worker(_VetoRisk(), _ApprovalPass())

        report = worker.run_cycle()

        self.assertEqual(len(execution.submitted), 0)
        self.assertEqual(report.vetoed_count, 1)

    def test_holds_order_when_approval_pending(self):
        worker, execution = self._build_worker(_PassRisk(), _ApprovalPending())

        report = worker.run_cycle()

        self.assertEqual(len(execution.submitted), 0)
        self.assertEqual(report.pending_approval_count, 1)


if __name__ == "__main__":
    unittest.main()
