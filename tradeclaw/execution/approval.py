from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from tradeclaw.persistence.errors import RecordNotFoundError, StateConflictError


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
    status: str = "pending"
    reason: str = ""
    resolved_at: Optional[datetime] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _InMemoryApprovalRepository:
    def __init__(self):
        self._records: Dict[str, PendingApproval] = {}

    async def create_pending(self, approval_id: str, intent_id: str, mode: str, created_at: datetime, expires_at: datetime):
        if approval_id in self._records:
            raise StateConflictError(f"approval already exists: {approval_id}")
        record = PendingApproval(
            approval_id=approval_id,
            intent_id=intent_id,
            created_at=created_at,
            expires_at=expires_at,
            mode=mode,
        )
        self._records[approval_id] = record
        return record

    async def list_pending(self) -> List[PendingApproval]:
        pending = [record for record in self._records.values() if record.status == "pending"]
        return sorted(pending, key=lambda item: item.created_at)

    async def resolve(self, approval_id: str, status: str, reason: str = "") -> PendingApproval:
        record = self._records.get(approval_id)
        if record is None:
            raise RecordNotFoundError(f"approval not found: {approval_id}")
        if record.status != "pending":
            raise StateConflictError(f"approval already resolved: {approval_id}")
        record.status = status
        record.reason = reason
        record.resolved_at = _utcnow()
        return record

    async def expire_pending(self, now: datetime) -> List[PendingApproval]:
        expired_ids = [
            approval_id
            for approval_id, pending in self._records.items()
            if pending.status == "pending" and pending.expires_at <= now
        ]
        records: List[PendingApproval] = []
        for approval_id in expired_ids:
            record = self._records[approval_id]
            record.status = "expired"
            record.reason = "expired"
            record.resolved_at = now
            records.append(record)
        return records


class AutoApprovalGate:
    def request(self, intent, account_snapshot=None, market_context=None, mode="paper") -> ApprovalResult:
        return ApprovalResult(status="approved", intent_id=intent.intent_id)


class QueuedApprovalGate:
    def __init__(
        self,
        approval_repository=None,
        require_approval_modes=None,
        min_notional_for_approval: float = 0.0,
        timeout_seconds: int = 300,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.approval_repository = approval_repository or _InMemoryApprovalRepository()
        self.require_approval_modes = set(require_approval_modes or {"live"})
        self.min_notional_for_approval = float(min_notional_for_approval)
        self.timeout_seconds = int(timeout_seconds)
        self.clock = clock or _utcnow

    async def request(self, intent, account_snapshot=None, market_context=None, mode="paper") -> ApprovalResult:
        notional = _calculate_notional(intent)
        if mode not in self.require_approval_modes:
            return ApprovalResult(status="approved", intent_id=intent.intent_id)
        if notional < self.min_notional_for_approval:
            return ApprovalResult(status="approved", intent_id=intent.intent_id)

        now = self.clock()
        approval_id = str(uuid.uuid4())
        await self.approval_repository.create_pending(
            approval_id=approval_id,
            intent_id=intent.intent_id,
            mode=mode,
            created_at=now,
            expires_at=now + timedelta(seconds=self.timeout_seconds),
        )
        return ApprovalResult(status="pending", intent_id=intent.intent_id, approval_id=approval_id)

    async def list_pending(self):
        return await self.approval_repository.list_pending()

    async def approve(self, approval_id: str) -> ApprovalResult:
        pending = await self.approval_repository.resolve(approval_id, status="approved")
        return ApprovalResult(
            status="approved",
            intent_id=pending.intent_id,
            approval_id=pending.approval_id,
        )

    async def reject(self, approval_id: str, reason: str = "") -> ApprovalResult:
        pending = await self.approval_repository.resolve(approval_id, status="rejected", reason=reason)
        return ApprovalResult(
            status="rejected",
            intent_id=pending.intent_id,
            reason=reason,
            approval_id=pending.approval_id,
        )

    async def expire_pending(self, now: Optional[datetime] = None):
        return await self.approval_repository.expire_pending(now or self.clock())


def _calculate_notional(intent) -> float:
    if intent.amount is not None:
        return float(intent.amount)
    if intent.quantity is None:
        return 0.0
    return float(intent.quantity) * float(intent.price_reference)
