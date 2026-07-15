import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta

from doyoutrade.core.models import OrderIntent
from doyoutrade.execution.approval import QueuedApprovalGate
from doyoutrade.persistence.errors import RecordNotFoundError, StateConflictError


@dataclass
class _PendingRecord:
    approval_id: str
    intent_id: str
    mode: str
    status: str
    reason: str
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None = None
    intent_payload: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    trace_id: str | None = None
    account_id: str | None = None
    symbol: str | None = None
    action: str | None = None
    notional: str | None = None
    resolver_id: str | None = None
    decision_source: str | None = None
    decided_at: datetime | None = None
    dispatched_at: datetime | None = None
    dispatch_error: str | None = None
    dispatch_attempts: int | None = 0


class _FakeApprovalRepository:
    def __init__(self):
        # Keep resolved rows too so list_resumable can find approved-not-dispatched.
        self._records: dict[str, _PendingRecord] = {}

    async def create_pending(
        self,
        approval_id,
        intent_id,
        mode,
        created_at,
        expires_at,
        *,
        intent_payload=None,
        run_id=None,
        task_id=None,
        trace_id=None,
        account_id=None,
        symbol=None,
        action=None,
        notional=None,
    ):
        record = _PendingRecord(
            approval_id=approval_id,
            intent_id=intent_id,
            mode=mode,
            status="pending",
            reason="",
            created_at=created_at,
            expires_at=expires_at,
            intent_payload=intent_payload,
            run_id=run_id,
            task_id=task_id,
            trace_id=trace_id,
            account_id=account_id,
            symbol=symbol,
            action=action,
            notional=notional,
        )
        self._records[approval_id] = record
        return record

    async def list_pending(self):
        return sorted(
            (r for r in self._records.values() if r.status == "pending"),
            key=lambda item: item.created_at,
        )

    async def list_resumable(self):
        return sorted(
            (
                r
                for r in self._records.values()
                if r.status == "approved" and r.dispatched_at is None
            ),
            key=lambda item: item.created_at,
        )

    async def resolve(self, approval_id, status, reason="", *, resolver_id=None, decision_source=None):
        record = self._records[approval_id]
        record.status = status
        record.reason = reason
        record.resolved_at = datetime(2026, 1, 1, 10, 0, 0)
        record.decided_at = record.resolved_at
        if resolver_id is not None:
            record.resolver_id = resolver_id
        if decision_source is not None:
            record.decision_source = decision_source
        return record

    async def mark_dispatched(self, approval_id, dispatched_at):
        record = self._records.get(approval_id)
        if record is None or record.dispatched_at is not None:
            return False
        record.dispatched_at = dispatched_at
        record.dispatch_error = None
        return True

    async def mark_dispatch_failed(self, approval_id, error, *, abandon=False, dispatched_at=None):
        record = self._records[approval_id]
        record.dispatch_error = error
        record.dispatch_attempts = int(record.dispatch_attempts or 0) + 1
        if abandon:
            record.dispatched_at = dispatched_at or datetime(2026, 1, 1, 10, 0, 0)
        return record

    async def expire_pending(self, now):
        expired = []
        for record in list(self._records.values()):
            if record.status == "pending" and record.expires_at <= now:
                record.status = "expired"
                record.reason = "expired"
                record.resolved_at = now
                expired.append(record)
        return expired


class ApprovalQueueTests(unittest.IsolatedAsyncioTestCase):
    def _intent(self, intent_id="intent-x", amount=1000.0, price=10.0):
        return OrderIntent(
            intent_id=intent_id,
            symbol="600000.SH",
            action="buy",
            amount=amount,
            order_type="market",
            tif="day",
            strategy_tag="test",
            price_reference=price,
            rationale="test",
        )

    async def test_live_order_enters_pending_queue(self):
        repository = _FakeApprovalRepository()
        gate = QueuedApprovalGate(
            approval_repository=repository,
            min_notional_for_approval=500.0,
            timeout_seconds=60,
        )

        result = await gate.request(self._intent(), mode="live")

        self.assertEqual(result.status, "pending")
        self.assertIsNotNone(result.approval_id)
        self.assertEqual(len(await gate.list_pending()), 1)

    async def test_below_threshold_order_is_auto_approved(self):
        repository = _FakeApprovalRepository()
        gate = QueuedApprovalGate(
            approval_repository=repository,
            min_notional_for_approval=5000.0,
            timeout_seconds=60,
        )

        result = await gate.request(self._intent(amount=100.0, price=10.0), mode="live")

        self.assertEqual(result.status, "approved")
        self.assertEqual(await gate.list_pending(), [])

    async def test_approve_transitions_pending_to_approved(self):
        repository = _FakeApprovalRepository()
        gate = QueuedApprovalGate(
            approval_repository=repository,
            min_notional_for_approval=500.0,
            timeout_seconds=60,
        )
        pending = await gate.request(self._intent(intent_id="intent-1"), mode="live")

        approved = await gate.approve(pending.approval_id)

        self.assertEqual(approved.status, "approved")
        self.assertEqual(approved.intent_id, "intent-1")
        self.assertEqual(len(await gate.list_pending()), 0)

    async def test_reject_transitions_pending_to_rejected(self):
        repository = _FakeApprovalRepository()
        gate = QueuedApprovalGate(
            approval_repository=repository,
            min_notional_for_approval=500.0,
            timeout_seconds=60,
        )
        pending = await gate.request(self._intent(intent_id="intent-2"), mode="live")

        rejected = await gate.reject(pending.approval_id, reason="denied")

        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(rejected.reason, "denied")
        self.assertEqual(len(await gate.list_pending()), 0)

    async def test_expire_marks_request_as_expired(self):
        fake_now = datetime(2026, 1, 1, 10, 0, 0)

        class _Clock:
            def __init__(self):
                self.value = fake_now

            def __call__(self):
                return self.value

        clock = _Clock()
        repository = _FakeApprovalRepository()
        gate = QueuedApprovalGate(
            approval_repository=repository,
            min_notional_for_approval=500.0,
            timeout_seconds=30,
            clock=clock,
        )
        pending = await gate.request(self._intent(intent_id="intent-3"), mode="live")

        clock.value = fake_now + timedelta(seconds=31)
        expired = await gate.expire_pending()

        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].approval_id, pending.approval_id)
        self.assertEqual(expired[0].status, "expired")
        self.assertEqual(expired[0].reason, "expired")
        self.assertEqual(len(await gate.list_pending()), 0)

    async def test_default_in_memory_repository_raises_domain_errors_for_missing_and_resolved_entries(self):
        gate = QueuedApprovalGate(min_notional_for_approval=500.0, timeout_seconds=60)
        pending = await gate.request(self._intent(intent_id="intent-4"), mode="live")

        await gate.approve(pending.approval_id)

        with self.assertRaises(StateConflictError):
            await gate.reject(pending.approval_id, reason="too late")

        with self.assertRaises(RecordNotFoundError):
            await gate.approve("missing-approval")


