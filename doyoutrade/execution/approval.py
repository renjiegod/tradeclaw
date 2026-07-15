from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from doyoutrade.core.models import intent_to_json
from doyoutrade.debug import emit_debug_event
from doyoutrade.money.decimal_helpers import decimal_from_number, decimal_to_json_str
from doyoutrade.persistence.errors import RecordNotFoundError, StateConflictError


logger = logging.getLogger(__name__)


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
    # Intent-resume context — mirrors persistence.ApprovalSnapshot so the
    # in-memory repository behaves identically to the SQL one (no test/runtime
    # drift). All optional; populated by QueuedApprovalGate.request.
    intent_payload: Optional[str] = None
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    trace_id: Optional[str] = None
    account_id: Optional[str] = None
    symbol: Optional[str] = None
    action: Optional[str] = None
    notional: Optional[str] = None
    resolver_id: Optional[str] = None
    decision_source: Optional[str] = None
    decided_at: Optional[datetime] = None
    dispatched_at: Optional[datetime] = None
    dispatch_error: Optional[str] = None
    dispatch_attempts: Optional[int] = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _InMemoryApprovalRepository:
    def __init__(self):
        self._records: Dict[str, PendingApproval] = {}

    async def create_pending(
        self,
        approval_id: str,
        intent_id: str,
        mode: str,
        created_at: datetime,
        expires_at: datetime,
        *,
        intent_payload: Optional[str] = None,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        account_id: Optional[str] = None,
        symbol: Optional[str] = None,
        action: Optional[str] = None,
        notional: Optional[str] = None,
    ):
        if approval_id in self._records:
            raise StateConflictError(f"approval already exists: {approval_id}")
        record = PendingApproval(
            approval_id=approval_id,
            intent_id=intent_id,
            created_at=created_at,
            expires_at=expires_at,
            mode=mode,
            intent_payload=intent_payload,
            run_id=run_id,
            task_id=task_id,
            trace_id=trace_id,
            account_id=account_id,
            symbol=symbol,
            action=action,
            notional=notional,
            dispatch_attempts=0,
        )
        self._records[approval_id] = record
        return record

    async def list_pending(self) -> List[PendingApproval]:
        pending = [record for record in self._records.values() if record.status == "pending"]
        return sorted(pending, key=lambda item: item.created_at)

    async def list_approvals(
        self,
        *,
        statuses: Optional[List[str]] = None,
        symbol: Optional[str] = None,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        account_id: Optional[str] = None,
        decision_source: Optional[str] = None,
        search: Optional[str] = None,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[List[PendingApproval], int]:
        """In-memory mirror of the SQL repository's filtered history view.

        Same contract (newest-first, returns ``(page, total)``) so tests and the
        in-memory runtime behave identically to postgres — no test/runtime drift.
        """
        needle = search.strip().lower() if search and search.strip() else None

        def _match(record: PendingApproval) -> bool:
            if statuses and record.status not in statuses:
                return False
            if symbol and record.symbol != symbol:
                return False
            if task_id and record.task_id != task_id:
                return False
            if run_id and record.run_id != run_id:
                return False
            if account_id and record.account_id != account_id:
                return False
            if decision_source and record.decision_source != decision_source:
                return False
            if created_after is not None and (
                record.created_at is None or record.created_at < created_after
            ):
                return False
            if created_before is not None and (
                record.created_at is None or record.created_at > created_before
            ):
                return False
            if needle is not None:
                haystacks = [
                    record.approval_id,
                    record.intent_id,
                    record.symbol,
                    record.task_id,
                    record.run_id,
                ]
                if not any(value and needle in value.lower() for value in haystacks):
                    return False
            return True

        matched = [record for record in self._records.values() if _match(record)]
        matched.sort(
            key=lambda item: (item.created_at or datetime.min, item.approval_id),
            reverse=True,
        )
        total = len(matched)
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))
        return matched[safe_offset : safe_offset + safe_limit], total

    async def list_resumable(self) -> List[PendingApproval]:
        resumable = [
            record
            for record in self._records.values()
            if record.status == "approved" and record.dispatched_at is None
        ]
        return sorted(resumable, key=lambda item: item.created_at)

    async def resolve(
        self,
        approval_id: str,
        status: str,
        reason: str = "",
        *,
        resolver_id: Optional[str] = None,
        decision_source: Optional[str] = None,
    ) -> PendingApproval:
        record = self._records.get(approval_id)
        if record is None:
            raise RecordNotFoundError(f"approval not found: {approval_id}")
        if record.status != "pending":
            raise StateConflictError(f"approval already resolved: {approval_id}")
        now = _utcnow()
        record.status = status
        record.reason = reason
        record.resolved_at = now
        record.decided_at = now
        if resolver_id is not None:
            record.resolver_id = resolver_id
        if decision_source is not None:
            record.decision_source = decision_source
        return record

    async def mark_dispatched(self, approval_id: str, dispatched_at: datetime) -> bool:
        record = self._records.get(approval_id)
        if record is None or record.dispatched_at is not None:
            return False
        record.dispatched_at = dispatched_at
        record.dispatch_error = None
        return True

    async def mark_dispatch_failed(
        self,
        approval_id: str,
        error: str,
        *,
        abandon: bool = False,
        dispatched_at: Optional[datetime] = None,
    ) -> PendingApproval:
        record = self._records.get(approval_id)
        if record is None:
            raise RecordNotFoundError(f"approval not found: {approval_id}")
        record.dispatch_error = error
        record.dispatch_attempts = int(record.dispatch_attempts or 0) + 1
        if abandon:
            record.dispatched_at = dispatched_at or _utcnow()
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
    def request(
        self,
        intent,
        account_snapshot=None,
        market_context=None,
        mode="paper",
        *,
        cycle_state=None,
        **_: Any,
    ) -> ApprovalResult:
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

    async def request(
        self,
        intent,
        account_snapshot=None,
        market_context=None,
        mode="paper",
        *,
        cycle_state=None,
        min_notional_for_approval: Optional[float] = None,
        timeout_seconds: Optional[int] = None,
        account_id: Optional[str] = None,
    ) -> ApprovalResult:
        notional = _calculate_notional(intent)
        if mode not in self.require_approval_modes:
            return ApprovalResult(status="approved", intent_id=intent.intent_id)
        eff_min = (
            self.min_notional_for_approval
            if min_notional_for_approval is None
            else float(min_notional_for_approval)
        )
        if notional < decimal_from_number(eff_min):
            return ApprovalResult(status="approved", intent_id=intent.intent_id)

        eff_timeout = (
            self.timeout_seconds if timeout_seconds is None else int(timeout_seconds)
        )
        now = self.clock()
        approval_id = str(uuid.uuid4())
        # Persist the intent BODY + run context so the approved order can be
        # re-dispatched after this cycle ends (the in-memory OrderIntent is gone
        # by then). intent serialization failing must be loud — a pending with
        # no payload could never resume (§错误可见性).
        run_id = getattr(cycle_state, "run_id", None)
        trace_id = getattr(cycle_state, "trace_id", None)
        task_id = getattr(cycle_state, "task_id", None)
        notional_str = decimal_to_json_str(notional)
        await self.approval_repository.create_pending(
            approval_id=approval_id,
            intent_id=intent.intent_id,
            mode=mode,
            created_at=now,
            expires_at=now + timedelta(seconds=eff_timeout),
            intent_payload=intent_to_json(intent),
            run_id=run_id,
            task_id=task_id,
            trace_id=trace_id,
            account_id=account_id,
            symbol=getattr(intent, "symbol", None),
            action=getattr(intent, "action", None),
            notional=notional_str,
        )
        await emit_debug_event(
            "approval_pending_persisted",
            {
                "approval_id": approval_id,
                "intent_id": intent.intent_id,
                "run_id": run_id,
                "task_id": task_id,
                "account_id": account_id,
                "symbol": getattr(intent, "symbol", None),
                "action": getattr(intent, "action", None),
                "notional": notional_str,
                "mode": mode,
                "expires_in_seconds": eff_timeout,
                "hint": (
                    "Order held for human approval. Approve via the Feishu card / "
                    "web Approvals page; the scheduler resume sweep then dispatches "
                    "it through the same submit path as an in-cycle order."
                ),
            },
        )
        logger.info(
            "approval pending persisted approval_id=%s intent_id=%s run_id=%s "
            "task_id=%s symbol=%s action=%s notional=%s mode=%s",
            approval_id,
            intent.intent_id,
            run_id,
            task_id,
            getattr(intent, "symbol", None),
            getattr(intent, "action", None),
            notional_str,
            mode,
        )
        return ApprovalResult(status="pending", intent_id=intent.intent_id, approval_id=approval_id)

    async def list_pending(self):
        return await self.approval_repository.list_pending()

    async def list_approvals(
        self,
        *,
        statuses: Optional[List[str]] = None,
        symbol: Optional[str] = None,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        account_id: Optional[str] = None,
        decision_source: Optional[str] = None,
        search: Optional[str] = None,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        limit: int = 50,
        offset: int = 0,
    ):
        """Pass-through to the repository's filtered history view.

        Mirrors how ``list_pending`` delegates, so the API talks to the gate (not
        the repository) for both the pending queue and the full history.
        """
        return await self.approval_repository.list_approvals(
            statuses=statuses,
            symbol=symbol,
            task_id=task_id,
            run_id=run_id,
            account_id=account_id,
            decision_source=decision_source,
            search=search,
            created_after=created_after,
            created_before=created_before,
            limit=limit,
            offset=offset,
        )

    async def list_resumable(self):
        return await self.approval_repository.list_resumable()

    async def mark_dispatched(self, approval_id: str, dispatched_at: Optional[datetime] = None):
        return await self.approval_repository.mark_dispatched(
            approval_id, dispatched_at or self.clock()
        )

    async def mark_dispatch_failed(
        self, approval_id: str, error: str, *, abandon: bool = False
    ):
        return await self.approval_repository.mark_dispatch_failed(
            approval_id, error, abandon=abandon
        )

    async def approve(
        self,
        approval_id: str,
        *,
        resolver_id: Optional[str] = None,
        decision_source: Optional[str] = None,
    ) -> ApprovalResult:
        pending = await self.approval_repository.resolve(
            approval_id,
            status="approved",
            resolver_id=resolver_id,
            decision_source=decision_source,
        )
        await self._emit_resolved(pending, "approved", resolver_id, decision_source)
        return ApprovalResult(
            status="approved",
            intent_id=pending.intent_id,
            approval_id=pending.approval_id,
        )

    async def reject(
        self,
        approval_id: str,
        reason: str = "",
        *,
        resolver_id: Optional[str] = None,
        decision_source: Optional[str] = None,
    ) -> ApprovalResult:
        pending = await self.approval_repository.resolve(
            approval_id,
            status="rejected",
            reason=reason,
            resolver_id=resolver_id,
            decision_source=decision_source,
        )
        await self._emit_resolved(pending, "rejected", resolver_id, decision_source)
        return ApprovalResult(
            status="rejected",
            intent_id=pending.intent_id,
            reason=reason,
            approval_id=pending.approval_id,
        )

    @staticmethod
    async def _emit_resolved(pending, decision, resolver_id, decision_source):
        await emit_debug_event(
            "approval_resolved",
            {
                "approval_id": getattr(pending, "approval_id", None),
                "intent_id": getattr(pending, "intent_id", None),
                "run_id": getattr(pending, "run_id", None),
                "task_id": getattr(pending, "task_id", None),
                "symbol": getattr(pending, "symbol", None),
                "decision": decision,
                "resolver_id": resolver_id,
                "decision_source": decision_source,
                "hint": (
                    "approved orders are dispatched by the scheduler resume sweep; "
                    "rejected/expired orders are never dispatched."
                ),
            },
        )
        logger.info(
            "approval resolved approval_id=%s intent_id=%s decision=%s "
            "resolver_id=%s source=%s",
            getattr(pending, "approval_id", None),
            getattr(pending, "intent_id", None),
            decision,
            resolver_id,
            decision_source,
        )

    async def expire_pending(self, now: Optional[datetime] = None):
        expired = await self.approval_repository.expire_pending(now or self.clock())
        for record in expired or []:
            await emit_debug_event(
                "approval_expired",
                {
                    "approval_id": getattr(record, "approval_id", None),
                    "intent_id": getattr(record, "intent_id", None),
                    "run_id": getattr(record, "run_id", None),
                    "task_id": getattr(record, "task_id", None),
                    "symbol": getattr(record, "symbol", None),
                    "created_at": _iso_or_none(getattr(record, "created_at", None)),
                    "expires_at": _iso_or_none(getattr(record, "expires_at", None)),
                    "hint": (
                        "pending order timed out without a decision; it is NOT "
                        "dispatched. The strategy will re-propose it on a later "
                        "cycle if the signal still holds."
                    ),
                },
            )
            logger.info(
                "approval expired approval_id=%s intent_id=%s symbol=%s",
                getattr(record, "approval_id", None),
                getattr(record, "intent_id", None),
                getattr(record, "symbol", None),
            )
        return expired


def _calculate_notional(intent):
    return intent.quote_notional_decimal()


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None
