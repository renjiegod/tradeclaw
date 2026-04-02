import unittest
from datetime import datetime, timedelta

from tradeclaw.domain.models import OrderIntent
from tradeclaw.execution.approval import QueuedApprovalGate


class ApprovalQueueTests(unittest.TestCase):
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

    def test_live_order_enters_pending_queue(self):
        gate = QueuedApprovalGate(min_notional_for_approval=500.0, timeout_seconds=60)

        result = gate.request(self._intent(), mode="live")

        self.assertEqual(result.status, "pending")
        self.assertIsNotNone(result.approval_id)
        self.assertEqual(len(gate.list_pending()), 1)

    def test_approve_transitions_pending_to_approved(self):
        gate = QueuedApprovalGate(min_notional_for_approval=500.0, timeout_seconds=60)
        pending = gate.request(self._intent(intent_id="intent-1"), mode="live")

        approved = gate.approve(pending.approval_id)

        self.assertEqual(approved.status, "approved")
        self.assertEqual(approved.intent_id, "intent-1")
        self.assertEqual(len(gate.list_pending()), 0)

    def test_expire_marks_request_as_rejected(self):
        fake_now = datetime(2026, 1, 1, 10, 0, 0)

        class _Clock:
            def __init__(self):
                self.value = fake_now

            def __call__(self):
                return self.value

        clock = _Clock()
        gate = QueuedApprovalGate(min_notional_for_approval=500.0, timeout_seconds=30, clock=clock)
        pending = gate.request(self._intent(intent_id="intent-2"), mode="live")

        clock.value = fake_now + timedelta(seconds=31)
        expired = gate.expire_pending()

        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].approval_id, pending.approval_id)
        self.assertEqual(expired[0].status, "rejected")
        self.assertEqual(len(gate.list_pending()), 0)


if __name__ == "__main__":
    unittest.main()
