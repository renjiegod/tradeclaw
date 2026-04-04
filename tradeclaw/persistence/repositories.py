from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select, update

from tradeclaw.persistence.errors import PersistenceError, RecordNotFoundError, StateConflictError
from tradeclaw.persistence.models import (
    AgentInstance,
    ApprovalRecord,
    SystemStateRecord,
    TraceEventRecord,
)


@dataclass(frozen=True)
class InstanceSnapshot:
    instance_id: str
    name: str
    template_id: str
    mode: str
    orchestrator_mode: str
    description: str
    data_provider: str | None
    status: str
    last_error: str
    watch_symbols: tuple[str, ...]
    execution_strategy: str
    account_id: str
    model_id: str
    settings: dict | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ApprovalSnapshot:
    approval_id: str
    intent_id: str
    mode: str
    status: str
    reason: str
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None


@dataclass(frozen=True)
class TraceEventSnapshot:
    sequence: int
    run_id: str
    phase: str
    payload: dict
    timestamp: datetime


_TRACE_APPEND_MAX_RETRIES = 8
_APPROVAL_RESOLVE_STATUSES = frozenset({"approved", "rejected"})
_POSTGRES_UNIQUE_SQLSTATE = "23505"
_MYSQL_DUPLICATE_ENTRY_ERROR_CODE = 1062
_SQLITE_UNIQUE_ERROR_NAMES = frozenset(
    {
        "SQLITE_CONSTRAINT_PRIMARYKEY",
        "SQLITE_CONSTRAINT_UNIQUE",
    }
)

