import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta

from tradeclaw.domain.models import OrderIntent
from tradeclaw.execution.approval import QueuedApprovalGate
from tradeclaw.persistence.errors import RecordNotFoundError, StateConflictError


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


class _FakeApprovalRepository:
    def __init__(self):
        self._pending: dict[str, _PendingRecord] = {}

    async def create_pending(self, approval_id, intent_id, mode, created_at, expires_at):
        record = _PendingRecord(
            approval_id=approval_id,
            intent_id=intent_id,
            mode=mode,
            status="pending",
            reason="",
            created_at=created_at,
            expires_at=expires_at,
        )
        self._pending[approval_id] = record
        return record

    async def list_pending(self):
        return sorted(self._pending.values(), key=lambda item: item.created_at)

    async def resolve(self, approval_id, status, reason=""):
        record = self._pending.pop(approval_id)
        record.status = status
        record.reason = reason
        record.resolved_at = datetime(2026, 1, 1, 10, 0, 0)
        return record

    async def expire_pending(self, now):
        expired = []
        for approval_id, record in list(self._pending.items()):
            if record.expires_at <= now:
                self._pending.pop(approval_id)
                record.status = "expired"
                record.reason = "expired"
                record.resolved_at = now
                expired.append(record)
        return expired


class ApprovalQueueTests(unittest.IsolatedAsyncioTestCase):
    def _intent(self, intent_id="intent-x", quantity=100, price=10.0):
        return OrderIntent(
            intent_id=intent_id,
            symbol="600000.SH",
            side="buy",
            quantity=quantity,
            amount=None,
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

        result = await gate.request(self._intent(quantity=10, price=10.0), mode="live")

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


if __name__ == "__main__":
    unittest.main()
