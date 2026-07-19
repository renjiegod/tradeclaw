from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy import and_, delete, func, or_, select, true, update

from pathlib import Path

from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.diagnostics import runtime_diag
from doyoutrade.persistence.strategy_storage import StrategyStorage, VersionNotFound
from doyoutrade.models.route_settings_validate import validate_route_settings
from doyoutrade.persistence.errors import PersistenceError, RecordNotFoundError, StateConflictError
from doyoutrade.persistence.models import (
    AccountRecord,
    AssistantLoadedSkillRecord,
    CachedBarRangeRecord,
    CachedBarRecord,
    CachedBarSuspensionRecord,
    Task,
    Run,
    ApprovalRecord,
    CronJobRecord,
    CronJobRunRecord,
    CycleRunRecord,
    DebugSessionEventRecord,
    DebugSessionRecord,
    DebugSessionSpanRecord,
    DecisionSignalOutcomeRecord,
    DecisionSignalRecord,
    InstrumentCatalog,
    KnowledgeGraphChangeOperationRecord,
    KnowledgeGraphChangeSetRecord,
    KnowledgeGraphEdgeRecord,
    KnowledgeGraphNodeRecord,
    KnowledgeGraphRevisionRecord,
    KnowledgeGraphSourceStateRecord,
    KnowledgeGraphStateRecord,
    MarketBarRecord,
    MarketBarSyncStateRecord,
    ModelInvocationRecord,
    ModelRoute,
    MonitorAlertRecord,
    MonitorRuleRecord,
    StrategyDefinitionRecord,
    SystemStateRecord,
    TaskTrigger,
    TradeFillRecord,
    WatchlistRecord,
)

_LOG = logging.getLogger(__name__)

# OpenTelemetry span_id is a 64-bit integer; SQLAlchemy String columns used to receive
# str(int) (decimal). Debug/export paths use 16-char lowercase hex — normalize storage
# and accept both forms when looking up by span id.
_SPAN_ID_HEX = re.compile(r"^[0-9a-fA-F]{1,16}$")
_OTEL_SPAN_MASK = (1 << 64) - 1