_MISSING = object()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _instance_snapshot(record: AgentInstance) -> InstanceSnapshot:
    symbols = record.watch_symbols or []
    return InstanceSnapshot(
        instance_id=record.instance_id,
        name=record.name,
        template_id=record.template_id,
        mode=record.mode,
        orchestrator_mode=record.orchestrator_mode,
        description=record.description,
        data_provider=record.data_provider,
        status=record.status,
        last_error=record.last_error,
        watch_symbols=tuple(str(s) for s in symbols),
        execution_strategy=record.execution_strategy,
        account_id=record.account_id,
        model_id=record.model_id,
        settings=dict(record.settings) if record.settings is not None else None,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _approval_snapshot(record: ApprovalRecord) -> ApprovalSnapshot:
    return ApprovalSnapshot(
        approval_id=record.approval_id,
        intent_id=record.intent_id,
        mode=record.mode,
        status=record.status,
        reason=record.reason,
        created_at=record.created_at,
        expires_at=record.expires_at,
        resolved_at=record.resolved_at,
    )


def _trace_event_snapshot(record: TraceEventRecord) -> TraceEventSnapshot:
    return TraceEventSnapshot(
        sequence=record.sequence,
        run_id=record.run_id,
        phase=record.phase,
        payload=dict(record.payload),
        timestamp=record.timestamp,
    )


def _integrity_message(error: IntegrityError) -> str:
    return str(error.orig or error).lower()


def _is_unique_violation(error: IntegrityError) -> bool:
    # SQLAlchemy wraps driver-native exceptions, so check portable driver codes first
    # and fall back to message patterns for SQLite/MySQL/PostgreSQL adapters.
    original = error.orig or error
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if sqlstate == _POSTGRES_UNIQUE_SQLSTATE:
        return True

    sqlite_error_name = getattr(original, "sqlite_errorname", None)
    if sqlite_error_name in _SQLITE_UNIQUE_ERROR_NAMES:
        return True

    args = getattr(original, "args", ())
    if args and args[0] == _MYSQL_DUPLICATE_ENTRY_ERROR_CODE:
        return True

    message = _integrity_message(error)
    return (
        "unique constraint failed" in message
        or "duplicate key value violates unique constraint" in message
        or "duplicate entry" in message
    )


def _constraint_conflict(message: str) -> StateConflictError:
    return StateConflictError(message)


def _persistence_error(message: str) -> PersistenceError:
    return PersistenceError(message)


class SqlAlchemyInstanceRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_instance(self, **kwargs) -> InstanceSnapshot:
        kwargs.setdefault("watch_symbols", [])
        kwargs.setdefault("execution_strategy", "")
        kwargs.setdefault("account_id", "")
        kwargs.setdefault("model_id", "")
        kwargs.setdefault("settings", None)
        if kwargs["settings"] is not None:
            kwargs["settings"] = dict(kwargs["settings"])
        kwargs["watch_symbols"] = list(kwargs["watch_symbols"] or [])
        async with self.session_factory() as session:
            record = AgentInstance(**kwargs)
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    message = _integrity_message(error)
                    if "instances.name" in message:
                        raise _constraint_conflict(
                            f"instance name already exists: {kwargs['name']}",
                        ) from error
                    raise _constraint_conflict(
                        f"instance already exists: {kwargs['instance_id']}",
                    ) from error
                raise _persistence_error("failed to create instance") from error
            return _instance_snapshot(record)

    async def update_status(self, instance_id: str, status: str, last_error: str) -> InstanceSnapshot:
        async with self.session_factory() as session:
            await session.execute(
                update(AgentInstance)
                .where(AgentInstance.instance_id == instance_id)
                .values(status=status, last_error=last_error, updated_at=_utcnow())
            )
            result = await session.execute(
                select(AgentInstance).where(AgentInstance.instance_id == instance_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                await session.rollback()
                raise RecordNotFoundError(f"instance not found: {instance_id}")
            await session.commit()
            return _instance_snapshot(record)

    async def list_instances(self) -> list[InstanceSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(AgentInstance).order_by(AgentInstance.created_at, AgentInstance.instance_id)
            )
            return [_instance_snapshot(record) for record in result.scalars().all()]

    async def get_instance(self, identifier: str) -> InstanceSnapshot:
        async with self.session_factory() as session:
            record = await session.get(AgentInstance, identifier)
            if record is None:
                result = await session.execute(
                    select(AgentInstance).where(AgentInstance.name == identifier)
                )
                record = result.scalar_one_or_none()
            if record is None:
                raise RecordNotFoundError(f"instance not found: {identifier}")
            return _instance_snapshot(record)

    async def update_agent_config(
        self,
        instance_id: str,
        *,
        watch_symbols: list[str] | None = None,
        execution_strategy: str | None = None,
        account_id: str | None = None,
        model_id: str | None = None,
        settings: Any = _MISSING,
    ) -> InstanceSnapshot:
        async with self.session_factory() as session:
            record = await session.get(AgentInstance, instance_id)
            if record is None:
                raise RecordNotFoundError(f"instance not found: {instance_id}")

            if watch_symbols is not None:
                record.watch_symbols = list(watch_symbols)
            if execution_strategy is not None:
                record.execution_strategy = execution_strategy
            if account_id is not None:
                record.account_id = account_id
            if model_id is not None:
                record.model_id = model_id
            if settings is not _MISSING:
                record.settings = dict(settings) if settings is not None else None

            record.updated_at = _utcnow()
            await session.commit()
            return _instance_snapshot(record)


class SqlAlchemyApprovalRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_pending(
        self,
        approval_id: str,
        intent_id: str,
        mode: str,
        created_at: datetime,
        expires_at: datetime,
    ):
        async with self.session_factory() as session:
            record = ApprovalRecord(
                approval_id=approval_id,
                intent_id=intent_id,
                mode=mode,
                status="pending",
                reason="",
                created_at=created_at,
                expires_at=expires_at,
                resolved_at=None,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    raise _constraint_conflict(
                        f"approval already exists: {approval_id}",
                    ) from error
                raise _persistence_error("failed to create approval") from error
            return _approval_snapshot(record)

    async def list_pending(self):
        async with self.session_factory() as session:
            result = await session.execute(
                select(ApprovalRecord)
                .where(ApprovalRecord.status == "pending")
                .order_by(ApprovalRecord.created_at, ApprovalRecord.approval_id)
            )
            return [_approval_snapshot(record) for record in result.scalars().all()]

    async def resolve(self, approval_id: str, status: str, reason: str = ""):
        if status not in _APPROVAL_RESOLVE_STATUSES:
            raise _persistence_error(f"invalid approval resolution status: {status}")

        async with self.session_factory() as session:
            resolved_at = _utcnow()
            result = await session.execute(
                update(ApprovalRecord)
                .where(
                    ApprovalRecord.approval_id == approval_id,
                    ApprovalRecord.status == "pending",
                )
                .values(status=status, reason=reason, resolved_at=resolved_at)
            )
            if result.rowcount:
                record = await session.get(ApprovalRecord, approval_id)
                await session.commit()
                return _approval_snapshot(record)

            existing_status = await session.scalar(
                select(ApprovalRecord.status).where(ApprovalRecord.approval_id == approval_id)
            )
            if existing_status is None:
                await session.rollback()
                raise RecordNotFoundError(f"approval not found: {approval_id}")
            await session.rollback()
            raise StateConflictError(f"approval already resolved: {approval_id}")

    async def expire_pending(self, now: datetime):
        async with self.session_factory() as session:
            approval_ids = list(
                (
                    await session.scalars(
                        select(ApprovalRecord.approval_id)
                        .where(ApprovalRecord.status == "pending", ApprovalRecord.expires_at <= now)
                        .order_by(ApprovalRecord.created_at, ApprovalRecord.approval_id)
                    )
                ).all()
            )
            if not approval_ids:
                return []

            expired = []
            for approval_id in approval_ids:
                result = await session.execute(
                    update(ApprovalRecord)
                    .where(
                        ApprovalRecord.approval_id == approval_id,
                        ApprovalRecord.status == "pending",
                        ApprovalRecord.expires_at <= now,
                    )
                    .values(status="expired", reason="expired", resolved_at=now)
                )
                if result.rowcount:
                    record = await session.get(ApprovalRecord, approval_id)
                    expired.append(_approval_snapshot(record))
            await session.commit()
            return expired


class SqlAlchemyTraceEventRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def append_event(self, run_id: str, phase: str, payload: dict):
        for attempt in range(_TRACE_APPEND_MAX_RETRIES):
            async with self.session_factory() as session:
                max_sequence = await session.scalar(
                    select(func.max(TraceEventRecord.sequence)).where(TraceEventRecord.run_id == run_id)
                )
                record = TraceEventRecord(
                    run_id=run_id,
                    sequence=(max_sequence or 0) + 1,
                    phase=phase,
                    payload=dict(payload),
                )
                session.add(record)
                try:
                    await session.commit()
                except IntegrityError as error:
                    await session.rollback()
                    if not _is_unique_violation(error):
                        raise _persistence_error("failed to append trace event") from error
                    if attempt == _TRACE_APPEND_MAX_RETRIES - 1:
                        raise _constraint_conflict(
                            f"trace event sequence allocation conflicted for run: {run_id}",
                        ) from error
                    continue
                return _trace_event_snapshot(record)

        raise StateConflictError(f"trace event sequence allocation conflicted for run: {run_id}")

    async def list_run_events(self, run_id: str):
        async with self.session_factory() as session:
            result = await session.execute(
                select(TraceEventRecord)
                .where(TraceEventRecord.run_id == run_id)
                .order_by(TraceEventRecord.sequence)
            )
            return [_trace_event_snapshot(record) for record in result.scalars().all()]


class SqlAlchemySystemStateRepository:
    _GLOBAL_KEY = "global"

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def get_kill_switch_enabled(self) -> bool:
        async with self.session_factory() as session:
            record = await session.get(SystemStateRecord, self._GLOBAL_KEY)
            if record is None:
                return False
            return record.kill_switch_enabled

    async def set_kill_switch_enabled(self, enabled: bool) -> bool:
        for attempt in range(2):
            async with self.session_factory() as session:
                record = await session.get(SystemStateRecord, self._GLOBAL_KEY)
                if record is None:
                    record = SystemStateRecord(
                        state_key=self._GLOBAL_KEY,
                        kill_switch_enabled=enabled,
                        updated_at=_utcnow(),
                    )
                    session.add(record)
                else:
                    record.kill_switch_enabled = enabled
                    record.updated_at = _utcnow()

                try:
                    await session.commit()
                except IntegrityError as error:
                    await session.rollback()
                    if _is_unique_violation(error) and attempt == 0:
                        continue
                    if _is_unique_violation(error):
                        raise _constraint_conflict("system state update conflicted") from error
                    raise _persistence_error("failed to update system state") from error
                return record.kill_switch_enabled

        raise StateConflictError("system state update conflicted")
