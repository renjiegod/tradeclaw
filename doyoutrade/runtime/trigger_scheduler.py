"""Background daemon that fires due Task Triggers (cycle_runs.run_kind='trigger').

Phase 3: this is the SOLE scheduler — ``RuntimeTickLoop`` and ``tasks.tick_mode`` are
retired. A Task does nothing until it owns a Trigger: continuous trading is an
``interval``/``trade`` trigger; a scheduled signal-push is a ``cron``/``signal_only``
trigger. Each scan also expires stale pending approvals (the duty the tick loop used
to own). Migrated/imported triggers with a NULL ``next_fire_at`` self-initialize on the
first scan (see ``_maybe_fire``).

Due-ness reuses the validated APScheduler cron math via ``runtime.cron_timing`` — never
a hand-rolled minute-match (that is the TZ-drift bug class, see MEMORY
project_cron_pending_followups). Each fire produces exactly one ``cycle_runs`` row via
``service.run_trigger`` → the same ``run_cycle`` path, tagged ``trigger_id`` +
``run_kind='trigger'``.

Visibility (CLAUDE.md §错误可见性): every scan opens a ``trigger.scheduler.scan`` span;
every fire a ``trigger.fire`` span (attrs run_id/task_id/trigger_id/schedule_kind/
execution_intent/status). Meaningful skips (overlap, outside trading session) log at INFO
with a structured reason; fire failures log at ERROR with the exception type + trigger id
and set ``trigger.status='error'`` + ``last_error`` WITHOUT killing sibling triggers.
Not-yet-due / parent-not-running are quiet (not failures; would be per-second noise).

``backtest_range`` triggers are excluded from the poll (repo.list_schedulable filters
them) — backtests are launched on demand via the bar loop, never wall-clock polled.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone

from doyoutrade.debug import emit_debug_event
from doyoutrade.observability import get_logger, get_tracer
from doyoutrade.runtime.trigger_delivery import (
    deliver_approval_result_card,
    deliver_pending_approval_cards,
    deliver_trigger_result,
)
from doyoutrade.runtime.triggers import compute_next_fire, is_due


logger = get_logger(__name__)
tracer = get_tracer(__name__)

# Resume-dispatch retry budget. After this many consecutive failures an approved
# order is abandoned (terminal dispatched_at stamp) so a permanently-failing
# order cannot loop forever (CLAUDE.md §错误可见性 — visible, bounded).
_MAX_RESUME_ATTEMPTS = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TriggerScheduler:
    """Polls ``task_triggers`` and fires due ones through ``service.run_trigger``."""

    def __init__(
        self,
        service,
        trigger_repository,
        *,
        interval_seconds: float = 1.0,
        assistant_service=None,
        cycle_run_repository=None,
        approval_gate=None,
    ):
        self.service = service
        self.trigger_repository = trigger_repository
        self.interval_seconds = max(0.1, float(interval_seconds))
        # Delivery (Phase 2) is a post-cycle, best-effort hook. When unset (e.g. the
        # e2e bootstrap path), fires still run + persist; only the push is skipped.
        self.assistant_service = assistant_service
        self.cycle_run_repository = cycle_run_repository
        # Sole-driver duty (Phase 3): expire stale pending approvals every scan,
        # the cadence the retired RuntimeTickLoop used to provide. None = no gate
        # wired (e.g. the e2e bootstrap path).
        self.approval_gate = approval_gate
        self._task: asyncio.Task | None = None

    async def _maybe_deliver(self, trg, run_id: str | None) -> None:
        """Best-effort post-cycle push per trg.delivery_json. Never fails the fire."""
        if run_id is None:
            return
        try:
            await deliver_trigger_result(
                self.assistant_service,
                trigger=trg,
                run_id=run_id,
                cycle_run_repository=self.cycle_run_repository,
                instrument_catalog_repository=getattr(
                    self.service, "instrument_catalog_repository", None
                ),
                task_repository=getattr(self.service, "task_repository", None),
            )
        except Exception:
            logger.exception(
                "trigger delivery raised (fire already persisted) trigger_id=%s run_id=%s",
                trg.id, run_id,
            )
        # Separately push a Feishu approval card for any order this fire held for
        # human approval. Independent of the digest push (a pending order must be
        # notified even when digest mode is none) and equally best-effort.
        try:
            await deliver_pending_approval_cards(
                self.assistant_service,
                trigger=trg,
                run_id=run_id,
                approval_gate=self.approval_gate,
                cycle_run_repository=self.cycle_run_repository,
                instrument_catalog_repository=getattr(
                    self.service, "instrument_catalog_repository", None
                ),
            )
        except Exception:
            logger.exception(
                "approval card delivery raised (fire already persisted) trigger_id=%s run_id=%s",
                trg.id, run_id,
            )

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="doyoutrade-trigger-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                fired = await self.scan_once()
                if fired:
                    logger.info("trigger scheduler scan fired=%s", fired)
                await self._expire_pending_approvals()
                await self._resume_approved_intents()
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive loop logging
                logger.exception("trigger scheduler scan failed")
                await asyncio.sleep(self.interval_seconds)

    async def _expire_pending_approvals(self) -> None:
        """Sweep stale pending approvals each scan (replaces the RuntimeTickLoop hook).

        Best-effort: never let an approval-gate failure kill the scheduler loop.
        """
        gate = self.approval_gate
        if gate is None or not hasattr(gate, "expire_pending"):
            return
        try:
            expired = await gate.expire_pending()
        except Exception:
            logger.exception("trigger scheduler: approval_gate.expire_pending failed")
            return
        if expired:
            logger.info("trigger scheduler expired_pending_approvals=%s", len(expired))

    async def _resume_approved_intents(self) -> None:
        """Dispatch approved-but-not-yet-sent orders to their task's worker.

        The symmetric counterpart to ``_expire_pending_approvals``: a human
        approved an order via the card / web after the originating cycle ended,
        so the order now needs to actually reach the broker. Each approved order
        is re-dispatched through the task's live worker (same submit path as an
        in-cycle order → mock/qmt parity). Best-effort: an approval-layer or a
        single order's failure never kills the scheduler loop.
        """
        gate = self.approval_gate
        if gate is None or not hasattr(gate, "list_resumable"):
            return
        try:
            resumable = await gate.list_resumable()
        except Exception:
            logger.exception("trigger scheduler: approval_gate.list_resumable failed")
            return
        if not resumable:
            return
        with tracer.start_as_current_span("approval.resume") as span:
            span.set_attribute("approval.resumable", len(resumable))
            resumed = 0
            for approval in resumable:
                if await self._resume_one(approval) == "dispatched":
                    resumed += 1
            span.set_attribute("approval.resumed", resumed)
        if resumed:
            logger.info("trigger scheduler resumed_approved_intents=%s", resumed)

    async def _resume_one(self, approval) -> str:
        approval_id = getattr(approval, "approval_id", None)
        try:
            result = await self.service.dispatch_resumed_approval(approval)
        except Exception as exc:
            await self._record_resume_failure(approval, f"{type(exc).__name__}: {exc}")
            logger.exception(
                "trigger scheduler: dispatch_resumed_approval raised approval_id=%s",
                approval_id,
            )
            return "failed"

        status = result.get("status")
        if status == "dispatched":
            try:
                await self.approval_gate.mark_dispatched(approval_id)
            except Exception:
                logger.exception(
                    "trigger scheduler: mark_dispatched failed approval_id=%s", approval_id
                )
            fill = result.get("fill") or {}
            await emit_debug_event(
                "approval_intent_resumed",
                {
                    "approval_id": approval_id,
                    "intent_id": getattr(approval, "intent_id", None),
                    "run_id": result.get("run_id"),
                    "task_id": getattr(approval, "task_id", None),
                    "symbol": fill.get("symbol") or getattr(approval, "symbol", None),
                    "quantity": fill.get("quantity"),
                    "price": fill.get("price"),
                    "hint": "approved order dispatched to the adapter via the task worker",
                },
            )
            logger.info(
                "approval intent resumed approval_id=%s intent_id=%s run_id=%s symbol=%s",
                approval_id,
                getattr(approval, "intent_id", None),
                result.get("run_id"),
                fill.get("symbol") or getattr(approval, "symbol", None),
            )
            # Receipt: the approve→fill is async, so the operator who saw 已批准 now
            # gets an explicit 已成交 card with the ACTUAL fill (成交价/数量/金额/时间).
            await self._push_resume_result_card(approval, outcome="filled", fill=fill)
            return "dispatched"

        if status == "skipped":
            reason = result.get("reason") or "unknown"
            await emit_debug_event(
                "approval_resume_skipped",
                {
                    "approval_id": approval_id,
                    "intent_id": getattr(approval, "intent_id", None),
                    "task_id": getattr(approval, "task_id", None),
                    "reason": reason,
                    "hint": (
                        "approved order not dispatched this sweep — the task is not "
                        "running or a cycle is in flight. It stays resumable and will "
                        "be retried on the next sweep once the task is running."
                    ),
                },
            )
            logger.info(
                "approval resume skipped approval_id=%s reason=%s", approval_id, reason
            )
            return "skipped"

        # invalid / failed → bump attempts, abandon after the budget.
        reason = result.get("reason") or "unknown"
        error = result.get("error") or reason
        await self._record_resume_failure(approval, error)
        return "failed"

    async def _record_resume_failure(self, approval, error: str) -> None:
        approval_id = getattr(approval, "approval_id", None)
        attempts = int(getattr(approval, "dispatch_attempts", 0) or 0) + 1
        abandon = attempts >= _MAX_RESUME_ATTEMPTS
        try:
            await self.approval_gate.mark_dispatch_failed(approval_id, error, abandon=abandon)
        except Exception:
            logger.exception(
                "trigger scheduler: mark_dispatch_failed failed approval_id=%s", approval_id
            )
        event = "approval_resume_abandoned" if abandon else "approval_resume_failed"
        await emit_debug_event(
            event,
            {
                "approval_id": approval_id,
                "intent_id": getattr(approval, "intent_id", None),
                "task_id": getattr(approval, "task_id", None),
                "error": error,
                "attempts": attempts,
                "abandoned": abandon,
                "hint": (
                    "resume dispatch failed; "
                    + (
                        "retry budget exhausted — order abandoned (terminal). "
                        "Inspect the broker / worker error and re-create the order if needed."
                        if abandon
                        else "will retry on a later sweep."
                    )
                ),
            },
        )
        logger.warning(
            "approval resume %s approval_id=%s attempts=%s error=%s",
            "abandoned" if abandon else "failed",
            approval_id,
            attempts,
            error,
        )
        # Only notify on the TERMINAL outcome: a will-retry failure is transient
        # (next sweep may fill), so pushing then would spam. Abandon is final → the
        # operator must learn the approved order did NOT go through.
        if abandon:
            await self._push_resume_result_card(approval, outcome="abandoned", error=error)

    async def _push_resume_result_card(
        self, approval, *, outcome: str, fill: dict | None = None, error: str = ""
    ) -> None:
        """Best-effort post-dispatch order-result card + visibility event (§错误可见性).

        Tells the operator whether the approved order actually filled. A push
        failure never affects the dispatch (already recorded); the delivery outcome
        is emitted as a debug event so a silent notification gap stays visible (e.g.
        ``delivery_status=no_channel_target`` → web-only, not a hidden failure).
        """
        if self.assistant_service is None:
            return
        try:
            status = await deliver_approval_result_card(
                self.assistant_service,
                approval=approval,
                outcome=outcome,
                fill=fill,
                error=error,
                trigger_repository=self.trigger_repository,
                cycle_run_repository=self.cycle_run_repository,
                instrument_catalog_repository=getattr(
                    self.service, "instrument_catalog_repository", None
                ),
            )
        except Exception:
            logger.exception(
                "approval result card push raised approval_id=%s outcome=%s",
                getattr(approval, "approval_id", None), outcome,
            )
            status = "raised"
        await emit_debug_event(
            "approval_result_notified",
            {
                "approval_id": getattr(approval, "approval_id", None),
                "intent_id": getattr(approval, "intent_id", None),
                "task_id": getattr(approval, "task_id", None),
                "run_id": getattr(approval, "run_id", None),
                "outcome": outcome,
                "delivery_status": status,
                "hint": (
                    "post-dispatch order-result card; delivery_status=no_channel_target "
                    "means the task has no Feishu channel trigger so the outcome is "
                    "web-only (Approvals page)."
                ),
            },
        )

    async def scan_once(self) -> int:
        """One scan pass. Returns the number of triggers fired. Testable in isolation."""
        with tracer.start_as_current_span("trigger.scheduler.scan") as scan_span:
            now = _utcnow()
            try:
                triggers = await self.trigger_repository.list_schedulable()
            except Exception:
                logger.exception("trigger scheduler: list_schedulable failed")
                raise
            scan_span.set_attribute("trigger.candidates", len(triggers))
            fired = 0
            for trg in triggers:
                if await self._maybe_fire(trg, now=now):
                    fired += 1
            scan_span.set_attribute("trigger.fired", fired)
            return fired

    async def _maybe_fire(self, trg, *, now: datetime) -> bool:
        instance = self.service.scheduler.tasks.get(trg.task_id)
        if instance is None or getattr(instance, "status", None) != "running":
            # Quiet: most triggers belong to non-running tasks; not a failure.
            return False

        # Lazy-init: migrated / imported triggers land with next_fire_at NULL.
        # Compute + persist the first fire time here so they self-initialize, then
        # wait for the next scan to actually fire (a NULL next_fire_at is never due).
        if trg.next_fire_at is None:
            computed = self._next_fire_after(trg, now=now)
            await self.trigger_repository.update_trigger(trg.id, next_fire_at=computed)
            return False

        if not is_due(trg.next_fire_at, now=now):
            return False

        # Due. In-flight overlap guard: a long cycle must not be re-fired by the next
        # poll. Do NOT advance next_fire_at — retry on the next free poll.
        if self.service._cycle_lock(trg.task_id).locked():
            logger.info(
                "trigger skipped reason=trigger_overlap_skipped trigger_id=%s task_id=%s",
                trg.id,
                trg.task_id,
            )
            return False

        with tracer.start_as_current_span("trigger.fire") as fire_span:
            fire_span.set_attribute("trigger.id", trg.id)
            fire_span.set_attribute("task_id", trg.task_id)
            fire_span.set_attribute("trigger.schedule_kind", trg.schedule_kind)
            fire_span.set_attribute("trigger.execution_intent", trg.execution_intent)
            try:
                run_id = await self.service.run_trigger(trg)
            except Exception as exc:
                # Fire failure: ERROR + structured fields + isolate (siblings survive).
                fired_at = _utcnow()
                next_fire = self._next_fire_after(trg, now=fired_at)
                logger.error(
                    "trigger fire failed trigger_id=%s task_id=%s error_type=%s error=%s",
                    trg.id,
                    trg.task_id,
                    type(exc).__name__,
                    exc,
                )
                fire_span.set_attribute("trigger.status", "error")
                fire_span.set_attribute("trigger.error_type", type(exc).__name__)
                with contextlib.suppress(Exception):
                    await self.trigger_repository.record_fire(
                        trg.id,
                        last_fired_at=fired_at,
                        next_fire_at=next_fire,
                        last_run_id=None,
                        status="error",
                        last_error=f"{type(exc).__name__}: {exc}",
                    )
                return False

            fired_at = _utcnow()
            if run_id is None:
                # Parent stopped between gate and fire, or kill switch flipped — not a
                # fire. Recompute next so we re-evaluate later.
                fire_span.set_attribute("trigger.status", "no_cycle")
                await self.trigger_repository.record_fire(
                    trg.id,
                    last_fired_at=trg.last_fired_at or fired_at,
                    next_fire_at=self._next_fire_after(trg, now=fired_at),
                    last_run_id=None,
                    status=None,
                    last_error="",
                )
                return False

            # One-shot ('at') exhausts after a successful fire; recurring recomputes.
            if trg.schedule_kind == "at":
                fire_span.set_attribute("trigger.status", "exhausted")
                fire_span.set_attribute("run_id", run_id)
                await self.trigger_repository.record_fire(
                    trg.id,
                    last_fired_at=fired_at,
                    next_fire_at=None,
                    last_run_id=run_id,
                    status="exhausted",
                    last_error="",
                )
                await self._maybe_deliver(trg, run_id)
                if trg.delete_after_run:
                    with contextlib.suppress(Exception):
                        await self.trigger_repository.delete_trigger(trg.id)
                return True

            next_fire = self._next_fire_after(trg, now=fired_at)
            fire_span.set_attribute("trigger.status", "fired")
            fire_span.set_attribute("run_id", run_id)
            await self.trigger_repository.record_fire(
                trg.id,
                last_fired_at=fired_at,
                next_fire_at=next_fire,
                last_run_id=run_id,
                status=None,
                last_error="",
            )
            await self._maybe_deliver(trg, run_id)
            return True

    @staticmethod
    def _next_fire_after(trg, *, now: datetime) -> datetime | None:
        return compute_next_fire(
            schedule_kind=trg.schedule_kind,
            interval_seconds=trg.interval_seconds,
            cron_expression=trg.cron_expression,
            timezone_str=trg.timezone,
            at_iso=trg.at_iso,
            last_fired_at=now,
            now=now,
        )
