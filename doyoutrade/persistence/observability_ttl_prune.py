"""TTL pruning of the heavy observability / trace tables.

Deletes rows older than the retention window from:

* ``debug_session_events``  (filtered on ``timestamp``)
* ``debug_session_spans``   (filtered on ``start_time`` — this table has no ``created_at``)
* ``model_invocations``     (filtered on ``created_at``)
* ``debug_sessions``        (filtered on ``created_at``)

``cycle_runs`` / ``trade_fills`` / ``runs`` are the durable record and are
**never** touched here. By design this means a ``cycle_run`` older than the
retention window survives as a skeleton while its span / event / model-invocation
detail expires — the ``run_id`` chain
(``cycle_runs ↔ debug_sessions ↔ debug_session_spans ↔ model_invocations``)
stays intact *within* the window and intentionally thins out past it.

Every prune is visible per CLAUDE.md §错误可见性:

* a ``retention.prune`` OTel span with per-table ``retention.deleted.<table>``
  attributes + a terminal ``retention.status`` (``ok`` / ``failed``),
* a ``retention_prune_completed`` / ``retention_prune_failed`` debug event
  carrying ``reason`` / ``hint`` and the per-table breakdown,
* a structured ``logger.info`` (success) / ``logger.exception`` (failure) line.

A failed DELETE classifies the failing ``table``, rolls the whole pass back
(single transaction — all-or-nothing per tick), and re-raises so the caller's
loop logs it and the next tick retries. No silent swallow.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy import delete

from doyoutrade.debug import emit_debug_event_sync
from doyoutrade.observability import get_logger, get_tracer
from doyoutrade.persistence.models import (
    DebugSessionEventRecord,
    DebugSessionRecord,
    DebugSessionSpanRecord,
    ModelInvocationRecord,
    _utcnow,
)

logger = get_logger(__name__)
tracer = get_tracer(__name__)

DEFAULT_PRUNE_INTERVAL_HOURS = 24.0

# (model, age-column, table-name). Children are deleted before the parent
# ``debug_sessions`` row, mirroring the app-level cascade order in
# ``delete_task()``. There are no DB-level foreign keys between these tables,
# so the order is for consistency/readability, not referential integrity.
# ``cycle_runs`` / ``trade_fills`` / ``runs`` are intentionally absent — they
# are the durable record and must never be pruned by this policy.
_PRUNE_TARGETS: tuple[tuple[Any, Any, str], ...] = (
    (DebugSessionEventRecord, DebugSessionEventRecord.timestamp, "debug_session_events"),
    (DebugSessionSpanRecord, DebugSessionSpanRecord.start_time, "debug_session_spans"),
    (ModelInvocationRecord, ModelInvocationRecord.created_at, "model_invocations"),
    (DebugSessionRecord, DebugSessionRecord.created_at, "debug_sessions"),
)

# Surfaced in the completion event so operators can see at a glance what the
# policy deliberately keeps.
_PRESERVED_TABLES = ("cycle_runs", "trade_fills", "runs")


async def prune_observability_rows(
    session_factory: Any, *, ttl_days: int
) -> dict[str, int]:
    """Delete observability rows older than ``ttl_days`` days.

    The cutoff is computed against the same naive-UTC clock the models persist
    with (:func:`doyoutrade.persistence.models._utcnow`). All four DELETEs run in
    a single transaction so a tick is all-or-nothing; a failure re-raises after
    emitting the failure span/event/log.

    Returns a ``{table: deleted_count}`` dict.
    """
    if not isinstance(ttl_days, int) or isinstance(ttl_days, bool) or ttl_days <= 0:
        raise ValueError(f"ttl_days must be a positive integer, got {ttl_days!r}")

    cutoff = _utcnow() - timedelta(days=ttl_days)
    counts: dict[str, int] = {}

    with tracer.start_as_current_span("retention.prune") as span:
        span.set_attribute("retention.ttl_days", ttl_days)
        span.set_attribute("retention.cutoff", cutoff.isoformat())
        async with session_factory() as session:
            for model, age_column, table in _PRUNE_TARGETS:
                try:
                    result = await session.execute(
                        delete(model)
                        .where(age_column < cutoff)
                        .execution_options(synchronize_session=False)
                    )
                except Exception as exc:
                    span.set_attribute("retention.status", "failed")
                    span.set_attribute("retention.failed_table", table)
                    logger.exception(
                        "retention prune failed table=%s ttl_days=%s cutoff=%s "
                        "error_type=%s deleted_so_far=%s",
                        table,
                        ttl_days,
                        cutoff.isoformat(),
                        type(exc).__name__,
                        counts,
                    )
                    emit_debug_event_sync(
                        "retention_prune_failed",
                        {
                            "reason": "delete_failed",
                            "table": table,
                            "ttl_days": ttl_days,
                            "cutoff": cutoff.isoformat(),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "deleted_so_far": dict(counts),
                            "hint": (
                                "inspect DB health / locks; the whole pass rolled "
                                "back and the next prune tick retries. "
                                "cycle_runs/trade_fills/runs are never pruned."
                            ),
                        },
                    )
                    raise  # session context manager rolls the transaction back
                deleted = int(result.rowcount or 0)
                counts[table] = deleted
                span.set_attribute(f"retention.deleted.{table}", deleted)
            await session.commit()

        total = sum(counts.values())
        span.set_attribute("retention.deleted_total", total)
        span.set_attribute("retention.status", "ok")
        emit_debug_event_sync(
            "retention_prune_completed",
            {
                "reason": "ttl_exceeded",
                "ttl_days": ttl_days,
                "cutoff": cutoff.isoformat(),
                "deleted_total": total,
                "counts": dict(counts),
                "preserved_tables": list(_PRESERVED_TABLES),
                "hint": (
                    "observability detail older than the retention window was "
                    "removed; cycle_runs survive as the durable trace skeleton."
                ),
            },
        )
        logger.info(
            "retention prune complete ttl_days=%s cutoff=%s deleted_total=%s "
            "debug_session_events=%s debug_session_spans=%s model_invocations=%s "
            "debug_sessions=%s",
            ttl_days,
            cutoff.isoformat(),
            total,
            counts.get("debug_session_events", 0),
            counts.get("debug_session_spans", 0),
            counts.get("model_invocations", 0),
            counts.get("debug_sessions", 0),
        )

    return counts


class ObservabilityPruneService:
    """Recurring background loop that prunes observability rows past the TTL.

    Mirrors :class:`doyoutrade.assistant.job_watcher.JobWatchService`: owns a
    single ``asyncio.Task`` with ``start()`` / ``stop()`` lifecycle hooks wired
    into the API server (``doyoutrade/api/server.py``). The loop sleeps first —
    the bootstrap one-shot covers the boot-time sweep — then prunes. A failed
    prune is already logged + evented by :func:`prune_observability_rows`; the
    loop survives it and retries on the next tick (never silently dies).
    """

    def __init__(
        self,
        *,
        session_factory: Any,
        ttl_days: int = 7,
        interval_hours: float = DEFAULT_PRUNE_INTERVAL_HOURS,
    ) -> None:
        self._session_factory = session_factory
        self._ttl_days = int(ttl_days)
        # Floor at 60s so a misconfigured tiny interval can't busy-loop the DB.
        self._interval_seconds = max(60.0, float(interval_hours) * 3600.0)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._loop(), name="observability-ttl-prune"
            )
            logger.info(
                "ObservabilityPruneService started ttl_days=%s interval_hours=%.2f",
                self._ttl_days,
                self._interval_seconds / 3600.0,
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("ObservabilityPruneService stopped")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await prune_observability_rows(
                    self._session_factory, ttl_days=self._ttl_days
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # prune_observability_rows already emitted the failure span +
                # event with the failing table; the loop must survive to retry.
                logger.exception(
                    "observability ttl prune iteration failed ttl_days=%s",
                    self._ttl_days,
                )
