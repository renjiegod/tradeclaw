import unittest

from tradeclaw.core.worker import TradingWorker
from tradeclaw.domain.models import AccountSnapshot, AgentReview, MarketContext, OrderProposal, PositionSnapshot, RiskDecision
from tradeclaw.execution.approval import ApprovalResult
from tradeclaw.persistence.trace_store import InMemoryTraceStore


class _StaticDataProvider:
    def get_market_context(self):
        return MarketContext(symbol_to_price={"600000.SH": 10.0})

    def get_account_snapshot(self):
        return AccountSnapshot(cash=100000.0, equity=100000.0)

    def get_positions(self):
        return [PositionSnapshot(symbol="600000.SH", quantity=0, cost_price=0.0)]


class _UniverseProvider:
    def build_universe(self, *_):
        return ["600000.SH"]


class _SignalStrategy:
    def generate(self, *_):
        return [
            OrderProposal(
                symbol="600000.SH",
                side="buy",
                quantity=100,
                strategy_tag="test",
                rationale="test",
            )
        ]


class _AgentStrategy:
    def review(self, *_):
        return [AgentReview(proposal_index=0, confidence=0.9, approved=True)]


class _Risk:
    def evaluate(self, intents, *_):
        return [RiskDecision(intent_id=intents[0].intent_id, action="pass")]


class _Approval:
    def request(self, intent, *_):
        return ApprovalResult(status="approved", intent_id=intent.intent_id)


class _Execution:
    def submit_intent(self, intent):
        return intent


class WorkerTraceTests(unittest.TestCase):
    def test_worker_persists_run_phases_to_trace_store(self):
        store = InMemoryTraceStore()
        worker = TradingWorker(
            data_provider=_StaticDataProvider(),
            universe_provider=_UniverseProvider(),
            signal_strategy=_SignalStrategy(),
            agent_strategy=_AgentStrategy(),
            intent_builder=None,
            intent_validator=None,
            risk_engine=_Risk(),
            approval_gate=_Approval(),
            execution_adapter=_Execution(),
            trace_store=store,
        )

        worker.run_cycle()

        events = store.get_run_events(worker.last_run_id)
        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[-1].phase, "persist_trace_and_metrics")


if __name__ == "__main__":
    unittest.main()