class ApprovalHistoryListTests(unittest.IsolatedAsyncioTestCase):
    """list_approvals: filtered, paginated, newest-first history over ALL
    statuses (the web Approvals page). Exercises the DEFAULT in-memory
    repository so this is the same code path the runtime uses when not on
    postgres (no test/runtime drift)."""

    def _intent(self, intent_id, symbol="600000.SH", amount=1000.0, price=10.0, action="buy"):
        return OrderIntent(
            intent_id=intent_id,
            symbol=symbol,
            action=action,
            amount=amount,
            order_type="market",
            tif="day",
            strategy_tag="test",
            price_reference=price,
            rationale="test",
        )

    async def _seed(self):
        class _Clock:
            def __init__(self):
                self.value = datetime(2026, 6, 1, 0, 0, 0)

            def __call__(self):
                return self.value

        clock = _Clock()
        gate = QueuedApprovalGate(min_notional_for_approval=0.0, timeout_seconds=600, clock=clock)
        # a: pending, 600000, acct-1, created 01:00
        clock.value = datetime(2026, 6, 1, 1, 0, 0)
        a = await gate.request(self._intent("intent-a", symbol="600000.SH"), mode="live", account_id="acct-1")
        # b: approved (web), 600519, acct-2, created 02:00
        clock.value = datetime(2026, 6, 1, 2, 0, 0)
        b = await gate.request(self._intent("intent-b", symbol="600519.SH"), mode="live", account_id="acct-2")
        await gate.approve(b.approval_id, resolver_id="u1", decision_source="web")
        # c: rejected (feishu_card), 600000, acct-1, created 03:00
        clock.value = datetime(2026, 6, 1, 3, 0, 0)
        c = await gate.request(self._intent("intent-c", symbol="600000.SH"), mode="live", account_id="acct-1")
        await gate.reject(c.approval_id, reason="no", resolver_id="u2", decision_source="feishu_card")
        return gate, a, b, c

    async def test_no_filter_returns_all_newest_first(self):
        gate, a, b, c = await self._seed()
        items, total = await gate.list_approvals()
        self.assertEqual(total, 3)
        self.assertEqual(
            [item.approval_id for item in items],
            [c.approval_id, b.approval_id, a.approval_id],
        )

    async def test_status_filter(self):
        gate, a, b, c = await self._seed()
        pending, total = await gate.list_approvals(statuses=["pending"])
        self.assertEqual(total, 1)
        self.assertEqual(pending[0].approval_id, a.approval_id)

        resolved, total = await gate.list_approvals(statuses=["approved", "rejected"])
        self.assertEqual(total, 2)
        self.assertEqual(
            {item.approval_id for item in resolved}, {b.approval_id, c.approval_id}
        )

    async def test_symbol_and_source_and_search_filters(self):
        gate, a, b, c = await self._seed()
        by_symbol, total = await gate.list_approvals(symbol="600000.SH")
        self.assertEqual(total, 2)
        self.assertEqual(
            {item.approval_id for item in by_symbol}, {a.approval_id, c.approval_id}
        )

        by_source, total = await gate.list_approvals(decision_source="web")
        self.assertEqual([item.approval_id for item in by_source], [b.approval_id])

        by_search, total = await gate.list_approvals(search="intent-b")
        self.assertEqual([item.approval_id for item in by_search], [b.approval_id])

    async def test_created_after_filter(self):
        gate, a, b, c = await self._seed()
        items, total = await gate.list_approvals(created_after=datetime(2026, 6, 1, 2, 30, 0))
        self.assertEqual(total, 1)
        self.assertEqual(items[0].approval_id, c.approval_id)

    async def test_pagination_returns_page_and_full_total(self):
        gate, a, b, c = await self._seed()
        page1, total = await gate.list_approvals(limit=2, offset=0)
        self.assertEqual(total, 3)
        self.assertEqual(
            [item.approval_id for item in page1], [c.approval_id, b.approval_id]
        )
        page2, total = await gate.list_approvals(limit=2, offset=2)
        self.assertEqual(total, 3)
        self.assertEqual([item.approval_id for item in page2], [a.approval_id])


if __name__ == "__main__":
    unittest.main()
