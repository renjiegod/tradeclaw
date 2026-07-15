"""Live-trading approval: mock/qmt consistency + persisted-intent resume.

Covers the consistency contract (same OrderIntent + same price → same volume on
both PaperExecutionAdapter and QmtExecutionAdapter), the gate persisting the
intent body + run context, the resumable lifecycle, and the worker re-dispatch
path threading the original run_id onto the fill.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field

from doyoutrade.bootstrap import requires_human_approval
from doyoutrade.core.cycle_state import CycleRunState
from doyoutrade.core.models import FillRecord, OrderIntent, intent_from_json
from doyoutrade.core.worker import TradingWorker
from doyoutrade.execution.adapters import PaperExecutionAdapter
from doyoutrade.execution.approval import QueuedApprovalGate
from doyoutrade.execution.order_quantity import resolve_order_quantity
from doyoutrade.execution.qmt_adapter import QmtExecutionAdapter


def _intent(intent_id="oi-1", action="buy", amount=10000.0, price=10.0, symbol="600000.SH") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        symbol=symbol,
        action=action,
        amount=amount,
        order_type="limit",
        tif="day",
        strategy_tag="TestStrat",
        price_reference=price,
        rationale="unit-test",
        signal_tag="sig",
    )


class _StubQmtClient:
    """Stub for QmtProxyRestClient.submit_order — returns a canned proxy dict,
    or raises ``error`` (a QmtProxyError subclass) to simulate a proxy fault."""

    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    async def submit_order(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return dict(self._response)


def _accepted_order(volume: int, price: float, filled: int | None = None) -> dict:
    f = volume if filled is None else filled
    return {
        "order_id": "ord-123",
        "stock_code": "600000.SH",
        "side": "BUY",
        "order_type": "LIMIT",
        "volume": volume,
        "price": price,
        "status": "已报",
        "filled_volume": f,
        "filled_amount": float(f) * price,
        "average_price": price if f else None,
    }


@dataclass
class _RecordingAdapter:
    """Returns a deterministic FillRecord; records submitted intents."""

    fills: list = field(default_factory=list)
    submitted: list = field(default_factory=list)

    async def submit_intent(self, intent, *, cycle_state=None, market_context=None):
        self.submitted.append(intent)
        price = float(intent.price_reference)
        resolved = resolve_order_quantity(intent, price)
        if not resolved.ok:
            return None
        fill = FillRecord(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.action,
            quantity=resolved.quantity,
            price=price,
        )
        self.fills.append(fill)
        return fill


class _RecordingFillRepo:
    def __init__(self):
        self.inserted: list[dict] = []

    async def insert_fill(self, **kwargs):
        self.inserted.append(kwargs)
        return True


class MockQmtConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_intent_same_volume_paper_and_qmt(self):
        """The keystone: a buy of 10000 notional @10.0 → 1000 shares on BOTH
        adapters. mock can never resolve a different quantity than qmt."""
        intent = _intent(action="buy", amount=10000.0, price=10.0)

        paper = PaperExecutionAdapter()
        paper_fill = await paper.submit_intent(intent)

        client = _StubQmtClient(_accepted_order(volume=1000, price=10.0))
        qmt = QmtExecutionAdapter(client)
        qmt_fill = await qmt.submit_intent(intent)

        self.assertIsNotNone(paper_fill)
        self.assertIsNotNone(qmt_fill)
        self.assertEqual(paper_fill.quantity, 1000.0)
        self.assertEqual(qmt_fill.quantity, 1000.0)
        # The broker received exactly the shared-resolver volume + mapped side.
        self.assertEqual(client.calls[0]["volume"], 1000)
        self.assertEqual(client.calls[0]["side"], "BUY")
        self.assertEqual(client.calls[0]["order_type"], "LIMIT")

    async def test_qmt_sell_side_mapped(self):
        intent = _intent(action="sell", amount=500.0, price=12.0)
        client = _StubQmtClient(
            {**_accepted_order(volume=500, price=12.0), "side": "SELL"}
        )
        qmt = QmtExecutionAdapter(client)
        fill = await qmt.submit_intent(intent)
        self.assertIsNotNone(fill)
        self.assertEqual(client.calls[0]["side"], "SELL")
        self.assertEqual(client.calls[0]["volume"], 500)

    async def test_qmt_rejected_status_returns_none_no_fill(self):
        intent = _intent(action="buy", amount=10000.0, price=10.0)
        client = _StubQmtClient({"order_id": "", "status": "废单", "filled_volume": 0})
        qmt = QmtExecutionAdapter(client)
        fill = await qmt.submit_intent(intent)
        self.assertIsNone(fill)  # never counted as submitted
        self.assertEqual(qmt.fills, [])

    async def test_qmt_sub_one_share_never_reaches_broker(self):
        # 5 notional @10.0 → 0 whole shares; rejected by the shared resolver
        # BEFORE any broker call (same as Paper's execution_zero_fill path).
        intent = _intent(action="buy", amount=5.0, price=10.0)
        client = _StubQmtClient(_accepted_order(volume=1, price=10.0))
        qmt = QmtExecutionAdapter(client)
        fill = await qmt.submit_intent(intent)
        self.assertIsNone(fill)
        self.assertEqual(client.calls, [])  # broker never contacted

    async def test_qmt_proxy_errors_return_none_without_propagating(self):
        # The proxy maps a failed real submit to 400 (ClientError), a generic
        # server fault to 500 (ServerError), and auth to 401 (AuthenticationError)
        # — all QmtProxyError siblings. None may propagate out of submit_intent
        # (that would fail the whole cycle); each is an isolated, visible veto.
        from qmt_proxy_sdk.exceptions import (
            AuthenticationError,
            ClientError,
            ServerError,
        )

        for err in (
            ClientError("真实下单失败", status_code=400),
            ServerError("提交订单失败", status_code=500),
            AuthenticationError("unauthorized", status_code=401),
        ):
            intent = _intent(action="buy", amount=10000.0, price=10.0)
            client = _StubQmtClient(error=err)
            qmt = QmtExecutionAdapter(client)
            fill = await qmt.submit_intent(intent)
            self.assertIsNone(fill, f"{type(err).__name__} should yield no fill")
            self.assertEqual(qmt.fills, [])
            self.assertEqual(len(client.calls), 1, "broker was contacted exactly once")

    async def test_qmt_accepted_unfilled_limit_records_ordered_volume(self):
        # A resting LIMIT order (filled_volume == 0) still records the SUBMITTED
        # order at the ordered volume + limit price; broker state is on the fill.
        intent = _intent(action="buy", amount=10000.0, price=10.0)
        client = _StubQmtClient(_accepted_order(volume=1000, price=10.0, filled=0))
        qmt = QmtExecutionAdapter(client)
        fill = await qmt.submit_intent(intent)
        self.assertIsNotNone(fill)
        self.assertEqual(fill.quantity, 1000.0)
        self.assertEqual(fill.price, 10.0)


class RequiresHumanApprovalTests(unittest.TestCase):
    def test_live_requires_approval(self):
        cfg = type("C", (), {"mode": "live"})()
        self.assertTrue(requires_human_approval(cfg, None))

    def test_paper_and_backtest_do_not(self):
        for mode in ("paper", "backtest", "signal_only", None):
            cfg = type("C", (), {"mode": mode})()
            self.assertFalse(requires_human_approval(cfg, None))


class GatePersistenceAndResumeTests(unittest.IsolatedAsyncioTestCase):
    async def _pending_gate(self):
        gate = QueuedApprovalGate(min_notional_for_approval=0.0, require_approval_modes={"live"})
        intent = _intent(action="buy", amount=10000.0, price=10.0)
        state = CycleRunState(run_id="run-1", trace_id="tr-1", task_id="task-1", agent_name="agent")
        result = await gate.request(intent, mode="live", cycle_state=state, account_id="acct-1")
        return gate, intent, result

    async def test_request_persists_intent_body_and_run_context(self):
        gate, intent, result = await self._pending_gate()
        self.assertEqual(result.status, "pending")
        pendings = await gate.list_pending()
        self.assertEqual(len(pendings), 1)
        p = pendings[0]
        self.assertEqual(p.run_id, "run-1")
        self.assertEqual(p.task_id, "task-1")
        self.assertEqual(p.trace_id, "tr-1")
        self.assertEqual(p.account_id, "acct-1")
        self.assertEqual(p.symbol, "600000.SH")
        self.assertEqual(p.action, "buy")
        self.assertEqual(p.notional, "10000")
        # The intent body round-trips so it can be re-dispatched.
        self.assertTrue(p.intent_payload)
        oi = intent_from_json(p.intent_payload)
        self.assertEqual(oi.intent_id, intent.intent_id)
        self.assertEqual(oi.amount, 10000.0)
        self.assertEqual(oi.price_reference, 10.0)

    async def test_approve_makes_resumable_then_dispatch_clears_it(self):
        gate, _intent_obj, result = await self._pending_gate()
        self.assertEqual(await gate.list_resumable(), [])  # pending != resumable
        await gate.approve(result.approval_id, resolver_id="u1", decision_source="web")
        resumable = await gate.list_resumable()
        self.assertEqual(len(resumable), 1)
        self.assertEqual(resumable[0].approval_id, result.approval_id)
        self.assertEqual(resumable[0].resolver_id, "u1")
        self.assertEqual(resumable[0].decision_source, "web")
        # Once dispatched it must never resume again (idempotent).
        marked = await gate.mark_dispatched(result.approval_id)
        self.assertTrue(marked)
        self.assertEqual(await gate.list_resumable(), [])

    async def test_rejected_is_never_resumable(self):
        gate, _intent_obj, result = await self._pending_gate()
        await gate.reject(result.approval_id, reason="too big", resolver_id="u1")
        self.assertEqual(await gate.list_resumable(), [])

    async def test_paper_mode_auto_approves_no_pending(self):
        gate = QueuedApprovalGate(min_notional_for_approval=0.0, require_approval_modes={"live"})
        intent = _intent()
        state = CycleRunState(run_id="r", trace_id="t", task_id="k", agent_name="a")
        result = await gate.request(intent, mode="paper", cycle_state=state)
        self.assertEqual(result.status, "approved")
        self.assertEqual(await gate.list_pending(), [])


class WorkerResumeDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_preapproved_threads_original_run_id_onto_fill(self):
        adapter = _RecordingAdapter()
        repo = _RecordingFillRepo()
        worker = TradingWorker(
            data_provider=None,
            account_reader=None,
            universe_provider=None,
            signal_generator=None,
            risk_engine=None,
            execution_adapter=adapter,
            run_mode="live",
            trade_fill_repository=repo,
        )
        intent = _intent(action="buy", amount=10000.0, price=10.0)
        fill_payload = await worker.dispatch_preapproved_intent(intent, run_id="run-orig")
        self.assertIsNotNone(fill_payload)
        self.assertEqual(len(adapter.submitted), 1)
        self.assertEqual(adapter.submitted[0].intent_id, intent.intent_id)
        # The resumed fill is correlated with the ORIGINAL cycle's run_id.
        self.assertEqual(len(repo.inserted), 1)
        self.assertEqual(repo.inserted[0]["cycle_run_id"], "run-orig")

    async def test_dispatch_preapproved_rejected_returns_none(self):
        adapter = _RecordingAdapter()
        repo = _RecordingFillRepo()
        worker = TradingWorker(
            data_provider=None,
            account_reader=None,
            universe_provider=None,
            signal_generator=None,
            risk_engine=None,
            execution_adapter=adapter,
            run_mode="live",
            trade_fill_repository=repo,
        )
        # sub-one-share → adapter returns None → no fill persisted.
        intent = _intent(action="buy", amount=5.0, price=10.0)
        fill_payload = await worker.dispatch_preapproved_intent(intent, run_id="run-orig")
        self.assertIsNone(fill_payload)
        self.assertEqual(repo.inserted, [])


@dataclass
class _ResumeSnapshot:
    approval_id: str
    intent_id: str = "oi-1"
    task_id: str = "task-1"
    symbol: str = "600000.SH"
    run_id: str = "run-1"
    dispatch_attempts: int = 0


class _FakeResumeGate:
    def __init__(self, resumable):
        self._resumable = list(resumable)
        self.dispatched: list[str] = []
        self.failed: list[tuple] = []

    async def list_resumable(self):
        return list(self._resumable)

    async def mark_dispatched(self, approval_id, dispatched_at=None):
        self.dispatched.append(approval_id)
        return True

    async def mark_dispatch_failed(self, approval_id, error, *, abandon=False):
        self.failed.append((approval_id, error, abandon))


class _FakeResumeService:
    def __init__(self, outcomes):
        self._outcomes = outcomes
        self.calls: list[str] = []

    async def dispatch_resumed_approval(self, approval):
        self.calls.append(approval.approval_id)
        return self._outcomes[approval.approval_id]


class SchedulerResumeSweepTests(unittest.IsolatedAsyncioTestCase):
    def _scheduler(self, gate, service):
        from doyoutrade.runtime.trigger_scheduler import TriggerScheduler

        return TriggerScheduler(
            service=service,
            trigger_repository=None,
            approval_gate=gate,
        )

    async def test_dispatched_marks_dispatched(self):
        ap = _ResumeSnapshot(approval_id="appr-1")
        gate = _FakeResumeGate([ap])
        service = _FakeResumeService(
            {"appr-1": {"status": "dispatched", "fill": {"symbol": "600000.SH", "quantity": 1000.0, "price": 10.0}, "run_id": "run-1"}}
        )
        sched = self._scheduler(gate, service)
        await sched._resume_approved_intents()
        self.assertEqual(gate.dispatched, ["appr-1"])
        self.assertEqual(gate.failed, [])

    async def test_skipped_not_running_leaves_resumable(self):
        ap = _ResumeSnapshot(approval_id="appr-2")
        gate = _FakeResumeGate([ap])
        service = _FakeResumeService({"appr-2": {"status": "skipped", "reason": "task_not_running"}})
        sched = self._scheduler(gate, service)
        await sched._resume_approved_intents()
        self.assertEqual(gate.dispatched, [])  # not marked → retried next sweep
        self.assertEqual(gate.failed, [])

    async def test_failed_records_failure_without_abandon_under_budget(self):
        ap = _ResumeSnapshot(approval_id="appr-3", dispatch_attempts=0)
        gate = _FakeResumeGate([ap])
        service = _FakeResumeService(
            {"appr-3": {"status": "failed", "reason": "adapter_rejected"}}
        )
        sched = self._scheduler(gate, service)
        await sched._resume_approved_intents()
        self.assertEqual(gate.dispatched, [])
        self.assertEqual(len(gate.failed), 1)
        approval_id, _error, abandon = gate.failed[0]
        self.assertEqual(approval_id, "appr-3")
        self.assertFalse(abandon)

    async def test_failed_abandons_after_budget(self):
        # attempts already at budget-1 → this failure abandons (terminal).
        from doyoutrade.runtime.trigger_scheduler import _MAX_RESUME_ATTEMPTS

        ap = _ResumeSnapshot(approval_id="appr-4", dispatch_attempts=_MAX_RESUME_ATTEMPTS - 1)
        gate = _FakeResumeGate([ap])
        service = _FakeResumeService({"appr-4": {"status": "failed", "reason": "adapter_rejected"}})
        sched = self._scheduler(gate, service)
        await sched._resume_approved_intents()
        self.assertEqual(len(gate.failed), 1)
        _approval_id, _error, abandon = gate.failed[0]
        self.assertTrue(abandon)


if __name__ == "__main__":
    unittest.main()
