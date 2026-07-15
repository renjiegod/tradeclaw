"""Repository for assistant job watches (background-job completion wake-ups).

Kept in its own module (not ``repositories.py``) because the consumer set is
small and assistant-scoped: the in-process ``watch_job`` tool writes rows,
``doyoutrade/assistant/job_watcher.py`` polls and resolves them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from doyoutrade.persistence.models import AssistantJobWatchRecord


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _watch_to_dict(rec: AssistantJobWatchRecord) -> dict[str, Any]:
    return {
        "watch_id": rec.watch_id,
        "session_id": rec.session_id,
        "agent_id": rec.agent_id,
        "job_kind": rec.job_kind,
        "job_id": rec.job_id,
        "note": rec.note,
        "status": rec.status,
        "last_error": rec.last_error,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "updated_at": rec.updated_at.isoformat() if rec.updated_at else None,
        "fired_at": rec.fired_at.isoformat() if rec.fired_at else None,
    }


class SqlAlchemyAssistantJobWatchRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create(
        self,
        *,
        session_id: str,
        agent_id: str,
        job_id: str,
        job_kind: str = "backtest",
        note: str | None = None,
    ) -> dict[str, Any]:
        record = AssistantJobWatchRecord(
            watch_id=f"wjob-{uuid4().hex[:12]}",
            session_id=session_id,
            agent_id=agent_id,
            job_kind=job_kind,
            job_id=job_id,
            note=note,
            status="pending",
        )
        async with self.session_factory() as session:
            session.add(record)
            await session.commit()
            return _watch_to_dict(record)

    async def get(self, watch_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            rec = await session.get(AssistantJobWatchRecord, watch_id)
            return _watch_to_dict(rec) if rec else None

    async def list_pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(AssistantJobWatchRecord)
                .where(AssistantJobWatchRecord.status == "pending")
                .order_by(AssistantJobWatchRecord.created_at.asc())
                .limit(limit)
            )
            return [_watch_to_dict(rec) for rec in result.scalars().all()]

    async def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(AssistantJobWatchRecord)
                .where(AssistantJobWatchRecord.session_id == session_id)
                .order_by(AssistantJobWatchRecord.created_at.asc())
            )
            return [_watch_to_dict(rec) for rec in result.scalars().all()]

    async def resolve(
        self,
        watch_id: str,
        *,
        status: str,
        last_error: str | None = None,
    ) -> dict[str, Any] | None:
        """Transition a watch out of ``pending`` (fired / failed / cancelled)."""
        if status not in ("fired", "failed", "cancelled"):
            raise ValueError(
                f"watch resolution status must be fired/failed/cancelled, got {status!r}"
            )
        async with self.session_factory() as session:
            rec = await session.get(AssistantJobWatchRecord, watch_id)
            if rec is None:
                return None
            rec.status = status
            rec.last_error = last_error
            rec.updated_at = _utcnow()
            if status == "fired":
                rec.fired_at = _utcnow()
            await session.commit()
            return _watch_to_dict(rec)