def _coerce_otel_span_id_for_storage(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return format(value & _OTEL_SPAN_MASK, "016x")
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if _SPAN_ID_HEX.fullmatch(s):
            return format(int(s, 16), "016x")
        if s.isdigit():
            return format(int(s) & _OTEL_SPAN_MASK, "016x")
        return s
    return str(value)


def _model_invocation_span_id_lookup_variants(span_id: str) -> tuple[str, ...]:
    s = span_id.strip()
    seen: list[str] = []
    for candidate in (s, s.lower()):
        if candidate and candidate not in seen:
            seen.append(candidate)
    if _SPAN_ID_HEX.fullmatch(s):
        as_int = int(s, 16) & _OTEL_SPAN_MASK
        for candidate in (format(as_int, "016x"), str(as_int)):
            if candidate not in seen:
                seen.append(candidate)
    elif s.isdigit():
        as_int = int(s) & _OTEL_SPAN_MASK
        for candidate in (format(as_int, "016x"), str(as_int)):
            if candidate not in seen:
                seen.append(candidate)
    return tuple(seen)


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    name: str
    mode: str
    description: str
    data_provider: str | None
    status: str
    last_error: str
    universe: tuple[str, ...]
    execution_strategy: str
    account_id: str
    model_id: str
    enabled_skills: tuple[str, ...]
    settings: dict | None
    created_at: datetime
    updated_at: datetime
    # Persisted backtest summary (see ``doyoutrade.backtest.summary``); ``None``
    # for non-backtest tasks and for backtest tasks that have not finalized yet.
    backtest_summary: dict | None = None


@dataclass(frozen=True)
class TaskTriggerSnapshot:
    """Immutable view of a ``task_triggers`` row (a Task-owned schedule+intent+delivery)."""

    id: str
    task_id: str
    name: str
    enabled: bool
    status: str
    schedule_kind: str
    interval_seconds: int | None
    cron_expression: str | None
    timezone: str
    at_iso: str | None
    range_start: str | None
    range_end: str | None
    bar_interval: str | None
    trading_session: str | None
    delete_after_run: bool
    execution_intent: str
    delivery_json: dict | None
    last_fired_at: datetime | None
    next_fire_at: datetime | None
    last_run_id: str | None
    last_error: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MonitorRuleSnapshot:
    """Immutable view of a ``monitor_rules`` row (a standalone 盯盘规则)."""

    id: str
    name: str
    enabled: bool
    status: str
    scope_kind: str
    scope_json: dict
    condition_json: dict
    delivery_json: dict | None
    cooldown_seconds: int
    last_error: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MonitorAlertSnapshot:
    """Immutable view of a ``monitor_alerts`` row (one fired alert)."""

    id: int
    monitor_rule_id: str
    symbol: str
    condition_name: str
    transition_key: str
    triggered_at: datetime
    last_price: float | None
    limit_price: float | None
    diagnostics_json: dict
    run_id: str | None
    delivery_status: str
    delivered_at: datetime | None
    created_at: datetime


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
    # Intent-resume context (migration 20260614_01). Defaults keep legacy
    # construction sites and the in-memory path working unchanged.
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
    dispatch_attempts: int | None = None


@dataclass(frozen=True)
class DebugSessionSnapshot:
    session_id: str
    task_id: str
    status: str
    run_id: str | None
    error_message: str
    error_type: str | None
    traceback_tail: str | None
    config_overrides: dict | None
    input_overrides: dict | None
    effective_config: dict | None
    session_type: str  # NEW: "debug" | "scheduled" | "manual" | "cron"
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class DebugSessionEventSnapshot:
    sequence: int
    session_id: str
    event_type: str
    payload: dict
    timestamp: datetime


@dataclass(frozen=True)
class StrategyDefinitionSnapshot:
    definition_id: str
    name: str
    current_version: str | None
    api_version: str
    input_contract_json: dict | None
    parameter_schema_json: dict | None
    default_parameters_json: dict | None
    capabilities_json: dict | None
    provenance_json: dict | None
    code_hash: str
    generation_prompt: str
    generation_model: str
    generation_metadata_json: dict | None
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DebugSessionSpanSnapshot:
    span_id: str
    trace_id: str
    parent_span_id: str | None
    session_id: str
    name: str
    span_type: str
    start_time: datetime
    end_time: datetime | None
    duration_ms: float | None
    attributes: dict
    status: str
    span_source: str  # NEW: "debug" | "scheduled" | "manual" | "cron"


@dataclass(frozen=True)
class ModelRouteRecord:
    """Self-contained model config (merged former provider + route)."""

    id: str
    route_name: str
    provider_kind: str
    base_url: str | None
    api_key: str
    target_model: str | None
    settings: dict | None
    created_at: datetime
    updated_at: datetime


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


def _to_naive_utc(value: datetime | None) -> datetime | None:
    """Project an aware datetime onto naive UTC so PostgreSQL
    ``TIMESTAMP WITHOUT TIME ZONE`` columns accept it.

    Callers in this codebase consistently produce ``datetime.now(timezone.utc)``
    when writing to ``cron_job_runs`` / ``cron_jobs``, but those tables
    declare ``DateTime`` (naive) — asyncpg then refuses the parameter with
    "can't subtract offset-naive and offset-aware datetimes". Normalizing
    at the repo boundary keeps the call sites simple (they may continue
    to emit aware UTC) and matches the convention already used by
    :func:`_utcnow`."""

    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _coerce_naive_utc_dt(value: Any) -> datetime | None:
    """Like :func:`_to_naive_utc` but also accept ISO-8601 strings.

    ``_cron_job_dict`` serializes ``last_run_at`` / ``created_at`` /
    ``updated_at`` to ISO strings for the JSON API. When that dict is fed
    back into ``upsert_job`` (``merged = {**existing, **updates}``), those
    string values must be re-parsed to ``datetime`` — asyncpg rejects a
    ``str`` bound to a ``TIMESTAMP`` column ("expected a datetime ... got
    'str'"), which silently worked on SQLite but 500s on PostgreSQL.
    """

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        value = datetime.fromisoformat(text)
    if not isinstance(value, datetime):
        raise PersistenceError(
            f"expected datetime or ISO string for timestamp column, "
            f"got {type(value).__name__}: {value!r}"
        )
    return _to_naive_utc(value)


def _strategy_definition_id_from_settings(settings: dict | None) -> str | None:
    if not isinstance(settings, dict):
        return None
    strategy = settings.get("strategy")
    if isinstance(strategy, dict):
        definition_id = strategy.get("definition_id")
        if isinstance(definition_id, str):
            stripped = definition_id.strip()
            if stripped:
                return stripped
    # Legacy persisted settings keep a flat top-level key (pre-nested ``strategy`` block).
    flat = settings.get("strategy_definition_id")
    if isinstance(flat, str):
        stripped = flat.strip()
        if stripped:
            return stripped
    return None


def _sync_task_strategy_definition_id_column(record: Task) -> None:
    record.strategy_definition_id = _strategy_definition_id_from_settings(record.settings)


def _task_snapshot(record: Task) -> TaskSnapshot:
    settings = dict(record.settings) if record.settings is not None else {}
    uni = list(record.universe) if record.universe else []
    if not uni and isinstance(settings.get("universe"), list):
        uni = list(settings["universe"])
    enabled = record.enabled_skills if record.enabled_skills else []
    return TaskSnapshot(
        task_id=record.task_id,
        name=record.name,
        mode=str(record.mode),
        description=record.description,
        data_provider=record.data_provider,
        status=str(record.status),
        last_error=str(record.last_error or ""),
        universe=tuple(str(s) for s in uni),
        execution_strategy=settings.get("execution_strategy") if settings else (record.execution_strategy if hasattr(record, 'execution_strategy') else ""),
        account_id=settings.get("account_id") if settings else (record.account_id if hasattr(record, 'account_id') else ""),
        model_id=settings.get("model_id") if settings else (record.model_id if hasattr(record, 'model_id') else ""),
        enabled_skills=tuple(str(s) for s in enabled),
        settings=settings if settings is not None else None,
        created_at=record.created_at,
        updated_at=record.updated_at,
        backtest_summary=(
            dict(record.backtest_summary)
            if isinstance(record.backtest_summary, dict)
            else None
        ),
    )


def _task_trigger_snapshot(record: TaskTrigger) -> TaskTriggerSnapshot:
    return TaskTriggerSnapshot(
        id=record.id,
        task_id=record.task_id,
        name=str(record.name or ""),
        enabled=bool(record.enabled),
        status=str(record.status),
        schedule_kind=str(record.schedule_kind),
        interval_seconds=record.interval_seconds,
        cron_expression=record.cron_expression,
        timezone=str(record.timezone or "UTC"),
        at_iso=record.at_iso,
        range_start=record.range_start,
        range_end=record.range_end,
        bar_interval=record.bar_interval,
        trading_session=record.trading_session,
        delete_after_run=bool(record.delete_after_run),
        execution_intent=str(record.execution_intent),
        delivery_json=dict(record.delivery_json) if isinstance(record.delivery_json, dict) else None,
        last_fired_at=record.last_fired_at,
        next_fire_at=record.next_fire_at,
        last_run_id=record.last_run_id,
        last_error=str(record.last_error or ""),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _monitor_rule_snapshot(record: MonitorRuleRecord) -> MonitorRuleSnapshot:
    return MonitorRuleSnapshot(
        id=record.id,
        name=str(record.name or ""),
        enabled=bool(record.enabled),
        status=str(record.status),
        scope_kind=str(record.scope_kind),
        scope_json=dict(record.scope_json) if isinstance(record.scope_json, dict) else {},
        condition_json=dict(record.condition_json) if isinstance(record.condition_json, dict) else {},
        delivery_json=dict(record.delivery_json) if isinstance(record.delivery_json, dict) else None,
        cooldown_seconds=int(record.cooldown_seconds or 0),
        last_error=str(record.last_error or ""),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _monitor_alert_snapshot(record: MonitorAlertRecord) -> MonitorAlertSnapshot:
    return MonitorAlertSnapshot(
        id=int(record.id),
        monitor_rule_id=record.monitor_rule_id,
        symbol=str(record.symbol),
        condition_name=str(record.condition_name),
        transition_key=str(record.transition_key or ""),
        triggered_at=record.triggered_at,
        last_price=record.last_price,
        limit_price=record.limit_price,
        diagnostics_json=dict(record.diagnostics_json) if isinstance(record.diagnostics_json, dict) else {},
        run_id=record.run_id,
        delivery_status=str(record.delivery_status or "pending"),
        delivered_at=record.delivered_at,
        created_at=record.created_at,
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
        intent_payload=record.intent_payload,
        run_id=record.run_id,
        task_id=record.task_id,
        trace_id=record.trace_id,
        account_id=record.account_id,
        symbol=record.symbol,
        action=record.action,
        notional=record.notional,
        resolver_id=record.resolver_id,
        decision_source=record.decision_source,
        decided_at=record.decided_at,
        dispatched_at=record.dispatched_at,
        dispatch_error=record.dispatch_error,
        dispatch_attempts=record.dispatch_attempts,
    )


def _debug_session_snapshot(record: DebugSessionRecord) -> DebugSessionSnapshot:
    return DebugSessionSnapshot(
        session_id=record.session_id,
        task_id=record.task_id,
        status=record.status,
        run_id=record.run_id,
        error_message=record.error_message,
        error_type=record.error_type,
        traceback_tail=record.traceback_tail,
        config_overrides=dict(record.config_overrides) if record.config_overrides is not None else None,
        input_overrides=dict(record.input_overrides) if record.input_overrides is not None else None,
        effective_config=dict(record.effective_config) if record.effective_config is not None else None,
        session_type=record.session_type,  # NEW
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def _debug_session_event_snapshot(record: DebugSessionEventRecord) -> DebugSessionEventSnapshot:
    return DebugSessionEventSnapshot(
        sequence=record.sequence,
        session_id=record.session_id,
        event_type=record.event_type,
        payload=dict(record.payload),
        timestamp=record.timestamp,
    )


def _debug_session_span_snapshot(record: DebugSessionSpanRecord) -> DebugSessionSpanSnapshot:
    return DebugSessionSpanSnapshot(
        span_id=record.span_id,
        trace_id=record.trace_id,
        parent_span_id=record.parent_span_id,
        session_id=record.session_id,
        name=record.name,
        span_type=record.span_type,
        start_time=record.start_time,
        end_time=record.end_time,
        duration_ms=record.duration_ms,
        attributes=dict(record.attributes) if record.attributes else {},
        status=record.status,
        span_source=record.span_source,  # NEW
    )


def _model_route_record(row: ModelRoute) -> ModelRouteRecord:
    return ModelRouteRecord(
        id=row.id,
        route_name=row.route_name,
        provider_kind=row.provider_kind,
        base_url=row.base_url,
        api_key=row.api_key,
        target_model=row.target_model,
        settings=dict(row.settings) if row.settings is not None else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
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


def _cron_job_effective_status(record: CronJobRecord) -> str:
    """Single source of truth for a cron job's user-visible status badge.

    ``last_status`` only gets a value after the first fire writes one (see
    ``cron_manager._execute`` / ``_execute_task_pipeline``), so the column
    is ``NULL`` for freshly-created and never-fired jobs. The list page
    needs to distinguish that "never fired yet" case from "paused", which
    is purely a function of ``enabled`` — derive it here so every API
    consumer (web UI, CLI, automation scripts) gets the same view.
    """

    if record.last_status:
        return record.last_status
    return "waiting" if bool(record.enabled) else "paused"


def _cron_job_dict(record: CronJobRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "agent_id": record.agent_id,
        "name": record.name,
        "cron_expression": record.cron_expression,
        "timezone": record.timezone,
        "schedule_kind": record.schedule_kind,
        "at_iso": record.at_iso,
        "delete_after_run": bool(record.delete_after_run),
        "enabled": bool(record.enabled),
        "input_template": record.input_template,
        "max_concurrency": record.max_concurrency,
        "timeout_seconds": record.timeout_seconds,
        "pre_action": record.pre_action,
        "task_kind": record.task_kind,
        "task_params_json": record.task_params_json,
        "last_run_at": record.last_run_at.isoformat() if record.last_run_at else None,
        "last_run_session_id": record.last_run_session_id,
        "last_status": record.last_status,
        "last_error": record.last_error,
        # Derived: ``last_status`` if a fire has stamped one, otherwise
        # ``waiting`` (enabled, never fired) / ``paused`` (disabled).
        # Kept alongside the raw ``last_status`` so existing consumers
        # that care about the on-disk value keep working unchanged.
        "effective_status": _cron_job_effective_status(record),
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


class SqlAlchemyInstrumentCatalogRepository:
    """CRUD and upsert for :class:`~doyoutrade.persistence.models.InstrumentCatalog`."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def _row_to_dict(r: InstrumentCatalog) -> dict[str, Any]:
        return {
            "symbol": r.symbol,
            "display_name": r.display_name,
            "market": r.market,
            "instrument_type": r.instrument_type,
            "is_tradable": r.is_tradable,
            "last_sync_source": r.last_sync_source,
            "last_sync_at": r.last_sync_at.isoformat() if r.last_sync_at else None,
            "raw": r.raw,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }

    async def list_page(
        self,
        *,
        q: str | None,
        limit: int,
        offset: int,
        tradable_only: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        from doyoutrade.data.instrument_catalog.search_match import (
            is_pinyin_style_query,
            matches_instrument_query,
        )

        qstrip = (q or "").strip()
        async with self.session_factory() as session:
            list_stmt = select(InstrumentCatalog).order_by(InstrumentCatalog.symbol)
            if tradable_only:
                list_stmt = list_stmt.where(InstrumentCatalog.is_tradable.is_(True))
            if not qstrip:
                count_stmt = select(func.count()).select_from(InstrumentCatalog)
                if tradable_only:
                    count_stmt = count_stmt.where(InstrumentCatalog.is_tradable.is_(True))
                total = int((await session.execute(count_stmt)).scalar_one())
                result = await session.execute(list_stmt.limit(limit).offset(offset))
                rows = result.scalars().all()
                return [self._row_to_dict(r) for r in rows], total

            if is_pinyin_style_query(qstrip):
                result = await session.execute(list_stmt)
                matched = [
                    r
                    for r in result.scalars().all()
                    if matches_instrument_query(
                        qstrip,
                        symbol=r.symbol,
                        display_name=r.display_name,
                    )
                ]
                total = len(matched)
                page = matched[offset : offset + limit]
                return [self._row_to_dict(r) for r in page], total

            like = f"%{qstrip.lower()}%"
            filt = or_(
                InstrumentCatalog.symbol.startswith(qstrip),
                func.lower(InstrumentCatalog.display_name).like(like),
            )
            count_stmt = select(func.count()).select_from(InstrumentCatalog).where(filt)
            if tradable_only:
                count_stmt = count_stmt.where(InstrumentCatalog.is_tradable.is_(True))
            total = int((await session.execute(count_stmt)).scalar_one())
            result = await session.execute(list_stmt.where(filt).limit(limit).offset(offset))
            rows = result.scalars().all()
        return [self._row_to_dict(r) for r in rows], total

    async def get(self, symbol: str) -> dict[str, Any] | None:
        key = symbol.strip()
        if not key:
            return None
        async with self.session_factory() as session:
            r = await session.get(InstrumentCatalog, key)
            if r is None:
                return None
            return self._row_to_dict(r)

    async def find_missing_symbols(self, symbols: list[str]) -> list[str]:
        uniq: list[str] = []
        for s in symbols:
            t = str(s).strip()
            if t and t not in uniq:
                uniq.append(t)
        if not uniq:
            return []
        async with self.session_factory() as session:
            result = await session.execute(
                select(InstrumentCatalog.symbol).where(InstrumentCatalog.symbol.in_(uniq))
            )
            have = {row[0] for row in result.all()}
        return [s for s in uniq if s not in have]

    async def find_non_tradable_symbols(self, symbols: list[str]) -> list[str]:
        # Returns symbols present in the catalog with is_tradable explicitly
        # False (e.g. seeded indices). Symbols absent from the catalog are NOT
        # returned here — they are surfaced by find_missing_symbols. NULL
        # is_tradable (unknown, e.g. some QMT-synced rows) is treated as
        # tradable so only the explicit non-tradable flag (the load-bearing
        # marker on index seeds) blocks order-generating universes.
        uniq: list[str] = []
        for s in symbols:
            t = str(s).strip()
            if t and t not in uniq:
                uniq.append(t)
        if not uniq:
            return []
        async with self.session_factory() as session:
            result = await session.execute(
                select(InstrumentCatalog.symbol).where(
                    InstrumentCatalog.symbol.in_(uniq),
                    InstrumentCatalog.is_tradable.is_(False),
                )
            )
            non_tradable = {row[0] for row in result.all()}
        return [s for s in uniq if s in non_tradable]

    async def upsert_rows(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        inserted = 0
        updated = 0
        async with self.session_factory() as session:
            for raw in rows:
                sym = str(raw.get("symbol") or "").strip()
                if not sym:
                    continue
                existing = await session.get(InstrumentCatalog, sym)
                now = _utcnow()
                display = raw.get("display_name")
                if display == "":
                    display = None
                if existing is None:
                    it_new = raw.get("instrument_type")
                    it_str = (str(it_new)[:64] if it_new is not None else None) or None
                    session.add(
                        InstrumentCatalog(
                            symbol=sym,
                            display_name=display,
                            market=raw.get("market"),
                            instrument_type=it_str,
                            is_tradable=raw.get("is_tradable"),
                            last_sync_source=str(raw.get("last_sync_source") or "akshare"),
                            last_sync_at=raw.get("last_sync_at") or now,
                            raw=raw.get("raw"),
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    inserted += 1
                else:
                    existing.display_name = display if display is not None else existing.display_name
                    if raw.get("market") is not None:
                        existing.market = raw.get("market")
                    if raw.get("instrument_type") is not None:
                        it = str(raw.get("instrument_type") or "")[:64]
                        existing.instrument_type = it or None
                    if raw.get("is_tradable") is not None:
                        existing.is_tradable = raw.get("is_tradable")
                    existing.last_sync_source = str(raw.get("last_sync_source") or existing.last_sync_source)
                    existing.last_sync_at = raw.get("last_sync_at") or now
                    if raw.get("raw") is not None:
                        existing.raw = raw.get("raw")
                    existing.updated_at = now
                    updated += 1
            await session.commit()
        return inserted, updated

    async def delete_symbols(self, symbols: list[str]) -> int:
        """Delete rows by canonical ``symbol``. Returns number of rows deleted."""
        uniq: list[str] = []
        for s in symbols:
            t = str(s).strip()
            if t and t not in uniq:
                uniq.append(t)
        if not uniq:
            return 0
        async with self.session_factory() as session:
            result = await session.execute(
                delete(InstrumentCatalog).where(InstrumentCatalog.symbol.in_(uniq))
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def delete_all(self) -> int:
        """Remove every row in ``instrument_catalog``."""
        async with self.session_factory() as session:
            result = await session.execute(delete(InstrumentCatalog))
            await session.commit()
            return int(result.rowcount or 0)


class SqlAlchemyTaskRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_task(self, **kwargs) -> TaskSnapshot:
        # Pop migrated fields; they go into settings, not separate kwargs for ORM.
        universe = kwargs.pop("universe", None)
        execution_strategy = kwargs.pop("execution_strategy", None)
        account_id = kwargs.pop("account_id", None)
        model_id = kwargs.pop("model_id", None)
        enabled_skills = kwargs.pop("enabled_skills", None)
        # These columns were dropped; discard to prevent ORM errors.
        kwargs.pop("template_id", None)
        kwargs.pop("orchestrator_mode", None)
        kwargs.pop("watch_symbols", None)

        # Build settings dict: start with existing, merge migrated fields.
        settings = kwargs.pop("settings", None)
        settings = dict(settings) if isinstance(settings, dict) else {}
        if universe is not None:
            settings["universe"] = list(universe) if universe else []
        if execution_strategy is not None:
            settings["execution_strategy"] = str(execution_strategy)
        if account_id is not None:
            settings["account_id"] = str(account_id)
        if model_id is not None:
            settings["model_id"] = str(model_id)
        if enabled_skills is not None:
            settings["enabled_skills"] = list(enabled_skills) if enabled_skills else []

        kwargs["settings"] = settings if settings else None
        kwargs["strategy_definition_id"] = _strategy_definition_id_from_settings(kwargs["settings"])
        # model_route_name is now stored only in settings (settings["model_route_name"])
        # Do NOT set watch_symbols/orchestrator_mode/execution_strategy/account_id/model_id as separate columns.
        async with self.session_factory() as session:
            record = Task(**kwargs)
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    raise _constraint_conflict(
                        f"task already exists: {kwargs.get('task_id', '')}",
                    ) from error
                raise _persistence_error(
                    f"failed to create task: {_integrity_message(error)}",
                ) from error
            return _task_snapshot(record)

    async def update_status(self, task_id: str, status: str, last_error: str) -> TaskSnapshot:
        async with self.session_factory() as session:
            await session.execute(
                update(Task)
                .where(Task.task_id == task_id)
                .values(status=status, last_error=last_error, updated_at=_utcnow())
            )
            result = await session.execute(
                select(Task).where(Task.task_id == task_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                await session.rollback()
                raise RecordNotFoundError(f"task not found: {task_id}")
            await session.commit()
            return _task_snapshot(record)

    async def update_backtest_summary_and_status(
        self,
        task_id: str,
        *,
        summary: dict[str, Any] | None,
        status: str,
        last_error: str | None = None,
    ) -> TaskSnapshot:
        """Persist a freshly-computed backtest summary and transition ``status``.

        Only ``mode == 'backtest'`` tasks accept a summary; calling this on a
        non-backtest task raises :class:`ValueError`. ``status`` is typically
        ``"completed"`` (success) or ``"error"`` (failure with partial summary).
        ``last_error`` is preserved when ``None`` is passed.
        """

        async with self.session_factory() as session:
            record = await session.get(Task, task_id)
            if record is None:
                raise RecordNotFoundError(f"task not found: {task_id}")
            if str(record.mode) != "backtest":
                raise ValueError(
                    f"task {task_id} mode={record.mode!r} cannot accept a backtest summary",
                )
            record.backtest_summary = dict(summary) if isinstance(summary, dict) else None
            record.status = status
            if last_error is not None:
                record.last_error = last_error
            record.updated_at = _utcnow()
            await session.commit()
            return _task_snapshot(record)

    async def delete_task(self, task_id: str) -> None:
        async with self.session_factory() as session:
            record = await session.get(Task, task_id)
            if record is None:
                raise RecordNotFoundError(f"task not found: {task_id}")
            run_rows = (
                await session.execute(
                    select(Run.run_id, Run.session_id).where(Run.task_id == task_id)
                )
            ).all()
            run_ids = {str(row[0]) for row in run_rows if row[0]}
            session_ids = {str(row[1]) for row in run_rows if row[1]}

            debug_session_rows = (
                await session.execute(
                    select(DebugSessionRecord.session_id, DebugSessionRecord.run_id).where(
                        DebugSessionRecord.task_id == task_id
                    )
                )
            ).all()
            session_ids.update(str(row[0]) for row in debug_session_rows if row[0])
            run_ids.update(str(row[1]) for row in debug_session_rows if row[1])

            cycle_rows = (
                await session.execute(
                    select(
                        CycleRunRecord.run_id,
                        CycleRunRecord.session_id,
                        CycleRunRecord.trace_id,
                    ).where(CycleRunRecord.task_id == task_id)
                )
            ).all()
            run_ids.update(str(row[0]) for row in cycle_rows if row[0])
            session_ids.update(str(row[1]) for row in cycle_rows if row[1])
            trace_ids = {str(row[2]) for row in cycle_rows if row[2]}

            if session_ids:
                span_trace_rows = (
                    await session.execute(
                        select(DebugSessionSpanRecord.trace_id).where(
                            DebugSessionSpanRecord.session_id.in_(tuple(session_ids))
                        )
                    )
                ).all()
                trace_ids.update(str(row[0]) for row in span_trace_rows if row[0])

            if session_ids:
                await session.execute(
                    delete(DebugSessionEventRecord).where(
                        DebugSessionEventRecord.session_id.in_(tuple(session_ids))
                    )
                )

            span_filters = []
            if session_ids:
                span_filters.append(DebugSessionSpanRecord.session_id.in_(tuple(session_ids)))
            if trace_ids:
                span_filters.append(DebugSessionSpanRecord.trace_id.in_(tuple(trace_ids)))
            if span_filters:
                await session.execute(delete(DebugSessionSpanRecord).where(or_(*span_filters)))

            invocation_filters = [ModelInvocationRecord.task_id == task_id]
            if run_ids:
                invocation_filters.append(ModelInvocationRecord.run_id.in_(tuple(run_ids)))
            if trace_ids:
                invocation_filters.append(ModelInvocationRecord.trace_id.in_(tuple(trace_ids)))
            await session.execute(delete(ModelInvocationRecord).where(or_(*invocation_filters)))

            await session.execute(delete(TradeFillRecord).where(TradeFillRecord.task_id == task_id))
            await session.execute(delete(CycleRunRecord).where(CycleRunRecord.task_id == task_id))
            await session.execute(delete(Run).where(Run.task_id == task_id))
            await session.execute(delete(DebugSessionRecord).where(DebugSessionRecord.task_id == task_id))
            await session.delete(record)
            await session.commit()

    async def list_tasks(self) -> list[TaskSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Task).order_by(Task.created_at.desc(), Task.task_id.desc())
            )
            return [_task_snapshot(record) for record in result.scalars().all()]

    async def list_tasks_page(
        self,
        *,
        q: str | None,
        status: str | None,
        mode: str | None,
        limit: int,
        offset: int,
        definition_id: str | None = None,
        modes: list[str] | None = None,
    ) -> tuple[list[TaskSnapshot], int]:
        conditions = []
        q_strip = (q or "").strip()
        if q_strip:
            like = f"%{q_strip.lower()}%"
            conditions.append(
                or_(
                    Task.task_id.contains(q_strip),
                    func.lower(Task.name).like(like),
                )
            )
        status_strip = (status or "").strip()
        if status_strip:
            conditions.append(Task.status == status_strip)
        # ``modes`` (a set) takes precedence over the single ``mode`` so the UI can
        # group tabs ("trading" = paper/live/signal_only vs "backtest") in one query
        # while still allowing an exact single-mode sub-filter inside a tab.
        modes_clean = [m.strip() for m in (modes or []) if m and m.strip()]
        if modes_clean:
            conditions.append(Task.mode.in_(modes_clean))
        else:
            mode_strip = (mode or "").strip()
            if mode_strip:
                conditions.append(Task.mode == mode_strip)
        definition_id_strip = (definition_id or "").strip()
        if definition_id_strip:
            conditions.append(Task.strategy_definition_id == definition_id_strip)
        where_clause = and_(*conditions) if conditions else None
        async with self.session_factory() as session:
            count_stmt = select(func.count()).select_from(Task)
            list_stmt = select(Task).order_by(Task.created_at.desc(), Task.task_id.desc())
            if where_clause is not None:
                count_stmt = count_stmt.where(where_clause)
                list_stmt = list_stmt.where(where_clause)
            total = int((await session.execute(count_stmt)).scalar_one())
            rows = (await session.execute(list_stmt.limit(limit).offset(offset))).scalars().all()
            return ([_task_snapshot(record) for record in rows], total)

    async def get_task(self, identifier: str) -> TaskSnapshot:
        async with self.session_factory() as session:
            record = await session.get(Task, identifier)
            if record is None:
                raise RecordNotFoundError(f"task not found: {identifier}")
            return _task_snapshot(record)

    async def update_agent_config(
        self,
        task_id: str,
        *,
        universe: list[str] | None = None,
        execution_strategy: str | None = None,
        account_id: str | None = None,
        model_id: str | None = None,
        settings: Any = _MISSING,
    ) -> TaskSnapshot:
        # Read current settings as base, then merge in the updated fields.
        # During migration, field-specific args are written to settings (the new authority).
        async with self.session_factory() as session:
            record = await session.get(Task, task_id)
            if record is None:
                raise RecordNotFoundError(f"task not found: {task_id}")

            # Build the new settings dict from existing + field-specific args.
            new_settings = dict(record.settings) if record.settings else {}
            if universe is not None:
                new_settings["universe"] = list(universe)
                record.universe = list(universe)
            if execution_strategy is not None:
                new_settings["execution_strategy"] = str(execution_strategy)
                record.execution_strategy = str(execution_strategy)
            if account_id is not None:
                new_settings["account_id"] = str(account_id)
                record.account_id = str(account_id)
            if model_id is not None:
                new_settings["model_id"] = str(model_id)
                record.model_id = str(model_id)

            # settings=None explicitly means "clear settings to empty dict (not None)";
            # settings=_MISSING means "don't touch settings dict at all";
            # settings=dict means "replace settings with this dict".
            if settings is not _MISSING:
                record.settings = dict(settings) if settings is not None else {}
            else:
                record.settings = new_settings if new_settings else None
            _sync_task_strategy_definition_id_column(record)

            record.updated_at = _utcnow()
            await session.commit()
            return _task_snapshot(record)

    async def update_task(
        self,
        task_id: str,
        *,
        name: str | None = None,
        mode: str | None = None,
        description: str | None = None,
        data_provider: str | None = None,
        settings: Any = _MISSING,
    ) -> TaskSnapshot:
        """Full update of task fields. The migrated fields (execution_strategy,
        account_id, model_id, enabled_skills) are passed via settings dict."""
        async with self.session_factory() as session:
            record = await session.get(Task, task_id)
            if record is None:
                raise RecordNotFoundError(f"task not found: {task_id}")

            if name is not None:
                record.name = name
            if mode is not None:
                record.mode = mode
            if description is not None:
                record.description = description
            if data_provider is not None:
                record.data_provider = data_provider
            if settings is not _MISSING:
                # Merge migrated fields into settings (not separate columns).
                s = dict(settings) if isinstance(settings, dict) else {}
                record.settings = s if s else None
                un = s.get("universe") if isinstance(s.get("universe"), list) else None
                if un is not None:
                    record.universe = list(un)
                _sync_task_strategy_definition_id_column(record)

            record.updated_at = _utcnow()
            await session.commit()
            return _task_snapshot(record)


class SqlAlchemyTaskTriggerRepository:
    """CRUD for ``task_triggers`` (Task-owned schedule+intent+delivery rows).

    Dumb persistence: rich field validation (cron validity, schedule-kind field
    requirements, delivery-target resolution, next_fire_at computation) lives in
    ``doyoutrade.runtime.triggers`` so this layer never imports runtime/assistant.
    The only validation here is a strict-shape defensive guard so a malformed
    ``delivery_json`` raises on the asyncpg path instead of silently coercing to
    ``{}`` (CLAUDE.md §错误可见性 + the SQLite-vs-Postgres strictness gap).
    """

    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def _guard_delivery_json(value: object) -> None:
        if value is not None and not isinstance(value, dict):
            raise ValueError(
                f"delivery_json must be a dict or None, got {type(value).__name__}: {value!r}"
            )

    async def create_trigger(self, **kwargs) -> TaskTriggerSnapshot:
        trigger_id = kwargs.pop("id", None) or f"trg-{uuid.uuid4().hex[:12]}"
        self._guard_delivery_json(kwargs.get("delivery_json"))
        async with self.session_factory() as session:
            record = TaskTrigger(id=trigger_id, **kwargs)
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    raise _constraint_conflict(
                        f"task trigger already exists: {trigger_id}",
                    ) from error
                raise _persistence_error(
                    f"failed to create task trigger: {_integrity_message(error)}",
                ) from error
            return _task_trigger_snapshot(record)

    async def get_trigger(self, trigger_id: str) -> TaskTriggerSnapshot:
        async with self.session_factory() as session:
            record = await session.get(TaskTrigger, trigger_id)
            if record is None:
                raise RecordNotFoundError(f"task trigger not found: {trigger_id}")
            return _task_trigger_snapshot(record)

    async def list_for_task(self, task_id: str) -> list[TaskTriggerSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(TaskTrigger)
                .where(TaskTrigger.task_id == task_id)
                .order_by(TaskTrigger.created_at.asc())
            )
            return [_task_trigger_snapshot(r) for r in result.scalars().all()]

    async def list_schedulable(self) -> list[TaskTriggerSnapshot]:
        """Enabled + active triggers eligible for the wall-clock poll.

        The parent-task-running gate is applied by the scheduler against the
        in-memory RuntimeScheduler. ``backtest_range`` triggers are EXCLUDED — they
        are launched on demand via the backtest bar loop, never polled.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(TaskTrigger).where(
                    TaskTrigger.enabled.is_(True),
                    TaskTrigger.status == "active",
                    TaskTrigger.schedule_kind != "backtest_range",
                )
            )
            return [_task_trigger_snapshot(r) for r in result.scalars().all()]

    async def update_trigger(self, trigger_id: str, **fields) -> TaskTriggerSnapshot:
        """Patch semantics: only keys explicitly present in ``fields`` are written.

        Pass a key with value ``None`` to clear a nullable column.
        """
        if "delivery_json" in fields:
            self._guard_delivery_json(fields["delivery_json"])
        async with self.session_factory() as session:
            record = await session.get(TaskTrigger, trigger_id)
            if record is None:
                raise RecordNotFoundError(f"task trigger not found: {trigger_id}")
            for key, value in fields.items():
                setattr(record, key, value)
            record.updated_at = _utcnow()
            await session.commit()
            return _task_trigger_snapshot(record)

    async def record_fire(
        self,
        trigger_id: str,
        *,
        last_fired_at: datetime,
        next_fire_at: datetime | None,
        last_run_id: str | None,
        status: str | None = None,
        last_error: str = "",
    ) -> None:
        """Post-fire bookkeeping written by the TriggerScheduler.

        ``status`` transitions the trigger when supplied (e.g. ``'exhausted'`` for a
        spent one-shot, ``'error'`` on a failed fire); otherwise status is kept.
        ``last_error`` is always written ("" on success clears a prior error).
        """
        async with self.session_factory() as session:
            record = await session.get(TaskTrigger, trigger_id)
            if record is None:
                return
            record.last_fired_at = last_fired_at
            record.next_fire_at = next_fire_at
            if last_run_id:
                record.last_run_id = last_run_id
            if status is not None:
                record.status = status
            record.last_error = last_error or ""
            record.updated_at = _utcnow()
            await session.commit()

    async def delete_trigger(self, trigger_id: str) -> None:
        async with self.session_factory() as session:
            record = await session.get(TaskTrigger, trigger_id)
            if record is None:
                return
            await session.delete(record)
            await session.commit()


class SqlAlchemyMonitorRuleRepository:
    """CRUD for ``monitor_rules`` (standalone realtime 盯盘规则).

    Dumb persistence: condition-tree validity and delivery-target resolution live
    in ``doyoutrade.monitoring.conditions`` / ``doyoutrade.runtime.monitor_delivery``
    so this layer never imports runtime/monitoring. The only validation here is a
    strict-shape defensive guard so a malformed JSON column raises instead of
    silently coercing to ``{}`` (CLAUDE.md §错误可见性).
    """

    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def _guard_json_dict(name: str, value: object, *, allow_none: bool) -> None:
        if value is None:
            if allow_none:
                return
            raise ValueError(f"{name} must be a dict, got None")
        if not isinstance(value, dict):
            raise ValueError(
                f"{name} must be a dict{' or None' if allow_none else ''}, "
                f"got {type(value).__name__}: {value!r}"
            )

    async def create_rule(self, **kwargs) -> MonitorRuleSnapshot:
        rule_id = kwargs.pop("id", None) or f"mon-{uuid.uuid4().hex[:12]}"
        self._guard_json_dict("scope_json", kwargs.get("scope_json"), allow_none=False)
        self._guard_json_dict("condition_json", kwargs.get("condition_json"), allow_none=False)
        self._guard_json_dict("delivery_json", kwargs.get("delivery_json"), allow_none=True)
        async with self.session_factory() as session:
            record = MonitorRuleRecord(id=rule_id, **kwargs)
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    raise _constraint_conflict(
                        f"monitor rule already exists: {rule_id}",
                    ) from error
                raise _persistence_error(
                    f"failed to create monitor rule: {_integrity_message(error)}",
                ) from error
            return _monitor_rule_snapshot(record)

    async def get_rule(self, rule_id: str) -> MonitorRuleSnapshot:
        async with self.session_factory() as session:
            record = await session.get(MonitorRuleRecord, rule_id)
            if record is None:
                raise RecordNotFoundError(f"monitor rule not found: {rule_id}")
            return _monitor_rule_snapshot(record)

    async def list_rules(self) -> list[MonitorRuleSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(MonitorRuleRecord).order_by(MonitorRuleRecord.created_at.asc())
            )
            return [_monitor_rule_snapshot(r) for r in result.scalars().all()]

    async def list_active(self) -> list[MonitorRuleSnapshot]:
        """Enabled + active rules — the daemon's working set."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(MonitorRuleRecord).where(
                    MonitorRuleRecord.enabled.is_(True),
                    MonitorRuleRecord.status == "active",
                )
            )
            return [_monitor_rule_snapshot(r) for r in result.scalars().all()]

    async def update_rule(self, rule_id: str, **fields) -> MonitorRuleSnapshot:
        """Patch semantics: only keys explicitly present in ``fields`` are written."""
        if "scope_json" in fields:
            self._guard_json_dict("scope_json", fields["scope_json"], allow_none=False)
        if "condition_json" in fields:
            self._guard_json_dict("condition_json", fields["condition_json"], allow_none=False)
        if "delivery_json" in fields:
            self._guard_json_dict("delivery_json", fields["delivery_json"], allow_none=True)
        async with self.session_factory() as session:
            record = await session.get(MonitorRuleRecord, rule_id)
            if record is None:
                raise RecordNotFoundError(f"monitor rule not found: {rule_id}")
            for key, value in fields.items():
                setattr(record, key, value)
            record.updated_at = _utcnow()
            await session.commit()
            return _monitor_rule_snapshot(record)

    async def delete_rule(self, rule_id: str) -> None:
        async with self.session_factory() as session:
            record = await session.get(MonitorRuleRecord, rule_id)
            if record is None:
                return
            await session.delete(record)
            await session.commit()


class SqlAlchemyMonitorAlertRepository:
    """Append + read for ``monitor_alerts`` (fired alerts + durable dedup source)."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def insert_alert(self, **kwargs) -> MonitorAlertSnapshot:
        async with self.session_factory() as session:
            record = MonitorAlertRecord(**kwargs)
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise _persistence_error(
                    f"failed to insert monitor alert: {_integrity_message(error)}",
                ) from error
            await session.refresh(record)
            return _monitor_alert_snapshot(record)

    async def list_for_rule(
        self,
        rule_id: str,
        *,
        symbol: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[MonitorAlertSnapshot]:
        async with self.session_factory() as session:
            stmt = select(MonitorAlertRecord).where(
                MonitorAlertRecord.monitor_rule_id == rule_id
            )
            if symbol:
                stmt = stmt.where(MonitorAlertRecord.symbol == symbol)
            if since is not None:
                stmt = stmt.where(MonitorAlertRecord.triggered_at >= since)
            stmt = stmt.order_by(MonitorAlertRecord.triggered_at.desc()).limit(int(limit))
            result = await session.execute(stmt)
            return [_monitor_alert_snapshot(r) for r in result.scalars().all()]

    async def list_recent(
        self,
        *,
        symbol: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[MonitorAlertSnapshot]:
        async with self.session_factory() as session:
            stmt = select(MonitorAlertRecord)
            if symbol:
                stmt = stmt.where(MonitorAlertRecord.symbol == symbol)
            if since is not None:
                stmt = stmt.where(MonitorAlertRecord.triggered_at >= since)
            stmt = stmt.order_by(MonitorAlertRecord.triggered_at.desc()).limit(int(limit))
            result = await session.execute(stmt)
            return [_monitor_alert_snapshot(r) for r in result.scalars().all()]

    async def recent_for_dedup(
        self, monitor_rule_id: str, symbol: str, condition_name: str
    ) -> MonitorAlertSnapshot | None:
        """Latest alert for (rule, symbol, condition) — the daemon's cooldown probe."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(MonitorAlertRecord)
                .where(
                    MonitorAlertRecord.monitor_rule_id == monitor_rule_id,
                    MonitorAlertRecord.symbol == symbol,
                    MonitorAlertRecord.condition_name == condition_name,
                )
                .order_by(MonitorAlertRecord.triggered_at.desc())
                .limit(1)
            )
            record = result.scalar_one_or_none()
            return _monitor_alert_snapshot(record) if record is not None else None

    async def list_latest_per_dedup_key(self) -> list[MonitorAlertSnapshot]:
        """Most-recent alert across all (rule, symbol, condition) groups.

        Used once at daemon startup to rehydrate the cooldown floor so a restart
        inside a cooldown window still suppresses an immediate duplicate. Returns
        rows newest-first; the caller keeps the first seen per dedup key.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(MonitorAlertRecord).order_by(MonitorAlertRecord.triggered_at.desc())
            )
            seen: set[tuple[str, str, str]] = set()
            out: list[MonitorAlertSnapshot] = []
            for record in result.scalars().all():
                key = (record.monitor_rule_id, record.symbol, record.condition_name)
                if key in seen:
                    continue
                seen.add(key)
                out.append(_monitor_alert_snapshot(record))
            return out

    async def mark_delivered(
        self, alert_id: int, *, delivery_status: str, delivered_at: datetime | None
    ) -> None:
        async with self.session_factory() as session:
            record = await session.get(MonitorAlertRecord, alert_id)
            if record is None:
                return
            record.delivery_status = delivery_status
            record.delivered_at = delivered_at
            await session.commit()


class SqlAlchemyModelRouteRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def get_by_id(self, route_id: str) -> ModelRouteRecord:
        async with self.session_factory() as session:
            row = await session.get(ModelRoute, route_id)
            if row is None:
                raise RecordNotFoundError(f"model route not found: {route_id}")
            return _model_route_record(row)

    async def get_by_route_name(self, route_name: str) -> ModelRouteRecord:
        async with self.session_factory() as session:
            result = await session.execute(select(ModelRoute).where(ModelRoute.route_name == route_name))
            row = result.scalar_one_or_none()
            if row is None:
                raise RecordNotFoundError(f"model route not found: {route_name!r}")
            return _model_route_record(row)

    async def list_routes(self) -> list[ModelRouteRecord]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ModelRoute).order_by(ModelRoute.created_at, ModelRoute.id)
            )
            return [_model_route_record(r) for r in result.scalars().all()]

    async def create(
        self,
        *,
        route_name: str,
        provider_kind: str,
        api_key: str,
        base_url: str | None = None,
        target_model: str | None = None,
        settings: dict | None = None,
        id: str | None = None,
    ) -> ModelRouteRecord:
        rid = id or str(uuid.uuid4())
        validated_settings = validate_route_settings(settings)
        settings_stored = validated_settings if validated_settings else None
        async with self.session_factory() as session:
            row = ModelRoute(
                id=rid,
                route_name=route_name,
                provider_kind=provider_kind,
                base_url=base_url,
                api_key=api_key,
                target_model=target_model,
                settings=settings_stored,
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    raise _constraint_conflict(
                        f"model route name already exists: {route_name!r}",
                    ) from error
                raise _persistence_error("failed to create model route") from error
            return _model_route_record(row)

    async def update(
        self,
        route_id: str,
        *,
        route_name: str | object = _MISSING,
        provider_kind: str | object = _MISSING,
        base_url: Any = _MISSING,
        api_key: str | object = _MISSING,
        target_model: str | object = _MISSING,
        settings: Any = _MISSING,
    ) -> ModelRouteRecord:
        async with self.session_factory() as session:
            row = await session.get(ModelRoute, route_id)
            if row is None:
                raise RecordNotFoundError(f"model route not found: {route_id}")
            if route_name is not _MISSING:
                row.route_name = str(route_name)
            if provider_kind is not _MISSING:
                row.provider_kind = str(provider_kind)
            if base_url is not _MISSING:
                row.base_url = base_url
            if api_key is not _MISSING:
                row.api_key = str(api_key)
            if target_model is not _MISSING:
                row.target_model = target_model
            if settings is not _MISSING:
                if settings is None:
                    row.settings = None
                else:
                    validated = validate_route_settings(settings)
                    row.settings = validated if validated else None
            row.updated_at = _utcnow()
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    raise _constraint_conflict(
                        f"model route name already exists: {getattr(row, 'route_name', route_name)!r}",
                    ) from error
                raise _persistence_error("failed to update model route") from error
            await session.refresh(row)
            return _model_route_record(row)

    async def delete(self, route_id: str) -> None:
        async with self.session_factory() as session:
            row = await session.get(ModelRoute, route_id)
            if row is None:
                raise RecordNotFoundError(f"model route not found: {route_id}")
            await session.delete(row)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                msg = _integrity_message(error)
                if "foreign key" in msg or "constraint" in msg:
                    raise StateConflictError(
                        "cannot delete model route: still referenced by other rows",
                    ) from error
                raise _persistence_error("failed to delete model route") from error


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
        *,
        intent_payload: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
        account_id: str | None = None,
        symbol: str | None = None,
        action: str | None = None,
        notional: str | None = None,
    ):
        async with self.session_factory() as session:
            record = ApprovalRecord(
                approval_id=approval_id,
                intent_id=intent_id,
                mode=mode,
                status="pending",
                reason="",
                created_at=_to_naive_utc(created_at),
                expires_at=_to_naive_utc(expires_at),
                resolved_at=None,
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

    async def list_approvals(
        self,
        *,
        statuses: list[str] | None = None,
        symbol: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        account_id: str | None = None,
        decision_source: str | None = None,
        search: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ApprovalSnapshot], int]:
        """Filtered, paginated view over ALL approvals (pending + history).

        Unlike :meth:`list_pending` (only ``status='pending'``, oldest-first for
        the action queue), this powers the web Approvals page's full history:
        any status, newest-first. Returns ``(page_items, total_match_count)`` so
        the UI renders an accurate pager — ``total`` is the count BEFORE
        ``limit``/``offset`` are applied. ``search`` is a case-insensitive
        substring over approval_id / intent_id / symbol / task_id / run_id.
        """
        conditions = []
        if statuses:
            conditions.append(ApprovalRecord.status.in_(list(statuses)))
        if symbol:
            conditions.append(ApprovalRecord.symbol == symbol)
        if task_id:
            conditions.append(ApprovalRecord.task_id == task_id)
        if run_id:
            conditions.append(ApprovalRecord.run_id == run_id)
        if account_id:
            conditions.append(ApprovalRecord.account_id == account_id)
        if decision_source:
            conditions.append(ApprovalRecord.decision_source == decision_source)
        if created_after is not None:
            conditions.append(ApprovalRecord.created_at >= _to_naive_utc(created_after))
        if created_before is not None:
            conditions.append(ApprovalRecord.created_at <= _to_naive_utc(created_before))
        if search and search.strip():
            like = f"%{search.strip()}%"
            conditions.append(
                or_(
                    ApprovalRecord.approval_id.ilike(like),
                    ApprovalRecord.intent_id.ilike(like),
                    ApprovalRecord.symbol.ilike(like),
                    ApprovalRecord.task_id.ilike(like),
                    ApprovalRecord.run_id.ilike(like),
                )
            )
        # Clamp pagination to safe bounds: never an unbounded scan, never a
        # negative offset (a malformed query must not silently return garbage).
        safe_limit = max(1, min(int(limit), 500))
        safe_offset = max(0, int(offset))
        async with self.session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(ApprovalRecord).where(*conditions)
            )
            result = await session.execute(
                select(ApprovalRecord)
                .where(*conditions)
                .order_by(
                    ApprovalRecord.created_at.desc(),
                    ApprovalRecord.approval_id.desc(),
                )
                .limit(safe_limit)
                .offset(safe_offset)
            )
            items = [_approval_snapshot(record) for record in result.scalars().all()]
        return items, int(total or 0)

    async def resolve(
        self,
        approval_id: str,
        status: str,
        reason: str = "",
        *,
        resolver_id: str | None = None,
        decision_source: str | None = None,
    ):
        if status not in _APPROVAL_RESOLVE_STATUSES:
            raise _persistence_error(f"invalid approval resolution status: {status}")

        async with self.session_factory() as session:
            resolved_at = _utcnow()
            values: dict = {
                "status": status,
                "reason": reason,
                "resolved_at": resolved_at,
                "decided_at": resolved_at,
            }
            # Only overwrite audit columns when the caller supplied them so a
            # bare resolve() (tests, legacy callers) does not null them out.
            if resolver_id is not None:
                values["resolver_id"] = resolver_id
            if decision_source is not None:
                values["decision_source"] = decision_source
            result = await session.execute(
                update(ApprovalRecord)
                .where(
                    ApprovalRecord.approval_id == approval_id,
                    ApprovalRecord.status == "pending",
                )
                .values(**values)
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

    async def list_resumable(self):
        """Approved approvals not yet dispatched to an adapter, oldest first.

        These are the orders the human approved that still need to reach the
        broker (the scheduler's resume sweep consumes them).
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(ApprovalRecord)
                .where(
                    ApprovalRecord.status == "approved",
                    ApprovalRecord.dispatched_at.is_(None),
                )
                .order_by(ApprovalRecord.created_at, ApprovalRecord.approval_id)
            )
            return [_approval_snapshot(record) for record in result.scalars().all()]

    async def mark_dispatched(self, approval_id: str, dispatched_at: datetime):
        """Stamp ``dispatched_at`` so the resume sweep never re-submits the order.

        Idempotent: only stamps a row that is still un-dispatched.
        """
        if dispatched_at.tzinfo is not None:
            dispatched_at = dispatched_at.astimezone(timezone.utc).replace(tzinfo=None)
        async with self.session_factory() as session:
            result = await session.execute(
                update(ApprovalRecord)
                .where(
                    ApprovalRecord.approval_id == approval_id,
                    ApprovalRecord.dispatched_at.is_(None),
                )
                .values(dispatched_at=dispatched_at, dispatch_error=None)
            )
            await session.commit()
            return bool(result.rowcount)

    async def mark_dispatch_failed(
        self,
        approval_id: str,
        error: str,
        *,
        abandon: bool = False,
        dispatched_at: datetime | None = None,
    ):
        """Record a resume-dispatch failure and bump ``dispatch_attempts``.

        ``abandon=True`` stamps ``dispatched_at`` (terminal) so a permanently
        failing order stops being retried (the resume sweep skips dispatched
        rows). Otherwise the row stays resumable for the next sweep.
        """
        async with self.session_factory() as session:
            record = await session.get(ApprovalRecord, approval_id)
            if record is None:
                raise RecordNotFoundError(f"approval not found: {approval_id}")
            record.dispatch_error = error
            record.dispatch_attempts = int(record.dispatch_attempts or 0) + 1
            if abandon:
                stamp = dispatched_at or _utcnow()
                if stamp.tzinfo is not None:
                    stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
                record.dispatched_at = stamp
            await session.commit()
            return _approval_snapshot(record)

    async def expire_pending(self, now: datetime):
        # Normalize once at the boundary so the comparison and the writeback
        # both use naive UTC. Aware values would otherwise (a) fail the
        # ``expires_at <= now`` comparison on naive-column dialects with strict
        # type checks (PG/asyncpg), and (b) corrupt ``resolved_at`` on write.
        if now.tzinfo is not None:
            now = now.astimezone(timezone.utc).replace(tzinfo=None)
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


class SqlAlchemyDebugSessionRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_session(
        self,
        *,
        session_id: str,
        task_id: str,
        config_overrides: dict | None,
        input_overrides: dict | None,
        session_type: str = "debug",
    ) -> DebugSessionSnapshot:
        async with self.session_factory() as session:
            record = DebugSessionRecord(
                session_id=session_id,
                task_id=task_id,
                status="pending",
                run_id=None,
                error_message="",
                config_overrides=dict(config_overrides) if config_overrides is not None else None,
                input_overrides=dict(input_overrides) if input_overrides is not None else None,
                effective_config=None,
                session_type=session_type,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    raise _constraint_conflict(
                        f"debug session already exists: {session_id}",
                    ) from error
                raise _persistence_error("failed to create debug session") from error
            return _debug_session_snapshot(record)

    async def mark_running(
        self,
        session_id: str,
        *,
        run_id: str | None,
        effective_config: dict | None,
    ) -> DebugSessionSnapshot:
        async with self.session_factory() as session:
            record = await session.get(DebugSessionRecord, session_id)
            if record is None:
                raise RecordNotFoundError(f"debug session not found: {session_id}")
            record.status = "running"
            record.run_id = run_id
            record.effective_config = dict(effective_config) if effective_config is not None else None
            record.started_at = _utcnow()
            await session.commit()
            return _debug_session_snapshot(record)

    async def attach_run_id(self, session_id: str, run_id: str) -> DebugSessionSnapshot:
        async with self.session_factory() as session:
            record = await session.get(DebugSessionRecord, session_id)
            if record is None:
                raise RecordNotFoundError(f"debug session not found: {session_id}")
            record.run_id = run_id
            if record.started_at is None:
                record.started_at = _utcnow()
            if record.status == "pending":
                record.status = "running"
            await session.commit()
            return _debug_session_snapshot(record)

    async def mark_finished(
        self,
        session_id: str,
        *,
        status: str,
        error_message: str,
        error_type: str | None = None,
        traceback_tail: str | None = None,
    ) -> DebugSessionSnapshot:
        runtime_diag(f"mark_finished: acquiring session session_id={session_id}")
        async with self.session_factory() as session:
            runtime_diag(f"mark_finished: session acquired session_id={session_id}")
            record = await session.get(DebugSessionRecord, session_id)
            if record is None:
                raise RecordNotFoundError(f"debug session not found: {session_id}")
            record.status = status
            record.error_message = error_message
            # Only overwrite error_type/traceback_tail when caller supplied them so
            # that successful finalization leaves prior diagnostic context untouched.
            if error_type is not None:
                record.error_type = error_type or None
            if traceback_tail is not None:
                record.traceback_tail = traceback_tail or None
            if record.started_at is None:
                record.started_at = _utcnow()
            record.finished_at = _utcnow()
            await session.commit()
            runtime_diag(f"mark_finished: commit ok session_id={session_id}")
            return _debug_session_snapshot(record)

    async def get_session(self, session_id: str) -> DebugSessionSnapshot:
        async with self.session_factory() as session:
            record = await session.get(DebugSessionRecord, session_id)
            if record is None:
                raise RecordNotFoundError(f"debug session not found: {session_id}")
            return _debug_session_snapshot(record)

    async def list_sessions(self, task_id: str) -> list[DebugSessionSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(DebugSessionRecord)
                .where(DebugSessionRecord.task_id == task_id)
                .order_by(DebugSessionRecord.created_at.desc(), DebugSessionRecord.session_id.desc())
            )
            return [_debug_session_snapshot(record) for record in result.scalars().all()]

    async def get_latest_session(
        self,
        task_id: str,
        *,
        created_after: datetime | None = None,
    ) -> DebugSessionSnapshot | None:
        """Return the most recently created session for ``task_id`` regardless of status / type.

        ``created_after`` (exclusive) constrains the lookup to sessions created strictly
        after that timestamp — used by cron executors to avoid surfacing stale prior
        sessions when ``tick_once`` no-ops (kill switch on, instance not running).
        """
        async with self.session_factory() as session:
            stmt = (
                select(DebugSessionRecord)
                .where(DebugSessionRecord.task_id == task_id)
                .order_by(DebugSessionRecord.created_at.desc(), DebugSessionRecord.session_id.desc())
                .limit(1)
            )
            if created_after is not None:
                # ``created_at`` is TIMESTAMP WITHOUT TIME ZONE; an aware cutoff
                # (e.g. cron fired_at = datetime.now(timezone.utc)) makes asyncpg
                # reject the param ("can't subtract offset-naive and offset-aware").
                stmt = stmt.where(DebugSessionRecord.created_at > _to_naive_utc(created_after))
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            return _debug_session_snapshot(record) if record is not None else None

    async def get_active_session(self, task_id: str) -> DebugSessionSnapshot | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(DebugSessionRecord)
                .where(
                    DebugSessionRecord.task_id == task_id,
                    DebugSessionRecord.status.in_(("pending", "running")),
                )
                .order_by(DebugSessionRecord.created_at.desc(), DebugSessionRecord.session_id.desc())
                .limit(1)
            )
            record = result.scalar_one_or_none()
            return _debug_session_snapshot(record) if record is not None else None

    async def get_active_debug_session(self, task_id: str) -> DebugSessionSnapshot | None:
        """Return newest pending/running session created for interactive debug only.

        Scheduled/manual tick sessions reuse ``debug_sessions`` rows but must not block
        :meth:`TradingPlatformService.start_debug_session`.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(DebugSessionRecord)
                .where(
                    DebugSessionRecord.task_id == task_id,
                    DebugSessionRecord.session_type == "debug",
                    DebugSessionRecord.status.in_(("pending", "running")),
                )
                .order_by(DebugSessionRecord.created_at.desc(), DebugSessionRecord.session_id.desc())
                .limit(1)
            )
            record = result.scalar_one_or_none()
            return _debug_session_snapshot(record) if record is not None else None

    async def append_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict,
    ) -> DebugSessionEventSnapshot:
        for attempt in range(_TRACE_APPEND_MAX_RETRIES):
            async with self.session_factory() as session:
                max_sequence = await session.scalar(
                    select(func.max(DebugSessionEventRecord.sequence)).where(
                        DebugSessionEventRecord.session_id == session_id
                    )
                )
                record = DebugSessionEventRecord(
                    session_id=session_id,
                    sequence=(max_sequence or 0) + 1,
                    event_type=event_type,
                    payload=dict(payload),
                )
                session.add(record)
                try:
                    await session.commit()
                except IntegrityError as error:
                    await session.rollback()
                    if not _is_unique_violation(error):
                        raise _persistence_error("failed to append debug session event") from error
                    if attempt == _TRACE_APPEND_MAX_RETRIES - 1:
                        raise _constraint_conflict(
                            f"debug session event sequence allocation conflicted: {session_id}",
                        ) from error
                    continue
                return _debug_session_event_snapshot(record)
        raise StateConflictError(
            f"debug session event sequence allocation conflicted: {session_id}"
        )

    async def list_events(self, session_id: str) -> list[DebugSessionEventSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(DebugSessionEventRecord)
                .where(DebugSessionEventRecord.session_id == session_id)
                .order_by(DebugSessionEventRecord.sequence)
            )
            return [_debug_session_event_snapshot(record) for record in result.scalars().all()]


class SqlAlchemyDebugSessionSpanRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def append_span(
        self,
        span_id: str,
        trace_id: str,
        parent_span_id: str | None,
        session_id: str,
        name: str,
        span_type: str,
        start_time: datetime,
        end_time: datetime | None,
        duration_ms: float | None,
        attributes: dict,
        status: str,
        span_source: str = "debug",
    ) -> DebugSessionSpanSnapshot:
        async with self.session_factory() as session:
            record = DebugSessionSpanRecord(
                span_id=span_id,
                trace_id=trace_id,
                parent_span_id=parent_span_id,
                session_id=session_id,
                name=name,
                span_type=span_type,
                start_time=_to_naive_utc(start_time),
                end_time=_to_naive_utc(end_time),
                duration_ms=duration_ms,
                attributes=dict(attributes),
                status=status,
                span_source=span_source,
            )
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise _persistence_error("failed to append debug session span") from error
            return _debug_session_span_snapshot(record)

    async def list_spans_for_session(self, session_id: str) -> list[DebugSessionSpanSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(DebugSessionSpanRecord)
                .where(DebugSessionSpanRecord.session_id == session_id)
                .order_by(DebugSessionSpanRecord.start_time)
            )
            return [_debug_session_span_snapshot(record) for record in result.scalars().all()]

    async def list_spans_for_trace(self, trace_id: str) -> list[DebugSessionSpanSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(DebugSessionSpanRecord)
                .where(DebugSessionSpanRecord.trace_id == trace_id)
                .order_by(DebugSessionSpanRecord.start_time)
            )
            return [_debug_session_span_snapshot(record) for record in result.scalars().all()]


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


def _strategy_definition_snapshot(record: StrategyDefinitionRecord) -> StrategyDefinitionSnapshot:
    return StrategyDefinitionSnapshot(
        definition_id=record.definition_id,
        name=record.name,
        current_version=record.current_version,
        api_version=record.api_version,
        input_contract_json=dict(record.input_contract_json) if isinstance(record.input_contract_json, dict) else None,
        parameter_schema_json=(
            dict(record.parameter_schema_json) if isinstance(record.parameter_schema_json, dict) else None
        ),
        default_parameters_json=(
            dict(record.default_parameters_json) if isinstance(record.default_parameters_json, dict) else None
        ),
        capabilities_json=dict(record.capabilities_json) if isinstance(record.capabilities_json, dict) else None,
        provenance_json=dict(record.provenance_json) if isinstance(record.provenance_json, dict) else None,
        code_hash=record.code_hash,
        generation_prompt=record.generation_prompt,
        generation_model=record.generation_model,
        generation_metadata_json=(
            dict(record.generation_metadata_json) if isinstance(record.generation_metadata_json, dict) else None
        ),
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class SqlAlchemyStrategyDefinitionRepository:
    def __init__(self, session_factory, storage: StrategyStorage | None = None):
        self.session_factory = session_factory
        self._storage = storage

    async def read_current_code(self, definition_id: str) -> tuple[str, Path]:
        """Return (version_label, code_root_path) for the current version.

        Raises:
            RecordNotFoundError: when ``definition_id`` does not exist.
            VersionNotFound: when the definition exists but has no finalized version.
        """
        if self._storage is None:
            raise NotImplementedError(
                "read_current_code requires a StrategyStorage instance; "
                "wire storage= in Task 3+ bootstrap."
            )
        async with self.session_factory() as session:
            record = await session.get(StrategyDefinitionRecord, definition_id)
            if record is None:
                raise RecordNotFoundError(f"strategy definition not found: {definition_id}")
            if not record.current_version:
                raise VersionNotFound(
                    f"no finalized version for definition {definition_id}"
                )
        version_label = record.current_version
        code_root = self._storage.version_dir(definition_id, version_label)
        return version_label, code_root

    async def create_definition(self, **kwargs) -> StrategyDefinitionSnapshot:
        async with self.session_factory() as session:
            record = StrategyDefinitionRecord(**kwargs)
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    raise _constraint_conflict(
                        f"strategy definition already exists: {kwargs.get('definition_id', '')}",
                    ) from error
                raise _persistence_error("failed to create strategy definition") from error
            return _strategy_definition_snapshot(record)

    async def get_definition(self, definition_id: str) -> StrategyDefinitionSnapshot:
        async with self.session_factory() as session:
            record = await session.get(StrategyDefinitionRecord, definition_id)
            if record is None:
                raise RecordNotFoundError(f"strategy definition not found: {definition_id}")
            return _strategy_definition_snapshot(record)

    async def list_definitions(self) -> list[StrategyDefinitionSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(StrategyDefinitionRecord).order_by(
                    StrategyDefinitionRecord.created_at.desc(),
                    StrategyDefinitionRecord.definition_id.desc(),
                )
            )
            return [_strategy_definition_snapshot(record) for record in result.scalars().all()]

    async def update_definition(self, definition_id: str, **kwargs) -> StrategyDefinitionSnapshot:
        async with self.session_factory() as session:
            record = await session.get(StrategyDefinitionRecord, definition_id)
            if record is None:
                raise RecordNotFoundError(f"strategy definition not found: {definition_id}")
            for key, value in kwargs.items():
                setattr(record, key, value)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    raise _constraint_conflict(
                        f"strategy definition already exists: {definition_id}",
                    ) from error
                raise _persistence_error("failed to update strategy definition") from error
            await session.refresh(record)
            return _strategy_definition_snapshot(record)

    async def delete_definition(self, definition_id: str) -> None:
        await self.delete_definitions([definition_id])

    async def delete_definitions(self, definition_ids: list[str]) -> None:
        normalized_ids = [str(definition_id).strip() for definition_id in definition_ids if str(definition_id).strip()]
        if not normalized_ids:
            return
        async with self.session_factory() as session:
            result = await session.execute(
                select(StrategyDefinitionRecord.definition_id).where(
                    StrategyDefinitionRecord.definition_id.in_(normalized_ids)
                )
            )
            existing_ids = {str(definition_id) for definition_id in result.scalars().all()}
            missing_ids = [definition_id for definition_id in normalized_ids if definition_id not in existing_ids]
            if missing_ids:
                raise RecordNotFoundError(f"strategy definition not found: {missing_ids[0]}")
            try:
                await session.execute(
                    delete(StrategyDefinitionRecord).where(
                        StrategyDefinitionRecord.definition_id.in_(normalized_ids)
                    )
                )
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise _persistence_error("failed to delete strategy definitions") from error


def _model_invocation_to_dict(rec: ModelInvocationRecord) -> dict[str, Any]:
    """Serialize a model invocation record for API / debug-view payloads."""
    return {
        "id": rec.id,
        "created_at": rec.created_at.isoformat(),
        "model_id": rec.model_id,
        "provider_kind": rec.provider_kind,
        "model_route_name": rec.model_route_name,
        "provider_key": rec.provider_key,
        "model": rec.model,
        "task_id": rec.task_id,
        "run_id": rec.run_id,
        "trace_id": rec.trace_id,
        "span_id": rec.span_id,
        "call_kind": rec.call_kind,
        "first_token_latency_ms": rec.first_token_latency_ms,
        "total_latency_ms": rec.total_latency_ms,
        "input_tokens": rec.input_tokens,
        "output_tokens": rec.output_tokens,
        "total_tokens": rec.total_tokens,
        "cache_read_tokens": rec.cache_read_tokens,
        "cache_write_tokens": rec.cache_write_tokens,
        "ok": rec.ok,
        "error_message": rec.error_message or None,
        "request": dict(rec.request_payload),
        "response": dict(rec.response_payload) if rec.response_payload is not None else None,
    }


class SqlAlchemyModelInvocationRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def add_invocation(self, payload: dict[str, Any]) -> None:
        req = dict(payload["request_payload"])
        resp = payload.get("response_payload")
        async with self.session_factory() as session:
            record = ModelInvocationRecord(
                model_id=str(payload["model_id"]),
                provider_kind=str(payload["provider_kind"]),
                model_route_name=payload.get("model_route_name"),
                provider_key=payload.get("provider_key"),
                model=str(payload["model"]),
                task_id=payload.get("task_id"),
                run_id=payload.get("run_id"),
                trace_id=payload.get("trace_id"),
                span_id=_coerce_otel_span_id_for_storage(payload.get("span_id")),
                call_kind=str(payload["call_kind"]),
                first_token_latency_ms=payload.get("first_token_latency_ms"),
                total_latency_ms=payload.get("total_latency_ms"),
                input_tokens=payload.get("input_tokens"),
                output_tokens=payload.get("output_tokens"),
                total_tokens=payload.get("total_tokens"),
                cache_read_tokens=payload.get("cache_read_tokens"),
                cache_write_tokens=payload.get("cache_write_tokens"),
                ok=bool(payload["ok"]),
                error_message=str(payload.get("error_message") or ""),
                request_payload=req,
                response_payload=dict(resp) if isinstance(resp, dict) else None,
            )
            session.add(record)
            await session.commit()

    async def list_invocations(
        self,
        *,
        limit: int,
        offset: int,
        trace_id: str | None = None,
        span_id: str | None = None,
        run_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        cap = max(1, min(int(limit), 500))
        off = max(0, int(offset))
        t_sub = (trace_id or "").strip()
        s_sub = (span_id or "").strip()
        r_sub = (run_id or "").strip()
        conditions = []
        if t_sub:
            conditions.append(ModelInvocationRecord.trace_id == t_sub)
        if s_sub:
            conditions.append(ModelInvocationRecord.span_id == s_sub)
        if r_sub:
            conditions.append(ModelInvocationRecord.run_id == r_sub)
        where = and_(*conditions) if conditions else None
        async with self.session_factory() as session:
            count_base = select(func.count()).select_from(ModelInvocationRecord)
            if where is not None:
                count_base = count_base.where(where)
            count_result = await session.execute(count_base)
            total = int(count_result.scalar_one())
            list_stmt = select(ModelInvocationRecord).order_by(
                ModelInvocationRecord.created_at.desc(),
                ModelInvocationRecord.id.desc(),
            )
            if where is not None:
                list_stmt = list_stmt.where(where)
            result = await session.execute(list_stmt.offset(off).limit(cap))
            rows = [_model_invocation_to_dict(rec) for rec in result.scalars().all()]
            return rows, total

    async def list_invocations_for_run(self, run_id: str) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ModelInvocationRecord)
                .where(ModelInvocationRecord.run_id == run_id)
                .order_by(ModelInvocationRecord.created_at, ModelInvocationRecord.id)
            )
            return [_model_invocation_to_dict(rec) for rec in result.scalars().all()]

    async def list_invocations_for_trace(self, trace_id: str) -> list[dict[str, Any]]:
        """All invocations sharing an OpenTelemetry ``trace_id`` (chronological, uncapped)."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(ModelInvocationRecord)
                .where(ModelInvocationRecord.trace_id == trace_id)
                .order_by(ModelInvocationRecord.created_at, ModelInvocationRecord.id)
            )
            return [_model_invocation_to_dict(rec) for rec in result.scalars().all()]

    async def get_invocation_by_span_id(self, span_id: str) -> dict[str, Any] | None:
        variants = _model_invocation_span_id_lookup_variants(span_id)
        if not variants:
            return None
        async with self.session_factory() as session:
            result = await session.execute(
                select(ModelInvocationRecord)
                .where(ModelInvocationRecord.span_id.in_(variants))
                .limit(1),
            )
            rec = result.scalars().first()
            if rec is None:
                return None
            return _model_invocation_to_dict(rec)


def _cycle_proposals_json_for_api(raw: list[Any] | None) -> list[Any] | None:
    """Omit ``quantity`` / ``amount`` from stored proposal dicts (sizing lives in reviews)."""

    if raw is None:
        return None
    out: list[Any] = []
    for item in raw:
        if isinstance(item, dict):
            out.append({k: v for k, v in item.items() if k not in ("quantity", "amount")})
        else:
            out.append(item)
    return out


def cycle_run_to_dict(rec: CycleRunRecord) -> dict[str, Any]:
    """JSON-serializable view for API clients."""
    details_out: dict[str, Any] | None = None
    if rec.details is not None:
        details_out = dict(rec.details)
        raw_props = details_out.get("proposals")
        if raw_props is not None:
            details_out["proposals"] = _cycle_proposals_json_for_api(list(raw_props))
        # New cycles: ``decisions`` (post-sizing rows); pass through with shallow copy
        raw_decisions = details_out.get("decisions")
        if raw_decisions is not None and isinstance(raw_decisions, list):
            details_out["decisions"] = [dict(d) if isinstance(d, dict) else d for d in raw_decisions]
    return {
        "run_id": rec.run_id,
        "task_id": rec.task_id,
        "agent_name": rec.agent_name,
        "session_id": rec.session_id,
        "trace_id": rec.trace_id,
        "run_mode": rec.run_mode,
        "run_kind": rec.run_kind,
        "trigger_id": rec.trigger_id,
        "clock_mode": rec.clock_mode,
        "cycle_time": rec.cycle_time_utc.isoformat() if rec.cycle_time_utc is not None else None,
        # Backward-compatible alias for clients not yet migrated.
        "cycle_time_utc": rec.cycle_time_utc.isoformat() if rec.cycle_time_utc is not None else None,
        "wall_started_at": rec.wall_started_at.isoformat(),
        "wall_finished_at": rec.wall_finished_at.isoformat() if rec.wall_finished_at is not None else None,
        "runtime_params": dict(rec.runtime_params) if rec.runtime_params is not None else None,
        "status": rec.status,
        "details": details_out,
        "cycle_failed": rec.cycle_failed,
        "failure_message": rec.failure_message or None,
        "completed_phases": list(rec.completed_phases_json) if rec.completed_phases_json is not None else None,
        "submitted_count": rec.submitted_count,
        "vetoed_count": rec.vetoed_count,
        "pending_approval_count": rec.pending_approval_count,
        "code_version": rec.code_version,
        "code_hash": rec.code_hash,
    }


def run_to_dict(rec: Run) -> dict[str, Any]:
    return {
        "run_id": rec.run_id,
        "task_id": rec.task_id,
        "mode": rec.mode,
        "status": rec.status,
        "market_profile": rec.market_profile,
        "bar_interval": rec.bar_interval,
        "range_start_utc": rec.range_start_utc.isoformat(),
        "range_end_utc": rec.range_end_utc.isoformat(),
        "session_id": rec.session_id,
        "debug_enabled": bool(rec.debug_enabled),
        "config_overrides_json": dict(rec.config_overrides_json) if rec.config_overrides_json is not None else None,
        "starting_equity": rec.starting_equity,
        "ending_equity": rec.ending_equity,
        "return_pct": rec.return_pct,
        "error_message": rec.error_message or None,
        "bars_total": rec.bars_total,
        "bars_completed": rec.bars_completed,
        "stop_requested": bool(rec.stop_requested),
        "ledger_checkpoint_json": dict(rec.ledger_checkpoint_json) if rec.ledger_checkpoint_json is not None else None,
        "reference_starting_equity": rec.reference_starting_equity,
        "created_at": rec.created_at.isoformat(),
        "started_at": rec.started_at.isoformat() if rec.started_at is not None else None,
        "finished_at": rec.finished_at.isoformat() if rec.finished_at is not None else None,
        "model_route_name": rec.model_route_name,
        "strategy_code_hash": rec.strategy_code_hash,
        "code_version": rec.code_version,
        "engine_version": rec.engine_version,
    }


class SqlAlchemyRunRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_pending(
        self,
        *,
        run_id: str,
        task_id: str,
        mode: str,
        market_profile: str,
        bar_interval: str,
        range_start_utc: datetime,
        range_end_utc: datetime,
        session_id: str | None,
        bars_total: int,
        debug_enabled: bool = True,
        config_overrides_json: dict | None = None,
        model_route_name: str | None = None,
        config_snapshot_json: dict | None = None,
        engine_version: str | None = None,
        strategy_code_hash: str | None = None,
        code_version: str | None = None,
    ) -> None:
        """Insert a new run row in ``pending`` state.

        ``config_snapshot_json`` / ``engine_version`` / ``strategy_code_hash`` /
        ``code_version`` are optional run-time provenance fields; callers that
        don't have them yet may omit, but production write paths (backtest /
        instance start) should populate all four so analytics can reproduce the
        run later.
        """
        async with self.session_factory() as session:
            session.add(
                Run(
                    run_id=run_id,
                    task_id=task_id,
                    mode=mode,
                    status="pending",
                    market_profile=market_profile,
                    bar_interval=bar_interval,
                    range_start_utc=_to_naive_utc(range_start_utc),
                    range_end_utc=_to_naive_utc(range_end_utc),
                    session_id=session_id,
                    debug_enabled=bool(debug_enabled),
                    config_overrides_json=dict(config_overrides_json) if config_overrides_json else None,
                    model_route_name=model_route_name,
                    error_message="",
                    bars_total=bars_total,
                    bars_completed=0,
                    config_snapshot_json=dict(config_snapshot_json) if config_snapshot_json else None,
                    engine_version=engine_version,
                    strategy_code_hash=strategy_code_hash,
                    code_version=code_version,
                )
            )
            await session.commit()

    async def mark_running(self, run_id: str) -> None:
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return
            rec.status = "running"
            rec.started_at = _utcnow()
            await session.commit()

    async def mark_paused(self, run_id: str) -> None:
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return
            rec.status = "paused"
            await session.commit()

    async def mark_resumed(self, run_id: str) -> None:
        """Return job to ``running`` after a cooperative pause (does not reset ``started_at``)."""
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return
            rec.status = "running"
            await session.commit()

    async def set_progress(self, run_id: str, *, bars_completed: int) -> None:
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return
            rec.bars_completed = bars_completed
            await session.commit()

    async def update_running_metrics(
        self,
        run_id: str,
        *,
        starting_equity: float | None,
        ending_equity: float | None,
        return_pct: float | None,
    ) -> None:
        """Persist MTM return while a job is still ``running`` or ``paused`` (per-bar refresh)."""
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return
            if rec.status not in ("running", "paused"):
                return
            rec.starting_equity = starting_equity
            rec.ending_equity = ending_equity
            rec.return_pct = return_pct
            await session.commit()

    async def finalize_success(
        self,
        run_id: str,
        *,
        starting_equity: float | None,
        ending_equity: float | None,
        return_pct: float | None,
    ) -> None:
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return
            rec.status = "completed"
            rec.starting_equity = starting_equity
            rec.ending_equity = ending_equity
            rec.return_pct = return_pct
            rec.finished_at = _utcnow()
            await session.commit()

    async def finalize_failed(self, run_id: str, *, error_message: str) -> None:
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return
            rec.status = "failed"
            rec.error_message = error_message[:8000] if error_message else ""
            rec.finished_at = _utcnow()
            await session.commit()

    async def finalize_stopped(self, run_id: str) -> None:
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return
            rec.status = "stopped"
            rec.error_message = ""
            rec.finished_at = _utcnow()
            await session.commit()

    async def get(self, run_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return None
            return run_to_dict(rec)

    async def list_for_task(
        self,
        task_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where_clause = Run.task_id == task_id
        async with self.session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(Run).where(where_clause)
            )
            result = await session.execute(
                select(Run)
                .where(where_clause)
                .order_by(Run.created_at.desc(), Run.run_id.desc())
                .offset(offset)
                .limit(limit),
            )
            rows = result.scalars().all()
            return [run_to_dict(r) for r in rows], int(total or 0)

    async def list_jobs(
        self,
        task_id: str | None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        async with self.session_factory() as session:
            count_stmt = select(func.count()).select_from(Run)
            list_stmt = select(Run).order_by(
                Run.created_at.desc(),
                Run.run_id.desc(),
            )
            if task_id is not None:
                filt = Run.task_id == task_id
                count_stmt = count_stmt.where(filt)
                list_stmt = list_stmt.where(filt)
            total = int((await session.scalar(count_stmt)) or 0)
            result = await session.execute(list_stmt.offset(offset).limit(limit))
            rows = result.scalars().all()
            return [run_to_dict(r) for r in rows], total

    async def has_active_job(self, task_id: str) -> bool:
        async with self.session_factory() as session:
            n = await session.scalar(
                select(func.count())
                .select_from(Run)
                .where(
                    Run.task_id == task_id,
                    Run.status.in_(("pending", "running", "paused")),
                )
            )
            return int(n or 0) > 0

    async def has_any_job(self, task_id: str) -> bool:
        async with self.session_factory() as session:
            n = await session.scalar(
                select(func.count()).select_from(Run).where(Run.task_id == task_id)
            )
            return int(n or 0) > 0

    async def list_jobs_with_statuses(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(Run).where(Run.status.in_(statuses))
            )
            return [run_to_dict(r) for r in result.scalars().all()]

    async def set_stop_requested(self, run_id: str, value: bool) -> None:
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return
            rec.stop_requested = bool(value)
            await session.commit()

    async def save_ledger_checkpoint(self, run_id: str, payload: dict[str, Any] | None) -> None:
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None:
                return
            rec.ledger_checkpoint_json = dict(payload) if payload is not None else None
            await session.commit()

    async def set_reference_starting_equity_once(self, run_id: str, equity: float) -> None:
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None or rec.reference_starting_equity is not None:
                return
            rec.reference_starting_equity = float(equity)
            await session.commit()

    async def mark_paused_shutdown(self, run_id: str) -> None:
        """Process shutdown while job is still active: keep resumable (not ``failed``)."""
        async with self.session_factory() as session:
            rec = await session.get(Run, run_id)
            if rec is None or rec.status in ("completed", "failed"):
                return
            rec.status = "paused"
            await session.commit()


class SqlAlchemyTradeFillRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def insert_fill(
        self,
        *,
        task_id: str,
        cycle_run_id: str,
        symbol: str,
        side: str,
        quantity: str,
        price: str,
        filled_at: datetime,
        source_mode: str,
        run_id: str | None = None,
        session_id: str | None = None,
        amount: str | None = None,
        fee: str | None = None,
        currency: str | None = None,
        intent_id: str | None = None,
        rationale: str | None = None,
        raw_payload: dict | None = None,
        entry_tag: str | None = None,
        exit_tag: str | None = None,
        exit_reason: str | None = None,
    ) -> bool:
        """Insert one fill row.

        Returns ``True`` when inserted, ``False`` when a dedupe unique-key conflict
        indicates the same fill was already persisted.

        ``entry_tag`` / ``exit_tag`` come from ``OrderIntent.signal_tag`` —
        the factor identifier the strategy attached to ``Signal.tag``.
        The execution adapter chooses which column to populate based on
        ``side`` (buy → entry_tag, sell → exit_tag).

        ``exit_reason`` comes from ``OrderIntent.exit_reason`` (a SELL-only
        :class:`doyoutrade.strategy_sdk.signal.ExitReason` value); ``None`` on
        buys and uncategorized exits.
        """
        async with self.session_factory() as session:
            session.add(
                TradeFillRecord(
                    task_id=task_id,
                    cycle_run_id=cycle_run_id,
                    run_id=run_id,
                    session_id=session_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    amount=amount,
                    fee=fee,
                    currency=currency,
                    intent_id=intent_id,
                    rationale=rationale,
                    entry_tag=entry_tag,
                    exit_tag=exit_tag,
                    exit_reason=exit_reason,
                    intent_id_normalized=(intent_id or "").strip(),
                    filled_at=_to_naive_utc(filled_at),
                    source_mode=source_mode,
                    raw_payload=dict(raw_payload) if isinstance(raw_payload, dict) else None,
                    created_at=_utcnow(),
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    return False
                raise _persistence_error("failed to insert trade fill") from error

    async def list_for_task_run(
        self,
        *,
        task_id: str,
        run_id: str,
        symbol: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = [TradeFillRecord.task_id == task_id, TradeFillRecord.run_id == run_id]
        sym = (symbol or "").strip()
        if sym:
            conditions.append(TradeFillRecord.symbol == sym)
        async with self.session_factory() as session:
            result = await session.execute(
                select(TradeFillRecord)
                .where(and_(*conditions))
                .order_by(TradeFillRecord.filled_at, TradeFillRecord.id)
            )
            rows = result.scalars().all()
            out: list[dict[str, Any]] = []
            for rec in rows:
                out.append(
                    {
                        "id": rec.id,
                        "task_id": rec.task_id,
                        "cycle_run_id": rec.cycle_run_id,
                        "run_id": rec.run_id,
                        "session_id": rec.session_id,
                        "symbol": rec.symbol,
                        "side": rec.side,
                        "quantity": rec.quantity,
                        "price": rec.price,
                        "amount": rec.amount,
                        "fee": rec.fee,
                        "currency": rec.currency,
                        "intent_id": rec.intent_id,
                        "rationale": rec.rationale,
                        "entry_tag": rec.entry_tag,
                        "exit_tag": rec.exit_tag,
                        "exit_reason": rec.exit_reason,
                        "filled_at": rec.filled_at.isoformat(),
                        "source_mode": rec.source_mode,
                    }
                )
            return out

    async def list_for_task(
        self,
        *,
        task_id: str,
        source_mode: str | None = None,
    ) -> list[dict[str, Any]]:
        """All persisted fills for a task, optionally scoped to one run mode."""
        conditions = [TradeFillRecord.task_id == task_id]
        mode = (source_mode or "").strip()
        if mode:
            conditions.append(TradeFillRecord.source_mode == mode)
        async with self.session_factory() as session:
            result = await session.execute(
                select(TradeFillRecord)
                .where(and_(*conditions))
                .order_by(TradeFillRecord.filled_at, TradeFillRecord.id)
            )
            rows = result.scalars().all()
            out: list[dict[str, Any]] = []
            for rec in rows:
                out.append(
                    {
                        "id": rec.id,
                        "task_id": rec.task_id,
                        "cycle_run_id": rec.cycle_run_id,
                        "run_id": rec.run_id,
                        "session_id": rec.session_id,
                        "symbol": rec.symbol,
                        "side": rec.side,
                        "quantity": rec.quantity,
                        "price": rec.price,
                        "amount": rec.amount,
                        "fee": rec.fee,
                        "currency": rec.currency,
                        "intent_id": rec.intent_id,
                        "rationale": rec.rationale,
                        "entry_tag": rec.entry_tag,
                        "exit_tag": rec.exit_tag,
                        "exit_reason": rec.exit_reason,
                        "filled_at": rec.filled_at.isoformat(),
                        "source_mode": rec.source_mode,
                    }
                )
            return out

    async def get_by_intent_id(
        self, *, task_id: str, intent_id: str
    ) -> dict[str, Any] | None:
        """Most recent fill for one ``intent_id`` (scoped to a task).

        Used to attach the approval result receipt: an approved order usually
        dispatches and fills in a LATER resume cycle, so the fill is NOT under
        the approval's originating ``run_id`` — it must be joined by
        ``intent_id``. Returns the newest match (resume + retry can produce more
        than one) or ``None`` when the order has not (yet) filled.
        """
        needle = (intent_id or "").strip()
        if not needle:
            return None
        async with self.session_factory() as session:
            result = await session.execute(
                select(TradeFillRecord)
                .where(
                    and_(
                        TradeFillRecord.task_id == task_id,
                        TradeFillRecord.intent_id == needle,
                    )
                )
                .order_by(TradeFillRecord.filled_at.desc(), TradeFillRecord.id.desc())
                .limit(1)
            )
            rec = result.scalars().first()
            if rec is None:
                return None
            return {
                "id": rec.id,
                "task_id": rec.task_id,
                "cycle_run_id": rec.cycle_run_id,
                "run_id": rec.run_id,
                "symbol": rec.symbol,
                "side": rec.side,
                "quantity": rec.quantity,
                "price": rec.price,
                "amount": rec.amount,
                "fee": rec.fee,
                "currency": rec.currency,
                "intent_id": rec.intent_id,
                "filled_at": rec.filled_at.isoformat(),
                "source_mode": rec.source_mode,
            }


class SqlAlchemyCycleRunRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_started(
        self,
        *,
        run_id: str,
        task_id: str,
        agent_name: str,
        session_id: str | None,
        trace_id: str | None,
        run_mode: str,
        run_kind: str,
        clock_mode: str,
        cycle_time: datetime | None,
        runtime_params: dict | None,
        code_version: str | None = None,
        code_hash: str | None = None,
        trigger_id: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            record = CycleRunRecord(
                run_id=run_id,
                task_id=task_id,
                agent_name=agent_name or "",
                session_id=session_id,
                trace_id=trace_id,
                run_mode=run_mode,
                run_kind=run_kind,
                trigger_id=trigger_id,
                clock_mode=clock_mode,
                cycle_time_utc=_to_naive_utc(cycle_time),
                wall_started_at=_utcnow(),
                runtime_params=dict(runtime_params) if runtime_params is not None else None,
                status="running",
                cycle_failed=False,
                failure_message="",
                code_version=code_version,
                code_hash=code_hash,
            )
            session.add(record)
            await session.commit()

    async def finalize(
        self,
        run_id: str,
        *,
        status: str,
        details_patch: dict[str, Any] | None = None,
        cycle_failed: bool = False,
        failure_message: str = "",
        completed_phases_json: list | None = None,
        submitted_count: int | None = None,
        vetoed_count: int | None = None,
        pending_approval_count: int | None = None,
    ) -> None:
        async with self.session_factory() as session:
            record = await session.get(CycleRunRecord, run_id)
            if record is None:
                return
            record.status = status
            record.wall_finished_at = _utcnow()
            if details_patch:
                merged: dict[str, Any] = {**(record.details or {}), **details_patch}
                record.details = merged
            record.cycle_failed = cycle_failed
            if failure_message:
                record.failure_message = failure_message
            record.completed_phases_json = (
                list(completed_phases_json)
                if completed_phases_json is not None
                else record.completed_phases_json
            )
            if submitted_count is not None:
                record.submitted_count = submitted_count
            if vetoed_count is not None:
                record.vetoed_count = vetoed_count
            if pending_approval_count is not None:
                record.pending_approval_count = pending_approval_count
            await session.commit()

    async def patch_details(self, run_id: str, patch: dict[str, Any]) -> None:
        """Shallow-merge ``patch`` into ``details`` without touching status/timing.

        Used by the post-cycle delivery orchestrator to record what was actually
        PUSHED (the delivered card content + target + outcome) so the 周期详情
        view can replay it — in particular for Feishu channel pushes whose card
        is otherwise sent straight to the group and never persisted. Unlike
        :meth:`finalize`, this leaves ``status`` / ``wall_finished_at`` /
        ``cycle_failed`` untouched (the cycle already finalized). Best-effort: a
        missing run is a no-op.
        """
        if not patch:
            return
        async with self.session_factory() as session:
            record = await session.get(CycleRunRecord, run_id)
            if record is None:
                return
            record.details = {**(record.details or {}), **patch}
            await session.commit()

    async def list_for_task(
        self,
        task_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        run_id_contains: str | None = None,
        status: str | None = None,
        run_kind: str | None = None,
        run_mode: str | None = None,
        exclude_run_kind: str | None = None,
        wall_started_at_after: datetime | None = None,
        wall_started_at_before: datetime | None = None,
        session_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        conditions = [CycleRunRecord.task_id == task_id]
        if session_id:
            conditions.append(CycleRunRecord.session_id == session_id)
        if run_id_contains:
            conditions.append(CycleRunRecord.run_id.contains(run_id_contains))
        if status:
            conditions.append(CycleRunRecord.status == status)
        if run_kind:
            conditions.append(CycleRunRecord.run_kind == run_kind)
        if run_mode:
            conditions.append(CycleRunRecord.run_mode == run_mode)
        if exclude_run_kind:
            conditions.append(CycleRunRecord.run_kind != exclude_run_kind)
        if wall_started_at_after is not None:
            conditions.append(CycleRunRecord.wall_started_at >= wall_started_at_after)
        if wall_started_at_before is not None:
            conditions.append(CycleRunRecord.wall_started_at <= wall_started_at_before)
        where_clause = and_(*conditions)
        async with self.session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(CycleRunRecord).where(where_clause)
            )
            result = await session.execute(
                select(CycleRunRecord)
                .where(where_clause)
                .order_by(CycleRunRecord.wall_started_at.desc(), CycleRunRecord.run_id.desc())
                .offset(offset)
                .limit(limit),
            )
            rows = result.scalars().all()
            return [cycle_run_to_dict(r) for r in rows], int(total or 0)

    async def get_by_run_id(self, run_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            record = await session.get(CycleRunRecord, run_id)
            if record is None:
                return None
            return cycle_run_to_dict(record)

    async def get_for_task(self, task_id: str, run_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            record = await session.get(CycleRunRecord, run_id)
            if record is None or record.task_id != task_id:
                return None
            return cycle_run_to_dict(record)

    async def list_by_trace_id(self, trace_id: str) -> list[dict[str, Any]]:
        """All cycle runs carrying an OpenTelemetry ``trace_id`` (chronological)."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(CycleRunRecord)
                .where(CycleRunRecord.trace_id == trace_id)
                .order_by(CycleRunRecord.wall_started_at, CycleRunRecord.run_id)
            )
            return [cycle_run_to_dict(r) for r in result.scalars().all()]


