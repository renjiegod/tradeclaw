from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional


@dataclass
class ApprovalResult:
    status: str
    intent_id: str
    reason: str = ""
    approval_id: Optional[str] = None


@dataclass
class PendingApproval:
    approval_id: str
    intent_id: str
    created_at: datetime
    expires_at: datetime
    mode: str


class AutoApprovalGate:
    def request(self, intent, account_snapshot=None, market_context=None, mode="paper") -> ApprovalResult:
        return ApprovalResult(status="approved", intent_id=intent.intent_id)


class QueuedApprovalGate:
    def __init__(
        self,
        require_approval_modes=None,
        min_notional_for_approval: float = 0.0,
        timeout_seconds: int = 300,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.require_approval_modes = set(require_approval_modes or {"live"})
        self.min_notional_for_approval = float(min_notional_for_approval)
        self.timeout_seconds = int(timeout_seconds)
        self.clock = clock or datetime.utcnow
        self._pending: Dict[str, PendingApproval] = {}

    def request(self, intent, account_snapshot=None, market_context=None, mode="paper") -> ApprovalResult:
        notional = _calculate_notional(intent)
        if mode not in self.require_approval_modes:
            return ApprovalResult(status="approved", intent_id=intent.intent_id)
        if notional < self.min_notional_for_approval:
            return ApprovalResult(status="approved", intent_id=intent.intent_id)

        now = self.clock()
        approval_id = str(uuid.uuid4())
        self._pending[approval_id] = PendingApproval(
            approval_id=approval_id,
            intent_id=intent.intent_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self.timeout_seconds),
            mode=mode,
        )
        return ApprovalResult(status="pending", intent_id=intent.intent_id, approval_id=approval_id)

    def list_pending(self) -> List[PendingApproval]:
        return sorted(self._pending.values(), key=lambda item: item.created_at)

    def approve(self, approval_id: str) -> ApprovalResult:
        pending = self._pending.pop(approval_id)
        return ApprovalResult(
            status="approved",
            intent_id=pending.intent_id,
            approval_id=pending.approval_id,
        )

    def reject(self, approval_id: str, reason: str = "") -> ApprovalResult:
        pending = self._pending.pop(approval_id)
        return ApprovalResult(
            status="rejected",
            intent_id=pending.intent_id,
            reason=reason,
            approval_id=pending.approval_id,
        )

    def expire_pending(self, now: Optional[datetime] = None) -> List[ApprovalResult]:
        current = now or self.clock()
        expired_ids = [
            approval_id
            for approval_id, pending in self._pending.items()
            if pending.expires_at <= current
        ]
        results: List[ApprovalResult] = []
        for approval_id in expired_ids:
            pending = self._pending.pop(approval_id)
            results.append(
                ApprovalResult(
                    status="rejected",
                    intent_id=pending.intent_id,
                    reason="approval timeout",
                    approval_id=pending.approval_id,
                )
            )
        return results


def _calculate_notional(intent) -> float:
    if intent.amount is not None:
        return float(intent.amount)
    if intent.quantity is None:
        return 0.0
    return float(intent.quantity) * float(intent.price_reference)