def create_model_invocation_recorder(
    repository: SqlAlchemyModelInvocationRepository,
) -> Callable[[dict[str, Any]], None]:
    """Schedule :meth:`SqlAlchemyModelInvocationRepository.add_invocation` on the running loop.

    If there is no running event loop, the payload is dropped (sync / non-async callers).
    """

    def record(payload: dict[str, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def runner() -> None:
            try:
                await repository.add_invocation(payload)
            except Exception:
                _LOG.exception("failed to persist model invocation")

        loop.create_task(runner())

    return record


class SqlAlchemyCronJobRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def list_jobs(self, agent_id: str) -> list[dict[str, Any]]:
        if self.session_factory is None:
            return []
        async with self.session_factory() as session:
            stmt = select(CronJobRecord).order_by(CronJobRecord.created_at.desc())
            if agent_id:  # empty string → no filter (return all)
                stmt = stmt.where(CronJobRecord.agent_id == agent_id)
            rows = (await session.execute(stmt)).scalars().all()
            return [_cron_job_dict(r) for r in rows]

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            row = await session.get(CronJobRecord, job_id)
            return _cron_job_dict(row) if row else None

    async def upsert_job(self, data: dict[str, Any]) -> dict[str, Any]:
        _known_columns = {
            "id", "agent_id", "name", "cron_expression", "timezone",
            "schedule_kind", "at_iso", "delete_after_run",
            "enabled", "input_template", "max_concurrency", "timeout_seconds",
            "pre_action", "task_kind", "task_params_json",
            "last_run_at", "last_run_session_id", "last_status", "last_error",
            "created_at", "updated_at",
        }
        filtered = {k: v for k, v in data.items() if k in _known_columns}
        # ``merged = {**existing, **updates}`` (see cron_manager.update_job)
        # carries the ISO-string datetimes that _cron_job_dict produced for the
        # existing row. Re-parse them to datetime so asyncpg accepts the
        # TIMESTAMP columns (SQLite tolerated the strings; PostgreSQL 500s).
        for _dt_col in ("last_run_at", "created_at", "updated_at"):
            if _dt_col in filtered:
                filtered[_dt_col] = _coerce_naive_utc_dt(filtered[_dt_col])
        async with self.session_factory() as session:
            job_id = filtered.get("id")
            if job_id:
                row = await session.get(CronJobRecord, job_id)
                if row:
                    for k, v in filtered.items():
                        if k != "id":
                            setattr(row, k, v)
                    await session.commit()
                    return _cron_job_dict(row)
            new_id = job_id or f"cron-{uuid.uuid4().hex[:12]}"
            record = CronJobRecord(id=new_id, **filtered)
            session.add(record)
            await session.commit()
            return _cron_job_dict(record)

    async def delete_job(self, job_id: str) -> None:
        async with self.session_factory() as session:
            row = await session.get(CronJobRecord, job_id)
            if row:
                await session.delete(row)
                await session.commit()

    async def update_job_state(
        self,
        job_id: str,
        *,
        last_run_at: datetime | None = None,
        last_run_session_id: str | None = None,
        last_status: str | None = None,
        last_error: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            row = await session.get(CronJobRecord, job_id)
            if row:
                if last_run_at is not None:
                    # cron_manager emits aware UTC; ``last_run_at`` column
                    # is naive ``DateTime``. Strip at the boundary so
                    # asyncpg accepts the parameter on Postgres.
                    row.last_run_at = _to_naive_utc(last_run_at)
                if last_run_session_id is not None:
                    row.last_run_session_id = last_run_session_id
                if last_status is not None:
                    row.last_status = last_status
                if last_error is not None:
                    row.last_error = last_error
                await session.commit()


def _account_dict(record: AccountRecord) -> dict[str, Any]:
    """Serialize an AccountRecord. Credentials (token/session_id) are returned
    as-is — this is a local single-user deployment with plaintext storage."""
    return {
        "id": record.id,
        "name": record.name,
        "mode": record.mode,
        "base_url": record.base_url or "",
        "token": record.token,
        "timeout_seconds": float(record.timeout_seconds),
        "qmt_account_id": record.qmt_account_id,
        "qmt_terminal_id": record.qmt_terminal_id,
        "session_id": record.session_id,
        "mock_cash": float(record.mock_cash),
        "mock_equity": float(record.mock_equity),
        "mock_positions": list(record.mock_positions or []),
        "is_default": bool(record.is_default),
        "enabled": bool(record.enabled),
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


class SqlAlchemyAccountRepository:
    """CRUD for persisted QMT accounts (replaces the config.data.qmt block)."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def list_accounts(self) -> list[dict[str, Any]]:
        if self.session_factory is None:
            return []
        async with self.session_factory() as session:
            stmt = select(AccountRecord).order_by(AccountRecord.created_at.desc())
            rows = (await session.execute(stmt)).scalars().all()
            return [_account_dict(r) for r in rows]

    async def get_account(self, account_id: str) -> dict[str, Any] | None:
        if self.session_factory is None or not account_id:
            return None
        async with self.session_factory() as session:
            row = await session.get(AccountRecord, account_id)
            return _account_dict(row) if row else None

    async def get_default_account(self) -> dict[str, Any] | None:
        """The account flagged ``is_default`` and still enabled, if any."""
        if self.session_factory is None:
            return None
        async with self.session_factory() as session:
            stmt = (
                select(AccountRecord)
                .where(AccountRecord.is_default.is_(True))
                .where(AccountRecord.enabled.is_(True))
                .order_by(AccountRecord.updated_at.desc())
            )
            row = (await session.execute(stmt)).scalars().first()
            return _account_dict(row) if row else None

    async def upsert_account(self, data: dict[str, Any]) -> dict[str, Any]:
        _known_columns = {
            "id", "name", "mode", "base_url", "token", "timeout_seconds",
            "qmt_account_id", "qmt_terminal_id", "session_id", "mock_cash", "mock_equity",
            "mock_positions", "is_default", "enabled",
            "created_at", "updated_at",
        }
        filtered = {k: v for k, v in data.items() if k in _known_columns}
        async with self.session_factory() as session:
            account_id = filtered.get("id")
            if account_id:
                row = await session.get(AccountRecord, account_id)
                if row:
                    for k, v in filtered.items():
                        if k != "id":
                            setattr(row, k, v)
                    await session.commit()
                    return _account_dict(row)
            new_id = account_id or f"acct-{uuid.uuid4().hex[:12]}"
            create_kwargs = {k: v for k, v in filtered.items() if k != "id"}
            record = AccountRecord(id=new_id, **create_kwargs)
            session.add(record)
            await session.commit()
            return _account_dict(record)

    async def delete_account(self, account_id: str) -> None:
        async with self.session_factory() as session:
            row = await session.get(AccountRecord, account_id)
            if row:
                await session.delete(row)
                await session.commit()

    async def set_default(self, account_id: str) -> dict[str, Any] | None:
        """Make ``account_id`` the sole default. Clears the flag on all other
        rows first so the "at most one default" invariant holds without a
        DB-level partial-unique index (SQLite/Postgres differ there)."""
        async with self.session_factory() as session:
            row = await session.get(AccountRecord, account_id)
            if row is None:
                return None
            await session.execute(
                update(AccountRecord)
                .where(AccountRecord.id != account_id)
                .values(is_default=False)
            )
            row.is_default = True
            await session.commit()
            return _account_dict(row)

    async def update_session_id(self, account_id: str, session_id: str) -> None:
        """Persist a refreshed trading session id back onto the account row
        (replaces the old persist_qmt_session_id config.yaml write-back)."""
        async with self.session_factory() as session:
            row = await session.get(AccountRecord, account_id)
            if row is not None:
                row.session_id = session_id
                await session.commit()


def _watchlist_entry_dict(record: WatchlistRecord) -> dict[str, Any]:
    """Serialize a WatchlistRecord. ``tags`` is always a list and ``note`` a
    str so API / CLI / frontend never have to defend against NULL JSON."""
    return {
        "id": record.id,
        "symbol": record.symbol,
        "display_name": record.display_name,
        "tags": list(record.tags or []),
        "note": record.note or "",
        "sort_order": int(record.sort_order or 0),
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


class SqlAlchemyWatchlistRepository:
    """CRUD + tag/snapshot queries for the single watchlist pool.

    The watchlist drives: the REST/CLI ``watchlist`` surface, the K-line sync
    scope (``list_symbols``), eager ``@watchlist:<tag>`` universe resolution,
    and the per-cycle frozen ``snapshot`` consumed by
    ``ctx.dp.watchlist_symbols``. A unique ``symbol`` violation surfaces as a
    structured ``duplicate_watchlist_symbol: <symbol>`` conflict — never
    silently swallowed.
    """

    # Columns the caller is allowed to set on upsert. Anything else in the
    # payload is dropped on the floor here (Service-layer normalization is the
    # contract for rejecting unknown fields loudly).
    _KNOWN_COLUMNS = {
        "id", "symbol", "display_name", "tags", "note", "sort_order",
        "created_at", "updated_at",
    }

    def __init__(self, session_factory):
        self.session_factory = session_factory

    def _select_entries(self, tag: str | None):
        stmt = select(WatchlistRecord).order_by(
            WatchlistRecord.sort_order.asc(),
            WatchlistRecord.created_at.asc(),
        )
        return stmt

    @staticmethod
    def _matches_tag(record: WatchlistRecord, tag: str | None) -> bool:
        if not tag:
            return True
        return tag in list(record.tags or [])

    async def list_entries(self, tag: str | None = None) -> list[dict[str, Any]]:
        if self.session_factory is None:
            return []
        async with self.session_factory() as session:
            rows = (await session.execute(self._select_entries(tag))).scalars().all()
            return [
                _watchlist_entry_dict(r)
                for r in rows
                if self._matches_tag(r, tag)
            ]

    async def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        if self.session_factory is None or not entry_id:
            return None
        async with self.session_factory() as session:
            row = await session.get(WatchlistRecord, entry_id)
            return _watchlist_entry_dict(row) if row else None

    async def get_by_symbol(self, symbol: str) -> dict[str, Any] | None:
        if self.session_factory is None or not symbol:
            return None
        async with self.session_factory() as session:
            stmt = select(WatchlistRecord).where(WatchlistRecord.symbol == symbol)
            row = (await session.execute(stmt)).scalars().first()
            return _watchlist_entry_dict(row) if row else None

    async def upsert_entry(self, data: dict[str, Any]) -> dict[str, Any]:
        filtered = {k: v for k, v in data.items() if k in self._KNOWN_COLUMNS}
        # Coerce the structured columns: tags must be a list, note a str. The
        # Service layer validates shape, but defend the column invariant here
        # so a NULL/None never lands in a NOT NULL JSON / Text column.
        if "tags" in filtered:
            filtered["tags"] = list(filtered["tags"] or [])
        if "note" in filtered:
            filtered["note"] = "" if filtered["note"] is None else str(filtered["note"])
        symbol = filtered.get("symbol")
        async with self.session_factory() as session:
            entry_id = filtered.get("id")
            if entry_id:
                row = await session.get(WatchlistRecord, entry_id)
                if row:
                    for k, v in filtered.items():
                        if k != "id":
                            setattr(row, k, v)
                    try:
                        await session.commit()
                    except IntegrityError as error:
                        await session.rollback()
                        if _is_unique_violation(error):
                            raise _constraint_conflict(
                                f"duplicate_watchlist_symbol: {symbol}",
                            ) from error
                        raise _persistence_error(
                            f"failed to update watchlist entry: {_integrity_message(error)}",
                        ) from error
                    return _watchlist_entry_dict(row)
            new_id = entry_id or f"wl-{uuid.uuid4().hex[:12]}"
            create_kwargs = {k: v for k, v in filtered.items() if k != "id"}
            record = WatchlistRecord(id=new_id, **create_kwargs)
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    raise _constraint_conflict(
                        f"duplicate_watchlist_symbol: {symbol}",
                    ) from error
                raise _persistence_error(
                    f"failed to create watchlist entry: {_integrity_message(error)}",
                ) from error
            return _watchlist_entry_dict(record)

    async def delete_entry(self, entry_id: str) -> None:
        if self.session_factory is None or not entry_id:
            return
        async with self.session_factory() as session:
            row = await session.get(WatchlistRecord, entry_id)
            if row:
                await session.delete(row)
                await session.commit()

    async def list_symbols(self, tag: str | None = None) -> list[str]:
        entries = await self.list_entries(tag=tag)
        return [e["symbol"] for e in entries]

    async def snapshot(self) -> dict[str, list[str]]:
        """symbol -> tags, for the per-cycle frozen watchlist snapshot."""
        entries = await self.list_entries()
        return {e["symbol"]: list(e["tags"]) for e in entries}

    async def list_tags(self) -> list[dict[str, Any]]:
        """Distinct tags with their entry counts, ordered by count desc.

        Tags are denormalized inside the JSON column, so counting happens in
        Python rather than via a portable GROUP BY across SQLite/Postgres JSON.
        """
        entries = await self.list_entries()
        counts: dict[str, int] = {}
        for entry in entries:
            for tag in entry["tags"]:
                counts[tag] = counts.get(tag, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [{"tag": tag, "count": count} for tag, count in ordered]


def _cron_job_run_dict(record: CronJobRunRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "job_id": record.job_id,
        "fired_at": record.fired_at.isoformat() if record.fired_at else None,
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        "status": record.status,
        "trace_id": record.trace_id,
        "pre_kind": record.pre_kind,
        "pre_status": record.pre_status,
        "pre_run_id": record.pre_run_id,
        "pre_debug_session_id": record.pre_debug_session_id,
        "pre_result_json": record.pre_result_json,
        "pre_error": record.pre_error,
        "agent_session_id": record.agent_session_id,
        "agent_error": record.agent_error,
        "cron_task_kind": record.cron_task_kind,
        "delivery_status": record.delivery_status,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


class SqlAlchemyCronJobRunRepository:
    """CRUD for :class:`~doyoutrade.persistence.models.CronJobRunRecord`."""

    _UPDATE_WHITELIST = frozenset({
        "finished_at",
        "status",
        "trace_id",
        "pre_status",
        "pre_run_id",
        "pre_debug_session_id",
        "pre_result_json",
        "pre_error",
        "agent_session_id",
        "agent_error",
        "cron_task_kind",
        "delivery_status",
    })

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def create_run(self, data: dict[str, Any]) -> dict[str, Any]:
        async with self.session_factory() as session:
            fired_at = _to_naive_utc(data["fired_at"])
            started_at = _to_naive_utc(data.get("started_at", data["fired_at"]))
            record = CronJobRunRecord(
                id=data["id"],
                job_id=data["job_id"],
                fired_at=fired_at,
                started_at=started_at,
                status=data.get("status", "running"),
                pre_kind=data.get("pre_kind"),
                cron_task_kind=data.get("cron_task_kind"),
            )
            session.add(record)
            await session.commit()
            return _cron_job_run_dict(record)

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            row = await session.get(CronJobRunRecord, run_id)
            return _cron_job_run_dict(row) if row else None

    _DATETIME_FIELDS = frozenset({"fired_at", "started_at", "finished_at"})

    async def update_run(
        self,
        run_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            row = await session.get(CronJobRunRecord, run_id)
            if row is None:
                return None
            for k, v in updates.items():
                if k not in self._UPDATE_WHITELIST:
                    continue
                if k in self._DATETIME_FIELDS:
                    v = _to_naive_utc(v)
                setattr(row, k, v)
            await session.commit()
            return _cron_job_run_dict(row)

    async def list_for_job(
        self,
        job_id: str,
        *,
        limit: int = 50,
        before_fired_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            stmt = (
                select(CronJobRunRecord)
                .where(CronJobRunRecord.job_id == job_id)
                .order_by(CronJobRunRecord.fired_at.desc())
                .limit(limit)
            )
            if before_fired_at is not None:
                stmt = stmt.where(CronJobRunRecord.fired_at < before_fired_at)
            rows = (await session.execute(stmt)).scalars().all()
            return [_cron_job_run_dict(r) for r in rows]

    async def list_by_trace_id(self, trace_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Cron firings whose ``cron.job.fire`` span carried ``trace_id`` (newest first)."""
        async with self.session_factory() as session:
            stmt = (
                select(CronJobRunRecord)
                .where(CronJobRunRecord.trace_id == trace_id)
                .order_by(CronJobRunRecord.fired_at.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_cron_job_run_dict(r) for r in rows]


def _assistant_loaded_skill_dict(record: AssistantLoadedSkillRecord) -> dict[str, Any]:
    return {
        "session_id": record.session_id,
        "skill_name": record.skill_name,
        "skill_path": record.skill_path,
        "body": record.body,
        "body_hash": record.body_hash,
        "byte_size": record.byte_size,
        "loaded_at": record.loaded_at.isoformat() if record.loaded_at else None,
        "metadata_json": record.metadata_json,
    }


class SqlAlchemyAssistantLoadedSkillRepository:
    """Persistence for ``load_skill`` invocations in an assistant session.

    Backs the assistant service's compaction-resilient skill loading: when
    the context buffer is compacted the SKILL.md bodies that previously
    rode inside tool_result blocks are gone, so the service rebuilds a
    ``<system-reminder>`` from the rows in this table before the next
    model invocation. See ``AssistantLoadedSkillRecord`` for the schema
    rationale (composite PK, FK CASCADE).
    """

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def upsert(
        self,
        *,
        session_id: str,
        skill_name: str,
        skill_path: str,
        body: str,
        body_hash: str,
        metadata: dict | None = None,
    ) -> None:
        """Insert or overwrite the row for ``(session_id, skill_name)``.

        Portable get-then-write so this works on SQLite and Postgres
        without dialect-specific upsert syntax. Enforces non-empty
        ``session_id`` / ``skill_name`` per CLAUDE.md error-visibility
        rules — empty identifiers are a schema violation, not something
        we silently coerce.
        """
        if not session_id:
            raise ValueError(
                f"session_id must be non-empty, got {session_id!r}"
            )
        if not skill_name:
            raise ValueError(
                f"skill_name must be non-empty, got {skill_name!r}"
            )
        payload_metadata = dict(metadata or {})
        now = _utcnow()
        body_bytes = len(body.encode("utf-8"))
        async with self.session_factory() as session:
            existing = await session.get(
                AssistantLoadedSkillRecord, (session_id, skill_name)
            )
            if existing is None:
                row = AssistantLoadedSkillRecord(
                    session_id=session_id,
                    skill_name=skill_name,
                    skill_path=skill_path,
                    body=body,
                    body_hash=body_hash,
                    byte_size=body_bytes,
                    loaded_at=now,
                    metadata_json=payload_metadata,
                )
                session.add(row)
            else:
                existing.skill_path = skill_path
                existing.body = body
                existing.body_hash = body_hash
                existing.byte_size = body_bytes
                existing.loaded_at = now
                existing.metadata_json = payload_metadata
            await session.commit()

    async def list_by_session(self, session_id: str) -> list[dict[str, Any]]:
        """Return rows for a session ordered by ``loaded_at`` desc (newest first)."""
        if not session_id:
            raise ValueError(
                f"session_id must be non-empty, got {session_id!r}"
            )
        async with self.session_factory() as session:
            stmt = (
                select(AssistantLoadedSkillRecord)
                .where(AssistantLoadedSkillRecord.session_id == session_id)
                .order_by(AssistantLoadedSkillRecord.loaded_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_assistant_loaded_skill_dict(r) for r in rows]

    async def clear_for_session(self, session_id: str) -> int:
        """Delete all rows for a session; returns count. Normal path is FK CASCADE."""
        if not session_id:
            raise ValueError(
                f"session_id must be non-empty, got {session_id!r}"
            )
        async with self.session_factory() as session:
            stmt = delete(AssistantLoadedSkillRecord).where(
                AssistantLoadedSkillRecord.session_id == session_id
            )
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)


def _merge_cached_ranges(ranges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge overlapping / trading-day-adjacent ``(start, end)`` day ranges."""
    from doyoutrade.data.coverage_ranges import merge_cached_day_ranges

    return merge_cached_day_ranges(ranges)


def _bar_record_to_dict(record: CachedBarRecord) -> dict[str, Any]:
    return {
        "symbol": record.symbol,
        "timestamp": record.bar_timestamp,
        "open": float(record.open_price),
        "high": float(record.high_price),
        "low": float(record.low_price),
        "close": float(record.close_price),
        "volume": float(record.volume),
        "amount": float(record.amount) if record.amount is not None else None,
        "adjust_type": record.adjust,
    }


class SqlAlchemyCachedBarsRepository:
    """CRUD for the persistent OHLCV cache.

    Methods are keyed by ``(provider, symbol, interval)`` so the same
    symbol can be cached side-by-side across data sources without
    cross-source contamination — see ``cached_bars`` table docstring.
    """

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def covered_ranges(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[tuple[str, str]]:
        """Return merged ``(start, end)`` windows already fetched from upstream."""
        async with self.session_factory() as session:
            stmt = select(CachedBarRangeRecord).where(
                and_(
                    CachedBarRangeRecord.provider == provider,
                    CachedBarRangeRecord.symbol == symbol,
                    CachedBarRangeRecord.interval == interval,
                    CachedBarRangeRecord.adjust == adjust,
                )
            )
            rows = (await session.execute(stmt)).scalars().all()
        raw = [(r.range_start, r.range_end) for r in rows]
        return _merge_cached_ranges(raw)

    async def bars_in_range(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[dict[str, Any]]:
        """Return cached bars with ``start <= bar_timestamp[:10] <= end``, ordered ascending.

        Returned dicts use the same shape as :class:`doyoutrade.core.models.Bar`'s
        keyword arguments so the caller can ``Bar(**row)`` without extra
        translation.
        """
        # SQL filter uses the date-prefix lower bound only; the Python
        # post-filter is authoritative. A SQL upper bound is tricky
        # because ``bar_timestamp`` mixes ``YYYY-MM-DD`` (daily) and
        # ``YYYY-MM-DDTHH:MM:SS`` (intraday) — a naive ``<= end[:10]``
        # would exclude intraday bars on the end date.
        async with self.session_factory() as session:
            stmt = (
                select(CachedBarRecord)
                .where(
                    and_(
                        CachedBarRecord.provider == provider,
                        CachedBarRecord.symbol == symbol,
                        CachedBarRecord.interval == interval,
                        CachedBarRecord.adjust == adjust,
                        CachedBarRecord.bar_timestamp >= start[:10],
                    )
                )
                .order_by(CachedBarRecord.bar_timestamp)
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [
            _bar_record_to_dict(r)
            for r in rows
            if start[:10] <= r.bar_timestamp[:10] <= end[:10]
        ]

    async def suspended_days_in_range(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> set[str]:
        """Return the halted ``YYYY-MM-DD`` days persisted for ``[start, end]``."""
        async with self.session_factory() as session:
            stmt = select(CachedBarSuspensionRecord).where(
                and_(
                    CachedBarSuspensionRecord.provider == provider,
                    CachedBarSuspensionRecord.symbol == symbol,
                    CachedBarSuspensionRecord.interval == interval,
                    CachedBarSuspensionRecord.adjust == adjust,
                    CachedBarSuspensionRecord.suspended_day >= start[:10],
                    CachedBarSuspensionRecord.suspended_day <= end[:10],
                )
            )
            rows = (await session.execute(stmt)).scalars().all()
        return {r.suspended_day for r in rows}

    async def record_fetch(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        start: str,
        end: str,
        bars: list[dict[str, Any]],
        adjust: str = DEFAULT_BAR_ADJUST,
        suspended_days: set[str] | None = None,
    ) -> None:
        """Upsert *bars* and append a coverage range, then compact overlapping ranges.

        *bars* are dicts shaped like
        ``{"symbol", "timestamp", "open", "high", "low", "close",
        "volume", "amount" (optional)}``. Since ``adjust`` is now part of the PK,
        different 复权 modes are stored separately without conflict.

        *suspended_days* are the ``YYYY-MM-DD`` dates the upstream reported as
        halted (no tradeable bar) within ``[start, end]``. They are upserted
        into ``cached_bar_suspensions`` so a warm-cache replay can still tell a
        genuine halt apart from a missing-row data gap (see
        :class:`doyoutrade.persistence.models.CachedBarSuspensionRecord`).
        """
        now = _utcnow()
        async with self.session_factory() as session:
            for bar in bars:
                ts = str(bar.get("timestamp") or "").strip()
                if not ts:
                    continue
                pk = (provider, symbol, interval, adjust, ts)
                existing = await session.get(CachedBarRecord, pk)
                if existing is None:
                    session.add(
                        CachedBarRecord(
                            provider=provider,
                            symbol=symbol,
                            interval=interval,
                            adjust=adjust,
                            bar_timestamp=ts,
                            open_price=float(bar["open"]),
                            high_price=float(bar["high"]),
                            low_price=float(bar["low"]),
                            close_price=float(bar["close"]),
                            volume=float(bar["volume"]),
                            amount=(
                                float(bar["amount"])
                                if bar.get("amount") is not None
                                else None
                            ),
                            fetched_at=now,
                        )
                    )
                else:
                    existing.open_price = float(bar["open"])
                    existing.high_price = float(bar["high"])
                    existing.low_price = float(bar["low"])
                    existing.close_price = float(bar["close"])
                    existing.volume = float(bar["volume"])
                    existing.amount = (
                        float(bar["amount"]) if bar.get("amount") is not None else None
                    )
                    existing.fetched_at = now

            session.add(
                CachedBarRangeRecord(
                    provider=provider,
                    symbol=symbol,
                    interval=interval,
                    adjust=adjust,
                    range_start=start,
                    range_end=end,
                    fetched_at=now,
                )
            )

            for raw_day in suspended_days or ():
                day = str(raw_day or "").strip()[:10]
                if not day:
                    continue
                pk = (provider, symbol, interval, adjust, day)
                if await session.get(CachedBarSuspensionRecord, pk) is None:
                    session.add(
                        CachedBarSuspensionRecord(
                            provider=provider,
                            symbol=symbol,
                            interval=interval,
                            adjust=adjust,
                            suspended_day=day,
                            recorded_at=now,
                        )
                    )

            await session.commit()

        # Range compaction is best-effort and runs in its own
        # transaction so a failure here can't roll back the bar upsert
        # the caller just performed.
        try:
            await self._compact_ranges(provider=provider, symbol=symbol, interval=interval, adjust=adjust)
        except Exception:  # noqa: BLE001 — surface, but never lose the bars
            _LOG.warning(
                "cached_bar_ranges compaction failed for (%s, %s, %s, %s) — bars kept, "
                "extra range rows left in place",
                provider, symbol, interval, adjust,
                exc_info=True,
            )

    async def invalidate_symbol_cache(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> int:
        """Drop every cached bar AND coverage range under ``(provider, symbol, interval, adjust)``.

        Used by the adjust-drift self-heal (see
        :mod:`doyoutrade.data.adjust_drift`): a 除权/除息 event rescales the
        whole qfq history, so every stored bar for the key carries a stale
        factor — partial deletes would leave a price cliff in the cache.
        Returns the number of deleted bar rows. Plain ``DELETE ... WHERE``
        so it works on both SQLite and Postgres.
        """
        async with self.session_factory() as session:
            bar_result = await session.execute(
                delete(CachedBarRecord).where(
                    and_(
                        CachedBarRecord.provider == provider,
                        CachedBarRecord.symbol == symbol,
                        CachedBarRecord.interval == interval,
                        CachedBarRecord.adjust == adjust,
                    )
                )
            )
            await session.execute(
                delete(CachedBarRangeRecord).where(
                    and_(
                        CachedBarRangeRecord.provider == provider,
                        CachedBarRangeRecord.symbol == symbol,
                        CachedBarRangeRecord.interval == interval,
                        CachedBarRangeRecord.adjust == adjust,
                    )
                )
            )
            await session.execute(
                delete(CachedBarSuspensionRecord).where(
                    and_(
                        CachedBarSuspensionRecord.provider == provider,
                        CachedBarSuspensionRecord.symbol == symbol,
                        CachedBarSuspensionRecord.interval == interval,
                        CachedBarSuspensionRecord.adjust == adjust,
                    )
                )
            )
            await session.commit()
        removed = int(bar_result.rowcount)
        _LOG.info(
            "invalidate_symbol_cache: removed %d cached bars for (%s, %s, %s, %s)",
            removed, provider, symbol, interval, adjust,
        )
        return removed

    async def _compact_ranges(
        self,
        *,
        provider: str,
        symbol: str,
        interval: str,
        adjust: str,
    ) -> None:
        async with self.session_factory() as session:
            stmt = select(CachedBarRangeRecord).where(
                and_(
                    CachedBarRangeRecord.provider == provider,
                    CachedBarRangeRecord.symbol == symbol,
                    CachedBarRangeRecord.interval == interval,
                    CachedBarRangeRecord.adjust == adjust,
                )
            )
            rows = (await session.execute(stmt)).scalars().all()
            raw = [(r.range_start, r.range_end) for r in rows]
            merged = _merge_cached_ranges(raw)
            if len(merged) == len(rows):
                return
            await session.execute(
                delete(CachedBarRangeRecord).where(
                    and_(
                        CachedBarRangeRecord.provider == provider,
                        CachedBarRangeRecord.symbol == symbol,
                        CachedBarRangeRecord.interval == interval,
                        CachedBarRangeRecord.adjust == adjust,
                    )
                )
            )
            now = _utcnow()
            for start, end in merged:
                session.add(
                    CachedBarRangeRecord(
                        provider=provider,
                        symbol=symbol,
                        interval=interval,
                        adjust=adjust,
                        range_start=start,
                        range_end=end,
                        fetched_at=now,
                    )
                )
            await session.commit()


def _market_bar_time_from_timestamp(value: str, *, interval: str) -> datetime:
    if interval == "1d":
        return _market_bar_daily_time_from_source(value)
    raw = str(value).strip()
    if not raw:
        raise PersistenceError(
            f"market bar timestamp must be non-empty, got {type(value).__name__}: {value!r}"
        )
    raw_iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(raw_iso)
    except ValueError as exc:
        raise PersistenceError(
            f"market bar timestamp is invalid, got {type(value).__name__}: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise PersistenceError(
            "market bar timestamp for intraday interval must include timezone, "
            f"got {type(value).__name__}: {value!r}"
        )
    return parsed.astimezone(timezone.utc)


def _market_bar_daily_time_from_source(value: str) -> datetime:
    raw = str(value).strip()
    if len(raw) == 8 and raw.isdigit():
        date_part = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    elif len(raw) >= 10 and (len(raw) == 10 or raw[10] in {"T", " "}):
        date_part = raw[:10]
    else:
        raise PersistenceError(
            f"market bar timestamp is invalid, got {type(value).__name__}: {value!r}"
        )
    try:
        parsed_date = date.fromisoformat(date_part)
    except ValueError as exc:
        raise PersistenceError(
            f"market bar timestamp is invalid, got {type(value).__name__}: {value!r}"
        ) from exc
    return datetime.fromisoformat(f"{parsed_date.isoformat()}T00:00:00+00:00")


def _market_bar_timestamp_from_time(value: datetime, interval: str) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    if interval == "1d":
        return value.date().isoformat()
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _market_bar_daily_bound(value: datetime) -> datetime:
    return datetime.fromisoformat(f"{value.date().isoformat()}T00:00:00+00:00")


def _market_bar_record_to_dict(record: MarketBarRecord) -> dict[str, Any]:
    return {
        "symbol": record.symbol,
        "timestamp": _market_bar_timestamp_from_time(record.bar_time, record.interval),
        "open": float(record.open_price),
        "high": float(record.high_price),
        "low": float(record.low_price),
        "close": float(record.close_price),
        "volume": float(record.volume),
        "amount": float(record.amount) if record.amount is not None else None,
        "adjust_type": record.adjust,
    }


def _market_bar_sync_state_to_dict(
    record: MarketBarSyncStateRecord,
) -> dict[str, Any]:
    return {
        "symbol": record.symbol,
        "interval": record.interval,
        "provider": record.provider,
        "adjust": record.adjust,
        "target_start": record.target_start,
        "target_end": record.target_end,
        "covered_start": record.covered_start,
        "covered_end": record.covered_end,
        "last_success_at": record.last_success_at,
        "last_attempt_at": record.last_attempt_at,
        "last_error_code": record.last_error_code,
        "last_error_type": record.last_error_type,
        "last_error_message": record.last_error_message,
        "retry_count": int(record.retry_count),
        "status": record.status,
    }


def _require_market_bar_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistenceError(
            f"market bar {field} must be a non-empty string, "
            f"got {type(value).__name__}: {value!r}"
        )
    return value.strip()


def _require_market_bar_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistenceError(
            f"market bar identity field {field!r} must be a non-empty string, "
            f"got {type(value).__name__}: {value!r}"
        )
    return value.strip()


def _require_market_bar_float(bar: dict[str, Any], field: str) -> float:
    if field not in bar:
        raise PersistenceError(f"market bar missing required field {field!r}: {bar!r}")
    return _coerce_market_bar_finite_float(bar[field], field=field, required=True)


def _coerce_market_bar_finite_float(
    value: object,
    *,
    field: str,
    required: bool,
) -> float:
    if value is None:
        if required:
            raise PersistenceError(
                f"market bar field {field!r} must be numeric, "
                f"got {type(value).__name__}: {value!r}"
            )
        raise PersistenceError(
            f"market bar field {field!r} must be numeric when provided, "
            f"got {type(value).__name__}: {value!r}"
        )
    if isinstance(value, bool):
        raise PersistenceError(
            f"market bar field {field!r} must be numeric, "
            f"got {type(value).__name__}: {value!r}"
        )
    try:
        coerced = float(value)
    except (TypeError, ValueError) as exc:
        raise PersistenceError(
            f"market bar field {field!r} must be numeric, "
            f"got {type(value).__name__}: {value!r}"
        ) from exc
    if not math.isfinite(coerced):
        raise PersistenceError(
            f"market bar field {field!r} must be finite, "
            f"got {type(value).__name__}: {value!r}"
        )
    return coerced


def _optional_market_bar_float(bar: dict[str, Any], field: str) -> float | None:
    value = bar.get(field)
    if value is None:
        return None
    return _coerce_market_bar_finite_float(value, field=field, required=False)


# PostgreSQL/asyncpg cap a single statement at 32767 bind parameters; SQLite
# has its own (lower, version-dependent) limit. Stay well under both so a
# large multi-row bar upsert is split into several statements instead of
# raising InterfaceError. Headroom below 32767 leaves room for the dialect to
# add its own params (e.g. ON CONFLICT clauses).
_MARKET_BAR_MAX_BIND_PARAMS = 20000


def _market_bar_insert_for_dialect(dialect_name: str):
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        return insert
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        return insert
    raise PersistenceError(
        f"market bars upsert unsupported for dialect {dialect_name!r}"
    )


class SqlAlchemyMarketBarsRepository:
    """Repository for local market bars (SQLite or TimescaleDB) and sync coverage state."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def bars_in_range(
        self,
        *,
        provider: str,
        adjust: str,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        provider = _require_market_bar_identity(provider, "provider")
        adjust = _require_market_bar_identity(adjust, "adjust")
        symbol = _require_market_bar_identity(symbol, "symbol")
        interval = _require_market_bar_identity(interval, "interval")
        if interval == "1d":
            start = _market_bar_daily_bound(start)
            end = _market_bar_daily_bound(end)
        async with self.session_factory() as session:
            stmt = (
                select(MarketBarRecord)
                .where(
                    and_(
                        MarketBarRecord.provider == provider,
                        MarketBarRecord.adjust == adjust,
                        MarketBarRecord.symbol == symbol,
                        MarketBarRecord.interval == interval,
                        MarketBarRecord.bar_time >= start,
                        MarketBarRecord.bar_time <= end,
                    )
                )
                .order_by(MarketBarRecord.bar_time)
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [_market_bar_record_to_dict(row) for row in rows]

    async def upsert_bars(
        self,
        *,
        provider: str,
        adjust: str,
        interval: str,
        bars: list[dict[str, Any]],
    ) -> int:
        provider = _require_market_bar_identity(provider, "provider")
        adjust = _require_market_bar_identity(adjust, "adjust")
        interval = _require_market_bar_identity(interval, "interval")
        now = datetime.now(timezone.utc)
        upsert_rows: dict[tuple[str, str, str, str, datetime], dict[str, Any]] = {}
        for bar in bars:
            if not isinstance(bar, dict):
                raise PersistenceError(
                    "market bar payload must be a dict, "
                    f"got {type(bar).__name__}: {bar!r}"
                )
            symbol = _require_market_bar_text(bar.get("symbol"), "symbol")
            timestamp = _require_market_bar_text(bar.get("timestamp"), "timestamp")
            bar_time = _market_bar_time_from_timestamp(timestamp, interval=interval)
            row = {
                "symbol": symbol,
                "interval": interval,
                "provider": provider,
                "adjust": adjust,
                "bar_time": bar_time,
                "open_price": _require_market_bar_float(bar, "open"),
                "high_price": _require_market_bar_float(bar, "high"),
                "low_price": _require_market_bar_float(bar, "low"),
                "close_price": _require_market_bar_float(bar, "close"),
                "volume": _require_market_bar_float(bar, "volume"),
                "amount": _optional_market_bar_float(bar, "amount"),
                "source_fetched_at": now,
                "created_at": now,
                "updated_at": now,
            }
            upsert_rows[(symbol, interval, provider, adjust, bar_time)] = row
        if not upsert_rows:
            return 0

        rows = list(upsert_rows.values())
        # A single multi-row INSERT binds (len(rows) * columns_per_row)
        # parameters. asyncpg/PostgreSQL hard-cap a statement at 32767 bind
        # params, so a large backfill (e.g. a multi-year daily history for one
        # symbol) overflows one statement and asyncpg raises InterfaceError
        # ("the number of query arguments cannot exceed 32767"). Chunk the
        # rows so every statement stays well under the cap. SQLite has its own
        # (variable, often 32766/999) limit, so chunking helps both backends.
        columns_per_row = len(rows[0])
        chunk_size = max(1, _MARKET_BAR_MAX_BIND_PARAMS // columns_per_row)

        async with self.session_factory() as session:
            dialect_name = session.get_bind().dialect.name
            insert = _market_bar_insert_for_dialect(dialect_name)
            for start in range(0, len(rows), chunk_size):
                chunk = rows[start:start + chunk_size]
                stmt = insert(MarketBarRecord).values(chunk)
                excluded = stmt.excluded
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        MarketBarRecord.symbol,
                        MarketBarRecord.interval,
                        MarketBarRecord.provider,
                        MarketBarRecord.adjust,
                        MarketBarRecord.bar_time,
                    ],
                    set_={
                        "open_price": excluded.open_price,
                        "high_price": excluded.high_price,
                        "low_price": excluded.low_price,
                        "close_price": excluded.close_price,
                        "volume": excluded.volume,
                        "amount": excluded.amount,
                        "source_fetched_at": excluded.source_fetched_at,
                        "updated_at": excluded.updated_at,
                    },
                )
                await session.execute(stmt)
            # One commit for the whole batch keeps the upsert atomic across
            # chunks — a partial backfill must not leave a half-written range.
            await session.commit()
        return len(rows)

    async def get_sync_state(
        self,
        *,
        provider: str,
        adjust: str,
        symbol: str,
        interval: str,
    ) -> dict[str, Any] | None:
        provider = _require_market_bar_identity(provider, "provider")
        adjust = _require_market_bar_identity(adjust, "adjust")
        symbol = _require_market_bar_identity(symbol, "symbol")
        interval = _require_market_bar_identity(interval, "interval")
        async with self.session_factory() as session:
            record = await session.get(
                MarketBarSyncStateRecord,
                (symbol, interval, provider, adjust),
            )
            if record is None:
                return None
            return _market_bar_sync_state_to_dict(record)

    async def mark_sync_success(
        self,
        *,
        provider: str,
        adjust: str,
        symbol: str,
        interval: str,
        target_start: datetime,
        target_end: datetime,
        covered_start: datetime,
        covered_end: datetime,
    ) -> None:
        provider = _require_market_bar_identity(provider, "provider")
        adjust = _require_market_bar_identity(adjust, "adjust")
        symbol = _require_market_bar_identity(symbol, "symbol")
        interval = _require_market_bar_identity(interval, "interval")
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            pk = (symbol, interval, provider, adjust)
            record = await session.get(MarketBarSyncStateRecord, pk)
            if record is None:
                session.add(
                    MarketBarSyncStateRecord(
                        symbol=symbol,
                        interval=interval,
                        provider=provider,
                        adjust=adjust,
                        target_start=target_start,
                        target_end=target_end,
                        covered_start=covered_start,
                        covered_end=covered_end,
                        last_success_at=now,
                        last_attempt_at=now,
                        retry_count=0,
                        status="ok",
                    )
                )
            else:
                record.target_start = target_start
                record.target_end = target_end
                record.covered_start = covered_start
                record.covered_end = covered_end
                record.last_success_at = now
                record.last_attempt_at = now
                record.last_error_code = None
                record.last_error_type = None
                record.last_error_message = None
                record.retry_count = 0
                record.status = "ok"
            await session.commit()

    async def mark_sync_failure(
        self,
        *,
        provider: str,
        adjust: str,
        symbol: str,
        interval: str,
        target_start: datetime,
        target_end: datetime,
        error_code: str,
        error_type: str,
        error_message: str,
    ) -> None:
        provider = _require_market_bar_identity(provider, "provider")
        adjust = _require_market_bar_identity(adjust, "adjust")
        symbol = _require_market_bar_identity(symbol, "symbol")
        interval = _require_market_bar_identity(interval, "interval")
        now = datetime.now(timezone.utc)
        async with self.session_factory() as session:
            pk = (symbol, interval, provider, adjust)
            record = await session.get(MarketBarSyncStateRecord, pk)
            if record is None:
                session.add(
                    MarketBarSyncStateRecord(
                        symbol=symbol,
                        interval=interval,
                        provider=provider,
                        adjust=adjust,
                        target_start=target_start,
                        target_end=target_end,
                        last_attempt_at=now,
                        last_error_code=error_code,
                        last_error_type=error_type,
                        last_error_message=error_message,
                        retry_count=1,
                        status="failed",
                    )
                )
            else:
                record.target_start = target_start
                record.target_end = target_end
                record.last_attempt_at = now
                record.last_error_code = error_code
                record.last_error_type = error_type
                record.last_error_message = error_message
                record.retry_count = int(record.retry_count) + 1
                record.status = "failed"
            await session.commit()


# --------------------------------------------------------------------------
# Decision signals (决策信号落库 → 回测验证闭环)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DecisionSignalSnapshot:
    """Immutable view of a ``decision_signals`` row."""

    id: str
    task_id: str | None
    run_id: str | None
    cycle_run_id: str | None
    trace_id: str | None
    session_id: str | None
    source: str
    symbol: str
    action: str
    confidence: float | None
    score: float | None
    horizon: str
    entry_low: str | None
    entry_high: str | None
    stop_loss: str | None
    target_price: str | None
    reason: str | None
    status: str
    expires_at: datetime | None
    metadata_json: dict | None
    dedupe_key: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DecisionSignalOutcomeSnapshot:
    """Immutable view of a ``decision_signal_outcomes`` row."""

    id: int
    signal_id: str
    horizon: str
    engine_version: str
    outcome: str
    direction_expected: str
    direction_correct: bool | None
    anchor_date: str
    eval_window_days: int
    entry_price: float | None
    exit_price: float | None
    max_gain_pct: float | None
    max_drawdown_pct: float | None
    return_pct: float | None
    created_at: datetime


def _decision_signal_snapshot(record: DecisionSignalRecord) -> DecisionSignalSnapshot:
    return DecisionSignalSnapshot(
        id=record.id,
        task_id=record.task_id,
        run_id=record.run_id,
        cycle_run_id=record.cycle_run_id,
        trace_id=record.trace_id,
        session_id=record.session_id,
        source=str(record.source),
        symbol=str(record.symbol),
        action=str(record.action),
        confidence=record.confidence,
        score=record.score,
        horizon=str(record.horizon),
        entry_low=record.entry_low,
        entry_high=record.entry_high,
        stop_loss=record.stop_loss,
        target_price=record.target_price,
        reason=record.reason,
        status=str(record.status),
        expires_at=record.expires_at,
        metadata_json=dict(record.metadata_json) if isinstance(record.metadata_json, dict) else None,
        dedupe_key=str(record.dedupe_key),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _decision_signal_outcome_snapshot(
    record: DecisionSignalOutcomeRecord,
) -> DecisionSignalOutcomeSnapshot:
    return DecisionSignalOutcomeSnapshot(
        id=int(record.id),
        signal_id=str(record.signal_id),
        horizon=str(record.horizon),
        engine_version=str(record.engine_version),
        outcome=str(record.outcome),
        direction_expected=str(record.direction_expected),
        direction_correct=record.direction_correct,
        anchor_date=str(record.anchor_date),
        eval_window_days=int(record.eval_window_days),
        entry_price=record.entry_price,
        exit_price=record.exit_price,
        max_gain_pct=record.max_gain_pct,
        max_drawdown_pct=record.max_drawdown_pct,
        return_pct=record.return_pct,
        created_at=record.created_at,
    )


_DECISION_SIGNAL_ACTIONS = frozenset(
    {"buy", "sell", "hold", "add", "reduce", "watch", "take_profit", "stop_loss"}
)
_DECISION_SIGNAL_SOURCES = frozenset({"strategy", "backtest", "assistant"})
_DECISION_SIGNAL_STATUSES = frozenset({"active", "expired", "invalidated", "evaluated"})
_DECISION_SIGNAL_OUTCOMES = frozenset({"hit", "miss", "neutral"})


class SqlAlchemyDecisionSignalRepository:
    """CRUD + idempotent upserts for ``decision_signals`` / ``decision_signal_outcomes``.

    Dumb persistence with strict-shape guards: invalid enum values and missing
    attribution raise ``ValueError`` with the actual type + value (CLAUDE.md
    §错误可见性 — no silent coercion). Idempotency:

    - ``create_if_absent`` derives ``dedupe_key`` from
      ``(run_id or trace_id or session_id, symbol, action, horizon)``; a
      duplicate returns the existing row with ``created=False`` (select-first,
      IntegrityError fallback covers the concurrent-insert race).
    - ``upsert_outcome`` is keyed by ``(signal_id, horizon, engine_version)``;
      an existing row is updated in place.
    """

    def __init__(self, session_factory):
        self.session_factory = session_factory

    # -- validation -----------------------------------------------------

    @staticmethod
    def _guard_enum(name: str, value: object, allowed: frozenset[str]) -> str:
        if not isinstance(value, str) or value not in allowed:
            raise ValueError(
                f"{name} must be one of {sorted(allowed)}, "
                f"got {type(value).__name__}: {value!r}"
            )
        return value

    @staticmethod
    def build_dedupe_key(
        *,
        run_id: str | None,
        trace_id: str | None,
        session_id: str | None,
        symbol: str,
        action: str,
        horizon: str,
    ) -> str:
        """Normalized idempotency key: first non-empty attribution id + business triple."""
        scope = next(
            (str(v).strip() for v in (run_id, trace_id, session_id) if v and str(v).strip()),
            None,
        )
        if scope is None:
            raise ValueError(
                "decision signal requires at least one attribution id "
                "(run_id, trace_id or session_id) to build a dedupe_key; got all empty"
            )
        return f"{scope}|{symbol.strip()}|{action.strip()}|{horizon.strip()}"

    # -- writes ----------------------------------------------------------

    async def create_if_absent(self, **fields) -> tuple[DecisionSignalSnapshot, bool]:
        """Idempotently insert a signal. Returns ``(snapshot, created)``."""
        symbol = fields.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError(
                f"symbol must be a non-empty string, got {type(symbol).__name__}: {symbol!r}"
            )
        action = self._guard_enum("action", fields.get("action"), _DECISION_SIGNAL_ACTIONS)
        source = self._guard_enum("source", fields.get("source"), _DECISION_SIGNAL_SOURCES)
        status = fields.get("status", "active")
        self._guard_enum("status", status, _DECISION_SIGNAL_STATUSES)
        horizon = str(fields.get("horizon") or "5d").strip() or "5d"
        metadata_json = fields.get("metadata_json")
        if metadata_json is not None and not isinstance(metadata_json, dict):
            raise ValueError(
                "metadata_json must be a dict or None, "
                f"got {type(metadata_json).__name__}: {metadata_json!r}"
            )
        dedupe_key = self.build_dedupe_key(
            run_id=fields.get("run_id"),
            trace_id=fields.get("trace_id"),
            session_id=fields.get("session_id"),
            symbol=symbol,
            action=action,
            horizon=horizon,
        )
        signal_id = fields.pop("id", None) or f"dsig-{uuid.uuid4().hex[:12]}"
        fields.update(
            symbol=symbol.strip(), action=action, source=source,
            status=status, horizon=horizon, dedupe_key=dedupe_key,
        )
        async with self.session_factory() as session:
            existing = await session.execute(
                select(DecisionSignalRecord).where(
                    DecisionSignalRecord.dedupe_key == dedupe_key
                )
            )
            row = existing.scalar_one_or_none()
            if row is not None:
                return _decision_signal_snapshot(row), False
            record = DecisionSignalRecord(id=signal_id, **fields)
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                if _is_unique_violation(error):
                    # Concurrent insert of the same dedupe_key won the race —
                    # return the winner (still idempotent).
                    retry = await session.execute(
                        select(DecisionSignalRecord).where(
                            DecisionSignalRecord.dedupe_key == dedupe_key
                        )
                    )
                    winner = retry.scalar_one_or_none()
                    if winner is not None:
                        return _decision_signal_snapshot(winner), False
                    raise _constraint_conflict(
                        f"decision signal already exists: {dedupe_key}",
                    ) from error
                raise _persistence_error(
                    f"failed to create decision signal: {_integrity_message(error)}",
                ) from error
            return _decision_signal_snapshot(record), True

    async def update_status(self, signal_id: str, status: str) -> DecisionSignalSnapshot:
        self._guard_enum("status", status, _DECISION_SIGNAL_STATUSES)
        async with self.session_factory() as session:
            record = await session.get(DecisionSignalRecord, signal_id)
            if record is None:
                raise RecordNotFoundError(f"decision signal not found: {signal_id}")
            record.status = status
            record.updated_at = _utcnow()
            await session.commit()
            return _decision_signal_snapshot(record)

    async def expire_due_signals(self, now: datetime | None = None) -> int:
        """Lazy expiry: flip ``active`` signals whose ``expires_at`` passed to ``expired``."""
        cutoff = now or _utcnow()
        async with self.session_factory() as session:
            result = await session.execute(
                update(DecisionSignalRecord)
                .where(
                    DecisionSignalRecord.status == "active",
                    DecisionSignalRecord.expires_at.is_not(None),
                    DecisionSignalRecord.expires_at < cutoff,
                )
                .values(status="expired", updated_at=_utcnow())
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def upsert_outcome(self, **fields) -> DecisionSignalOutcomeSnapshot:
        """Idempotent write keyed by ``(signal_id, horizon, engine_version)``."""
        signal_id = fields.get("signal_id")
        if not isinstance(signal_id, str) or not signal_id.strip():
            raise ValueError(
                f"signal_id must be a non-empty string, got {type(signal_id).__name__}: {signal_id!r}"
            )
        self._guard_enum("outcome", fields.get("outcome"), _DECISION_SIGNAL_OUTCOMES)
        horizon = str(fields.get("horizon") or "").strip()
        if not horizon:
            raise ValueError(f"horizon must be a non-empty string, got {fields.get('horizon')!r}")
        engine_version = str(fields.get("engine_version") or "v1").strip() or "v1"
        fields.update(signal_id=signal_id, horizon=horizon, engine_version=engine_version)
        async with self.session_factory() as session:
            existing = await session.execute(
                select(DecisionSignalOutcomeRecord).where(
                    and_(
                        DecisionSignalOutcomeRecord.signal_id == signal_id,
                        DecisionSignalOutcomeRecord.horizon == horizon,
                        DecisionSignalOutcomeRecord.engine_version == engine_version,
                    )
                )
            )
            record = existing.scalar_one_or_none()
            if record is not None:
                for key, value in fields.items():
                    setattr(record, key, value)
                await session.commit()
                return _decision_signal_outcome_snapshot(record)
            record = DecisionSignalOutcomeRecord(**fields)
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise _persistence_error(
                    f"failed to upsert decision signal outcome: {_integrity_message(error)}",
                ) from error
            return _decision_signal_outcome_snapshot(record)

    # -- reads -----------------------------------------------------------

    async def get_signal(self, signal_id: str) -> DecisionSignalSnapshot:
        async with self.session_factory() as session:
            record = await session.get(DecisionSignalRecord, signal_id)
            if record is None:
                raise RecordNotFoundError(f"decision signal not found: {signal_id}")
            return _decision_signal_snapshot(record)

    async def list_signals(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
        symbol: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[DecisionSignalSnapshot], int]:
        conditions = []
        if task_id:
            conditions.append(DecisionSignalRecord.task_id == task_id)
        if run_id:
            conditions.append(DecisionSignalRecord.run_id == run_id)
        if symbol:
            conditions.append(DecisionSignalRecord.symbol == symbol)
        if status:
            self._guard_enum("status", status, _DECISION_SIGNAL_STATUSES)
            conditions.append(DecisionSignalRecord.status == status)
        where_clause = and_(*conditions) if conditions else true()
        async with self.session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(DecisionSignalRecord).where(where_clause)
            )
            result = await session.execute(
                select(DecisionSignalRecord)
                .where(where_clause)
                .order_by(DecisionSignalRecord.created_at.desc(), DecisionSignalRecord.id.desc())
                .offset(max(0, int(offset)))
                .limit(max(1, int(limit)))
            )
            rows = result.scalars().all()
            return [_decision_signal_snapshot(r) for r in rows], int(total or 0)

    async def list_outcomes(self, signal_id: str) -> list[DecisionSignalOutcomeSnapshot]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(DecisionSignalOutcomeRecord)
                .where(DecisionSignalOutcomeRecord.signal_id == signal_id)
                .order_by(DecisionSignalOutcomeRecord.created_at.asc())
            )
            return [_decision_signal_outcome_snapshot(r) for r in result.scalars().all()]


# ---------------------------------------------------------------------------
# Knowledge graph — kg_nodes / kg_edges / kg_source_state
# ---------------------------------------------------------------------------

_KG_PROVENANCES = frozenset({"deterministic", "llm", "manual"})

#: apply_projection 一次 IN 查询的分片大小（SQLite 变量上限保守值）。
_KG_IN_CHUNK = 400


@dataclass(frozen=True)
class KnowledgeGraphNodeSpec:
    """投影层产出的节点意图 — 自然键 ``(node_type, name)`` 定身份。"""

    node_type: str
    name: str
    display_name: str | None = None
    attrs: dict | None = None


@dataclass(frozen=True)
class KnowledgeGraphEdgeSpec:
    """投影层产出的边意图。``src`` / ``dst`` 是 ``(node_type, name)`` 自然键。

    ``dedupe_key`` 是事实身份（幂等键）；``state_key`` 非空时表示该边属于
    单值状态组：apply 时同组内 dedupe_key 不在本次投影里的 active 边会被
    置为 expired（角色变更即由此产生失效历史）。
    """

    src: tuple[str, str]
    dst: tuple[str, str]
    relation: str
    fact: str
    dedupe_key: str
    state_key: str | None = None
    attrs: dict | None = None
    provenance: str = "deterministic"
    confidence: float | None = None
    source_key: str | None = None
    source_ref: str | None = None
    valid_at: datetime | None = None
    invalid_at: datetime | None = None


@dataclass(frozen=True)
class KnowledgeGraphNodeSnapshot:
    """Immutable view of a ``kg_nodes`` row."""

    id: str
    node_type: str
    name: str
    display_name: str | None
    attrs: dict | None
    created_at: datetime
    updated_at: datetime
    status: str = "active"
    retired_at: datetime | None = None
    redirect_to_id: str | None = None


@dataclass(frozen=True)
class KnowledgeGraphEdgeSnapshot:
    """Immutable view of a ``kg_edges`` row."""

    id: str
    src_id: str
    dst_id: str
    relation: str
    fact: str
    attrs: dict | None
    dedupe_key: str
    state_key: str | None
    provenance: str
    confidence: float | None
    source_key: str | None
    source_ref: str | None
    valid_at: datetime | None
    invalid_at: datetime | None
    created_at: datetime
    expired_at: datetime | None


@dataclass(frozen=True)
class KnowledgeGraphSourceStateSnapshot:
    """Immutable view of a ``kg_source_state`` row."""

    source: str
    content_hash: str
    synced_at: datetime
    stats: dict | None


def _kg_node_snapshot(record: KnowledgeGraphNodeRecord) -> KnowledgeGraphNodeSnapshot:
    return KnowledgeGraphNodeSnapshot(
        id=record.id,
        node_type=record.node_type,
        name=record.name,
        display_name=record.display_name,
        attrs=record.attrs,
        created_at=record.created_at,
        updated_at=record.updated_at,
        status=getattr(record, "status", None) or "active",
        retired_at=getattr(record, "retired_at", None),
        redirect_to_id=getattr(record, "redirect_to_id", None),
    )


def _kg_edge_snapshot(record: KnowledgeGraphEdgeRecord) -> KnowledgeGraphEdgeSnapshot:
    return KnowledgeGraphEdgeSnapshot(
        id=record.id,
        src_id=record.src_id,
        dst_id=record.dst_id,
        relation=record.relation,
        fact=record.fact,
        attrs=record.attrs,
        dedupe_key=record.dedupe_key,
        state_key=record.state_key,
        provenance=record.provenance,
        confidence=record.confidence,
        source_key=record.source_key,
        source_ref=record.source_ref,
        valid_at=record.valid_at,
        invalid_at=record.invalid_at,
        created_at=record.created_at,
        expired_at=record.expired_at,
    )


def _chunked(values: list, size: int = _KG_IN_CHUNK):
    for i in range(0, len(values), size):
        yield values[i : i + size]


class SqlAlchemyKnowledgeGraphRepository:
    """CRUD + 幂等投影写入 for ``kg_nodes`` / ``kg_edges`` / ``kg_source_state``.

    Dumb persistence with strict-shape guards（CLAUDE.md §错误可见性 —— 非法
    provenance / 缺失端点节点直接 ``ValueError`` 带实际类型与值，不静默
    coercion）。核心入口 :meth:`apply_projection` 是幂等的：

    - 节点按自然键 ``(node_type, name)`` upsert（display_name / attrs 变更
      时原地更新）。
    - 边按 ``dedupe_key`` 幂等：内容未变 → 不动；内容变了 → 旧边置
      ``expired_at`` + 插入新边（保留历史，支持时点回溯）。
    - ``state_key`` 单值状态组：本次投影覆盖到的组里，dedupe_key 不在
      本次集合中的 active 边一律置 expired（个股角色变更的失效语义）。

    ``now`` 由调用方显式传入（库内不调 ``datetime.now()``，与 roles.py 的
    纪律一致），测试传固定值。
    """

    def __init__(self, session_factory):
        self.session_factory = session_factory

    # -- projection write ------------------------------------------------

    async def apply_projection(
        self,
        nodes: list[KnowledgeGraphNodeSpec],
        edges: list[KnowledgeGraphEdgeSpec],
        *,
        now: datetime,
        reconcile_source_keys: set[str] | None = None,
        source_hashes: dict[str, str] | None = None,
    ) -> dict[str, int]:
        """Idempotently apply one projection batch and its source watermarks.

        ``reconcile_source_keys`` identifies complete source snapshots. Active
        edges owned by one of those sources but absent from ``edges`` are
        expired in the same transaction. ``source_hashes`` advances watermarks
        atomically with the graph mutations, preventing half-applied syncs.
        """

        reconcile_source_keys = set(reconcile_source_keys or ())
        source_hashes = dict(source_hashes or {})

        for spec in nodes:
            if not isinstance(spec, KnowledgeGraphNodeSpec):
                raise ValueError(
                    f"nodes must be KnowledgeGraphNodeSpec, got {type(spec).__name__}: {spec!r}"
                )
            if not spec.node_type or not spec.name:
                raise ValueError(
                    f"node spec requires non-empty node_type and name, got "
                    f"({spec.node_type!r}, {spec.name!r})"
                )
        node_keys = {(s.node_type, s.name) for s in nodes}
        for spec in edges:
            if not isinstance(spec, KnowledgeGraphEdgeSpec):
                raise ValueError(
                    f"edges must be KnowledgeGraphEdgeSpec, got {type(spec).__name__}: {spec!r}"
                )
            if spec.provenance not in _KG_PROVENANCES:
                raise ValueError(
                    f"edge provenance must be one of {sorted(_KG_PROVENANCES)}, "
                    f"got {type(spec.provenance).__name__}: {spec.provenance!r}"
                )
            for endpoint, label in ((spec.src, "src"), (spec.dst, "dst")):
                if endpoint not in node_keys:
                    # 端点必须随批携带节点 spec —— 缺失说明投影层漏了实体，
                    # 这是编程错误，必须炸而不是静默丢边。
                    raise ValueError(
                        f"edge {spec.dedupe_key!r} references {label} node "
                        f"{endpoint!r} that is not part of this projection batch"
                    )
            if not spec.dedupe_key or not spec.relation or not spec.fact:
                raise ValueError(
                    f"edge spec requires non-empty dedupe_key/relation/fact, got "
                    f"dedupe_key={spec.dedupe_key!r} relation={spec.relation!r}"
                )
        for source, digest in source_hashes.items():
            if not source or not isinstance(digest, str) or not digest:
                raise ValueError(
                    "source_hashes requires non-empty string keys and digests, "
                    f"got source={source!r} digest={digest!r}"
                )

        dedupe_keys = [s.dedupe_key for s in edges]
        if len(set(dedupe_keys)) != len(dedupe_keys):
            seen: set[str] = set()
            dup = next(k for k in dedupe_keys if k in seen or seen.add(k))  # type: ignore[func-returns-value]
            raise ValueError(f"projection batch contains duplicate dedupe_key: {dup!r}")

        stats = {
            "nodes_created": 0,
            "nodes_updated": 0,
            "edges_created": 0,
            "edges_unchanged": 0,
            "edges_expired": 0,
        }

        async with self.session_factory() as session:
            # ---- nodes: upsert by natural key --------------------------------
            id_by_key: dict[tuple[str, str], str] = {}
            existing_nodes: dict[tuple[str, str], KnowledgeGraphNodeRecord] = {}
            key_list = sorted(node_keys)
            for chunk in _chunked(key_list):
                result = await session.execute(
                    select(KnowledgeGraphNodeRecord).where(
                        or_(
                            *[
                                and_(
                                    KnowledgeGraphNodeRecord.node_type == t,
                                    KnowledgeGraphNodeRecord.name == n,
                                )
                                for (t, n) in chunk
                            ]
                        )
                    )
                )
                for record in result.scalars().all():
                    existing_nodes[(record.node_type, record.name)] = record

            for spec in nodes:
                key = (spec.node_type, spec.name)
                record = existing_nodes.get(key)
                if record is None:
                    record = KnowledgeGraphNodeRecord(
                        id=f"kgn-{uuid.uuid4().hex[:12]}",
                        node_type=spec.node_type,
                        name=spec.name,
                        display_name=spec.display_name,
                        attrs=spec.attrs,
                        status="active",
                        retired_at=None,
                        redirect_to_id=None,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(record)
                    existing_nodes[key] = record
                    stats["nodes_created"] += 1
                else:
                    status = getattr(record, "status", "active") or "active"
                    if status == "merged" and getattr(record, "redirect_to_id", None):
                        survivor = await session.get(
                            KnowledgeGraphNodeRecord,
                            record.redirect_to_id,
                        )
                        if survivor is not None:
                            id_by_key[key] = survivor.id
                            continue
                    if status != "active":
                        # Do not revive retired/merged entities from projection.
                        id_by_key[key] = record.id
                        continue
                    changed = False
                    if spec.display_name is not None and spec.display_name != record.display_name:
                        record.display_name = spec.display_name
                        changed = True
                    if spec.attrs is not None and spec.attrs != record.attrs:
                        record.attrs = spec.attrs
                        changed = True
                    if changed:
                        record.updated_at = now
                        stats["nodes_updated"] += 1
                id_by_key[key] = record.id

            # ---- edges: dedupe-keyed idempotent write -------------------------
            active_by_dedupe: dict[str, KnowledgeGraphEdgeRecord] = {}
            for chunk in _chunked(dedupe_keys):
                result = await session.execute(
                    select(KnowledgeGraphEdgeRecord).where(
                        KnowledgeGraphEdgeRecord.dedupe_key.in_(chunk),
                        KnowledgeGraphEdgeRecord.expired_at.is_(None),
                    )
                )
                for record in result.scalars().all():
                    prior = active_by_dedupe.get(record.dedupe_key)
                    if prior is not None:
                        # 同 dedupe_key 出现两条 active 边：历史 bug 留下的
                        # 脏状态。修复动作（expire 较旧一条）必须可见。
                        _LOG.warning(
                            "kg apply_projection found duplicate active edges "
                            "dedupe_key=%r ids=(%s, %s); expiring the older one",
                            record.dedupe_key, prior.id, record.id,
                        )
                        older, newer = (
                            (prior, record)
                            if prior.created_at <= record.created_at
                            else (record, prior)
                        )
                        older.expired_at = now
                        stats["edges_expired"] += 1
                        active_by_dedupe[record.dedupe_key] = newer
                    else:
                        active_by_dedupe[record.dedupe_key] = record

            def _edge_content(spec: KnowledgeGraphEdgeSpec) -> tuple:
                return (
                    id_by_key[spec.src],
                    id_by_key[spec.dst],
                    spec.relation,
                    spec.fact,
                    spec.attrs,
                    spec.state_key,
                    spec.provenance,
                    spec.confidence,
                    spec.source_key or spec.source_ref,
                    spec.source_ref,
                    spec.valid_at,
                    spec.invalid_at,
                )

            def _record_content(record: KnowledgeGraphEdgeRecord) -> tuple:
                return (
                    record.src_id,
                    record.dst_id,
                    record.relation,
                    record.fact,
                    record.attrs,
                    record.state_key,
                    record.provenance,
                    record.confidence,
                    record.source_key,
                    record.source_ref,
                    record.valid_at,
                    record.invalid_at,
                )

            for spec in edges:
                existing = active_by_dedupe.get(spec.dedupe_key)
                if existing is not None and existing.provenance == "manual":
                    # Manual overlays win; projection must not expire them.
                    stats["edges_unchanged"] += 1
                    continue
                if existing is not None and _record_content(existing) == _edge_content(spec):
                    stats["edges_unchanged"] += 1
                    continue
                if existing is not None:
                    existing.expired_at = now
                    stats["edges_expired"] += 1
                session.add(
                    KnowledgeGraphEdgeRecord(
                        id=f"kge-{uuid.uuid4().hex[:12]}",
                        src_id=id_by_key[spec.src],
                        dst_id=id_by_key[spec.dst],
                        relation=spec.relation,
                        fact=spec.fact,
                        attrs=spec.attrs,
                        dedupe_key=spec.dedupe_key,
                        state_key=spec.state_key,
                        provenance=spec.provenance,
                        confidence=spec.confidence,
                        source_key=spec.source_key or spec.source_ref,
                        source_ref=spec.source_ref,
                        valid_at=spec.valid_at,
                        invalid_at=spec.invalid_at,
                        created_at=now,
                        expired_at=None,
                    )
                )
                stats["edges_created"] += 1

            # ---- state groups: expire superseded single-value edges ----------
            incoming_dedupe = set(dedupe_keys)
            state_keys = sorted({s.state_key for s in edges if s.state_key})
            for chunk in _chunked(state_keys):
                result = await session.execute(
                    select(KnowledgeGraphEdgeRecord).where(
                        KnowledgeGraphEdgeRecord.state_key.in_(chunk),
                        KnowledgeGraphEdgeRecord.expired_at.is_(None),
                    )
                )
                for record in result.scalars().all():
                    if record.dedupe_key in incoming_dedupe:
                        continue
                    if record.provenance == "manual":
                        continue
                    # 单值状态组里出现了不在本次投影中的旧状态（如角色已
                    # 从 龙头 变为 杂毛）——按 bi-temporal 语义置 expired，
                    # 保留历史行。
                    record.expired_at = now
                    stats["edges_expired"] += 1
                    _LOG.info(
                        "kg apply_projection expired superseded state edge "
                        "state_key=%r dedupe_key=%r", record.state_key, record.dedupe_key,
                    )

            # ---- complete source snapshots: expire facts no longer emitted ---
            incoming_by_source: dict[str, set[str]] = {
                source: set() for source in reconcile_source_keys
            }
            for spec in edges:
                source_key = spec.source_key or spec.source_ref
                if source_key in incoming_by_source:
                    incoming_by_source[source_key].add(spec.dedupe_key)
            for source_chunk in _chunked(sorted(reconcile_source_keys)):
                result = await session.execute(
                    select(KnowledgeGraphEdgeRecord).where(
                        KnowledgeGraphEdgeRecord.source_key.in_(source_chunk),
                        KnowledgeGraphEdgeRecord.expired_at.is_(None),
                    )
                )
                for record in result.scalars().all():
                    if record.dedupe_key in incoming_by_source[record.source_key]:
                        continue
                    if record.provenance == "manual":
                        continue
                    record.expired_at = now
                    stats["edges_expired"] += 1
                    _LOG.info(
                        "kg apply_projection expired removed source edge "
                        "source_key=%r dedupe_key=%r",
                        record.source_key,
                        record.dedupe_key,
                    )

            # ---- source watermarks: commit atomically with graph mutations ---
            if source_hashes:
                state_records: dict[str, KnowledgeGraphSourceStateRecord] = {}
                for source_chunk in _chunked(sorted(source_hashes)):
                    result = await session.execute(
                        select(KnowledgeGraphSourceStateRecord).where(
                            KnowledgeGraphSourceStateRecord.source.in_(source_chunk)
                        )
                    )
                    state_records.update(
                        {record.source: record for record in result.scalars().all()}
                    )
                for source, digest in sorted(source_hashes.items()):
                    record = state_records.get(source)
                    if record is None:
                        session.add(
                            KnowledgeGraphSourceStateRecord(
                                source=source,
                                content_hash=digest,
                                synced_at=now,
                                stats=dict(stats),
                            )
                        )
                    else:
                        record.content_hash = digest
                        record.synced_at = now
                        record.stats = dict(stats)

            mutation_count = (
                stats["nodes_created"]
                + stats["nodes_updated"]
                + stats["edges_created"]
                + stats["edges_expired"]
            )
            if source_hashes and mutation_count:
                state_result = await session.execute(
                    select(KnowledgeGraphStateRecord)
                    .where(KnowledgeGraphStateRecord.state_key == "default")
                    .with_for_update()
                )
                graph_state = state_result.scalar_one_or_none()
                if graph_state is None:
                    graph_state = KnowledgeGraphStateRecord(
                        state_key="default",
                        head_revision=0,
                        updated_at=now,
                    )
                    session.add(graph_state)
                    await session.flush()
                parent_revision = graph_state.head_revision
                revision = parent_revision + 1
                change_set_id = f"kgcs-{uuid.uuid4().hex[:12]}"
                proposal_body = json.dumps(
                    {
                        "source_hashes": source_hashes,
                        "stats": stats,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                session.add(
                    KnowledgeGraphChangeSetRecord(
                        id=change_set_id,
                        status="applied",
                        actor_type="system",
                        actor_id="source-ingestion",
                        base_revision=parent_revision,
                        revision=revision,
                        proposal_hash=hashlib.sha256(
                            proposal_body.encode("utf-8")
                        ).hexdigest(),
                        summary=(
                            "自动图谱投影："
                            + ", ".join(sorted(source_hashes))
                        ),
                        created_at=now,
                        applied_at=now,
                        rejected_at=None,
                    )
                )
                # Postgres enforces FKs on flush; without an ORM relationship,
                # child rows (operations / revisions) can be INSERTed before the
                # parent change_set. Flush the parent first so sync works on PG
                # (SQLite often skips FK checks and hides this).
                await session.flush()
                session.add(
                    KnowledgeGraphChangeOperationRecord(
                        id=f"kgop-{uuid.uuid4().hex[:12]}",
                        change_set_id=change_set_id,
                        position=0,
                        op_type="system_projection",
                        target_id=None,
                        before_json=None,
                        after_json={
                            "source_hashes": source_hashes,
                            "stats": dict(stats),
                        },
                    )
                )
                session.add(
                    KnowledgeGraphRevisionRecord(
                        revision=revision,
                        parent_revision=parent_revision,
                        change_set_id=change_set_id,
                        created_at=now,
                    )
                )
                graph_state.head_revision = revision
                graph_state.updated_at = now

            await session.commit()
        return stats

    # -- source watermarks ------------------------------------------------

    async def get_source_state(self, source: str) -> KnowledgeGraphSourceStateSnapshot | None:
        async with self.session_factory() as session:
            record = await session.get(KnowledgeGraphSourceStateRecord, source)
            if record is None:
                return None
            return KnowledgeGraphSourceStateSnapshot(
                source=record.source,
                content_hash=record.content_hash,
                synced_at=record.synced_at,
                stats=record.stats,
            )

    async def set_source_state(
        self,
        source: str,
        content_hash: str,
        *,
        now: datetime,
        stats: dict | None = None,
    ) -> None:
        if not source or not content_hash:
            raise ValueError(
                f"source and content_hash must be non-empty, got "
                f"source={source!r} content_hash={content_hash!r}"
            )
        async with self.session_factory() as session:
            record = await session.get(KnowledgeGraphSourceStateRecord, source)
            if record is None:
                session.add(
                    KnowledgeGraphSourceStateRecord(
                        source=source,
                        content_hash=content_hash,
                        synced_at=now,
                        stats=stats,
                    )
                )
            else:
                record.content_hash = content_hash
                record.synced_at = now
                record.stats = stats
            await session.commit()

    # -- reads -------------------------------------------------------------

    async def find_nodes(self, query: str, *, limit: int = 8) -> list[KnowledgeGraphNodeSnapshot]:
        """Resolve an entity by exact name / display_name first, LIKE fallback.

        精确命中排最前（symbol 代码 / 全名），其余按 LIKE 模糊补足到
        ``limit``。空 query 直接 ``ValueError``（可见错误优于全表扫）。
        Retired entities are hidden. Merged losers resolve through
        ``redirect_to_id`` to the surviving active entity.
        """
        text = (query or "").strip()
        if not text:
            raise ValueError(f"query must be a non-empty string, got {query!r}")
        async with self.session_factory() as session:
            exact = await session.execute(
                select(KnowledgeGraphNodeRecord)
                .where(
                    or_(
                        KnowledgeGraphNodeRecord.name == text,
                        KnowledgeGraphNodeRecord.display_name == text,
                    )
                )
                .order_by(KnowledgeGraphNodeRecord.node_type, KnowledgeGraphNodeRecord.name)
                .limit(limit * 3)
            )
            rows = list(exact.scalars().all())
            if len(rows) < limit:
                like = f"%{text}%"
                seen_ids = {r.id for r in rows}
                fuzzy = await session.execute(
                    select(KnowledgeGraphNodeRecord)
                    .where(
                        or_(
                            KnowledgeGraphNodeRecord.name.like(like),
                            KnowledgeGraphNodeRecord.display_name.like(like),
                        )
                    )
                    .order_by(KnowledgeGraphNodeRecord.node_type, KnowledgeGraphNodeRecord.name)
                    .limit(limit * 3)
                )
                for record in fuzzy.scalars().all():
                    if record.id not in seen_ids and len(rows) < limit * 3:
                        rows.append(record)

            resolved: list[KnowledgeGraphNodeRecord] = []
            seen_resolved: set[str] = set()
            for record in rows:
                current = record
                hops = 0
                while (
                    current.status == "merged"
                    and current.redirect_to_id
                    and hops < 8
                ):
                    nxt = await session.get(
                        KnowledgeGraphNodeRecord,
                        current.redirect_to_id,
                    )
                    if nxt is None:
                        break
                    current = nxt
                    hops += 1
                if current.status != "active":
                    continue
                if current.id in seen_resolved:
                    continue
                seen_resolved.add(current.id)
                resolved.append(current)
                if len(resolved) >= limit:
                    break
            return [_kg_node_snapshot(r) for r in resolved]

    async def neighborhood(
        self,
        node_id: str,
        *,
        hops: int = 1,
        include_expired: bool = False,
        edge_limit: int = 200,
    ) -> tuple[list[KnowledgeGraphNodeSnapshot], list[KnowledgeGraphEdgeSnapshot]]:
        """N-hop 邻域子图（迭代扩张，非通用 BFS —— hops 只支持 1..3）。

        返回 ``(nodes, edges)``；``include_expired=True`` 时包含已失效边
        （历史回溯视角）。``edge_limit`` 是硬上限，超限即截断并由调用方
        在渲染层明示截断（不静默）。
        """
        if not isinstance(hops, int) or not 1 <= hops <= 3:
            raise ValueError(f"hops must be an int in 1..3, got {hops!r}")
        async with self.session_factory() as session:
            center = await session.get(KnowledgeGraphNodeRecord, node_id)
            if center is None:
                raise RecordNotFoundError(f"kg node not found: {node_id}")
            hops_left = 0
            while (
                center.status == "merged"
                and center.redirect_to_id
                and hops_left < 8
            ):
                nxt = await session.get(
                    KnowledgeGraphNodeRecord,
                    center.redirect_to_id,
                )
                if nxt is None:
                    break
                center = nxt
                hops_left += 1
            node_id = center.id

            frontier = {node_id}
            visited_nodes = {node_id}
            edges_by_id: dict[str, KnowledgeGraphEdgeRecord] = {}
            for _ in range(hops):
                if not frontier or len(edges_by_id) >= edge_limit:
                    break
                conditions = [
                    or_(
                        KnowledgeGraphEdgeRecord.src_id.in_(sorted(frontier)),
                        KnowledgeGraphEdgeRecord.dst_id.in_(sorted(frontier)),
                    )
                ]
                if not include_expired:
                    conditions.append(KnowledgeGraphEdgeRecord.expired_at.is_(None))
                result = await session.execute(
                    select(KnowledgeGraphEdgeRecord)
                    .where(*conditions)
                    .order_by(KnowledgeGraphEdgeRecord.created_at.desc())
                    .limit(edge_limit)
                )
                next_frontier: set[str] = set()
                for record in result.scalars().all():
                    if len(edges_by_id) >= edge_limit:
                        break
                    edges_by_id.setdefault(record.id, record)
                    for endpoint in (record.src_id, record.dst_id):
                        if endpoint not in visited_nodes:
                            visited_nodes.add(endpoint)
                            next_frontier.add(endpoint)
                frontier = next_frontier

            node_rows: list[KnowledgeGraphNodeRecord] = []
            for chunk in _chunked(sorted(visited_nodes)):
                result = await session.execute(
                    select(KnowledgeGraphNodeRecord).where(
                        KnowledgeGraphNodeRecord.id.in_(chunk)
                    )
                )
                for record in result.scalars().all():
                    if (
                        not include_expired
                        and getattr(record, "status", "active") != "active"
                        and record.id != node_id
                    ):
                        continue
                    node_rows.append(record)
            node_rows.sort(key=lambda r: (r.id != node_id, r.node_type, r.name))
            edge_rows = sorted(
                edges_by_id.values(),
                key=lambda r: (r.expired_at is not None, r.relation, r.created_at),
            )
            return (
                [_kg_node_snapshot(r) for r in node_rows],
                [_kg_edge_snapshot(r) for r in edge_rows],
            )

    async def counts(self) -> dict[str, int]:
        """Graph size snapshot for diagnostics / sync summaries."""
        async with self.session_factory() as session:
            nodes = await session.execute(select(func.count(KnowledgeGraphNodeRecord.id)))
            active = await session.execute(
                select(func.count(KnowledgeGraphEdgeRecord.id)).where(
                    KnowledgeGraphEdgeRecord.expired_at.is_(None)
                )
            )
            expired = await session.execute(
                select(func.count(KnowledgeGraphEdgeRecord.id)).where(
                    KnowledgeGraphEdgeRecord.expired_at.is_not(None)
                )
            )
            return {
                "nodes": int(nodes.scalar_one()),
                "active_edges": int(active.scalar_one()),
                "expired_edges": int(expired.scalar_one()),
            }

    async def list_entry_points(
        self, *, per_type: int = 4, limit: int = 12
    ) -> list[KnowledgeGraphNodeSnapshot]:
        """Sample active nodes for UI empty-state chips.

        Prefer ``role`` / ``cycle`` / ``symbol`` (exploration-friendly),
        ranked by active degree then name for stability. Returns at most
        ``limit`` nodes, taking up to ``per_type`` from each preferred type
        before filling with remaining types.
        """
        if per_type < 1 or limit < 1:
            return []
        preferred = ("role", "cycle", "symbol")
        async with self.session_factory() as session:
            degree_src = (
                select(
                    KnowledgeGraphEdgeRecord.src_id.label("node_id"),
                    func.count().label("deg"),
                )
                .where(KnowledgeGraphEdgeRecord.expired_at.is_(None))
                .group_by(KnowledgeGraphEdgeRecord.src_id)
            )
            degree_dst = (
                select(
                    KnowledgeGraphEdgeRecord.dst_id.label("node_id"),
                    func.count().label("deg"),
                )
                .where(KnowledgeGraphEdgeRecord.expired_at.is_(None))
                .group_by(KnowledgeGraphEdgeRecord.dst_id)
            )
            degree_union = degree_src.union_all(degree_dst).subquery()
            degree = (
                select(
                    degree_union.c.node_id,
                    func.sum(degree_union.c.deg).label("degree"),
                )
                .group_by(degree_union.c.node_id)
                .subquery()
            )
            result = await session.execute(
                select(KnowledgeGraphNodeRecord, degree.c.degree)
                .outerjoin(degree, KnowledgeGraphNodeRecord.id == degree.c.node_id)
                .where(KnowledgeGraphNodeRecord.status == "active")
                .order_by(
                    func.coalesce(degree.c.degree, 0).desc(),
                    KnowledgeGraphNodeRecord.node_type,
                    KnowledgeGraphNodeRecord.name,
                )
                .limit(max(limit * 4, 48))
            )
            rows = list(result.all())

        by_type: dict[str, list[KnowledgeGraphNodeRecord]] = {}
        for record, _deg in rows:
            by_type.setdefault(record.node_type, []).append(record)

        picked: list[KnowledgeGraphNodeRecord] = []
        seen: set[str] = set()
        for node_type in preferred:
            for record in by_type.get(node_type, [])[:per_type]:
                if record.id in seen:
                    continue
                seen.add(record.id)
                picked.append(record)
                if len(picked) >= limit:
                    return [_kg_node_snapshot(r) for r in picked]
        for node_type, records in sorted(by_type.items()):
            if node_type in preferred:
                continue
            for record in records[:per_type]:
                if record.id in seen:
                    continue
                seen.add(record.id)
                picked.append(record)
                if len(picked) >= limit:
                    return [_kg_node_snapshot(r) for r in picked]
        return [_kg_node_snapshot(r) for r in picked]

    # -- decision-signal projection feed -----------------------------------

    async def list_decision_signal_projection_rows(self) -> list[dict[str, Any]]:
        """Read ``decision_signals`` (+outcomes) as plain dicts for projection.

        知识图谱投影只需要一个只读快照；放在本 repo 里避免把
        ``SqlAlchemyDecisionSignalRepository`` 再接进工具装配线。
        """
        async with self.session_factory() as session:
            signals = await session.execute(
                select(DecisionSignalRecord).order_by(DecisionSignalRecord.created_at.asc())
            )
            outcome_rows = await session.execute(
                select(DecisionSignalOutcomeRecord).order_by(
                    DecisionSignalOutcomeRecord.created_at.asc()
                )
            )
            outcomes_by_signal: dict[str, list[dict[str, Any]]] = {}
            for record in outcome_rows.scalars().all():
                outcomes_by_signal.setdefault(record.signal_id, []).append(
                    {
                        "horizon": record.horizon,
                        "outcome": record.outcome,
                        "return_pct": record.return_pct,
                        "anchor_date": record.anchor_date,
                    }
                )
            rows: list[dict[str, Any]] = []
            for record in signals.scalars().all():
                rows.append(
                    {
                        "id": record.id,
                        "symbol": record.symbol,
                        "action": record.action,
                        "source": record.source,
                        "confidence": record.confidence,
                        "horizon": record.horizon,
                        "reason": record.reason,
                        "status": record.status,
                        "created_at": record.created_at,
                        "outcomes": outcomes_by_signal.get(record.id, []),
                    }
                )
            return rows
