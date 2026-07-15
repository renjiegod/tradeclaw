"""JobWatchService — wakes assistant sessions when background jobs finish.

The in-process ``watch_job`` tool registers a watch row
(``assistant_job_watches``); this service polls pending watches against the
backtest run repository and, when a job reaches a terminal status, runs the
cron-style compose-and-deliver pipeline:

1. render ``job_completed_framing.j2`` (job id / terminal status / the
   model's own note),
2. run one turn in a fresh worker session (the composer has the full CLI
   surface, so it reads the report itself via ``backtest summary``),
3. deliver the reply into the ORIGINATING session via
   :func:`deliver_assistant_message_to_session` (persisted message +
   channel forward), with ``metadata.source="job_watch"``.

Every outcome is visible: ``assistant.job_watch.fire`` spans,
``job_watch.fired`` / ``job_watch.failed`` events on the originating
session, and ERROR logs on breakage. A watch never silently disappears —
failures resolve the row to ``failed`` with ``last_error``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from doyoutrade.assistant.cron_executors._deliver import (
    deliver_assistant_message_to_session,
)
from doyoutrade.assistant.prompt_templates import render_job_completed_framing
from doyoutrade.observability import get_logger, get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)

# Run.status values that end a backtest job (see
# ``doyoutrade/platform/service.py`` finalize_success / finalize_failed /
# finalize_stopped). ``paused`` is NOT terminal.
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "stopped"})

DEFAULT_POLL_INTERVAL_SECONDS = 5.0


class JobWatchService:
    """Polling loop resolving pending job watches into session wake-ups."""

    def __init__(
        self,
        *,
        watch_repository: Any,
        run_repository: Any,
        assistant_service: Any,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._watch_repo = watch_repository
        self._run_repo = run_repository
        self._svc = assistant_service
        self._interval = max(0.5, float(poll_interval_seconds))
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="assistant-job-watcher")
            logger.info("JobWatchService started interval=%.1fs", self._interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("JobWatchService stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The loop must survive a bad poll; the failure itself is
                # fully logged here and the next tick retries.
                logger.exception("job_watch poll iteration failed")
            await asyncio.sleep(self._interval)

    async def poll_once(self) -> int:
        """Resolve every pending watch whose job reached a terminal status.

        Returns the number of watches resolved this pass (fired + failed) —
        primarily for tests and diagnostics.
        """
        watches = await self._watch_repo.list_pending(limit=100)
        resolved = 0
        for watch in watches:
            job_id = str(watch.get("job_id") or "")
            try:
                run = await self._run_repo.get(job_id)
            except Exception as exc:
                logger.error(
                    "job_watch run lookup failed watch_id=%s job_id=%s err=%s: %s",
                    watch.get("watch_id"),
                    job_id,
                    type(exc).__name__,
                    exc,
                )
                continue  # transient read failure: keep pending, retry next tick
            if run is None:
                await self._resolve_failed(
                    watch,
                    error="job_not_found",
                    hint="the watched run id no longer resolves; it may have been deleted",
                )
                resolved += 1
                continue
            status = str(run.get("status") or "")
            if status not in TERMINAL_JOB_STATUSES:
                continue
            await self._fire(watch, run)
            resolved += 1
        return resolved

    async def _fire(self, watch: dict[str, Any], run: dict[str, Any]) -> None:
        watch_id = str(watch.get("watch_id") or "")
        job_id = str(watch.get("job_id") or "")
        session_id = str(watch.get("session_id") or "")
        job_status = str(run.get("status") or "")
        with tracer.start_as_current_span("assistant.job_watch.fire") as span:
            span.set_attribute("assistant.job_watch.watch_id", watch_id)
            span.set_attribute("assistant.job_watch.job_id", job_id)
            span.set_attribute("assistant.job_watch.job_status", job_status)
            span.set_attribute("assistant.session_id", session_id)
            try:
                framing = render_job_completed_framing(
                    job_id=job_id,
                    job_kind=str(watch.get("job_kind") or "backtest"),
                    job_status=job_status,
                    origin_session_id=session_id,
                    watch_created_at=str(watch.get("created_at") or ""),
                    note=watch.get("note"),
                )
                worker_session = await self._svc.create_session(
                    agent_id=str(watch.get("agent_id") or ""),
                    title=f"[JobWatch] {job_id}",
                )
                result = await self._svc.send_message(
                    session_id=worker_session["session_id"],
                    content=framing,
                )
                reply_text = ""
                messages = result.get("messages") if isinstance(result, dict) else None
                if isinstance(messages, list) and messages:
                    last = messages[-1]
                    if isinstance(last, dict):
                        reply_text = str(last.get("content") or "").strip()
                if not reply_text:
                    raise RuntimeError("composer produced an empty reply")

                # ``cron_job_id`` / ``cron_job_run_id`` are the delivery
                # helper's correlation slots (named for its original cron
                # caller); we ride them with watch/job ids so the
                # cron.delivery span still pins this push to the watch.
                delivery_status, delivery_info = await deliver_assistant_message_to_session(
                    self._svc,
                    target_session_id=session_id,
                    content=reply_text,
                    cron_job_id=watch_id,
                    cron_job_run_id=job_id,
                    cron_task_kind="job_watch",
                    source="job_watch",
                    extra_metadata={
                        "job_watch_id": watch_id,
                        "job_id": job_id,
                        "job_status": job_status,
                    },
                )
                span.set_attribute("assistant.job_watch.delivery_status", delivery_status)
                if delivery_status == "failed":
                    error_text = ""
                    if isinstance(delivery_info, dict):
                        error_text = str(delivery_info.get("error") or "")
                    raise RuntimeError(f"delivery failed: {error_text}")
            except Exception as exc:
                span.set_attribute("assistant.job_watch.status", "failed")
                await self._resolve_failed(
                    watch,
                    error=f"{type(exc).__name__}: {exc}",
                    hint="composer turn or delivery broke; inspect the worker session trace",
                )
                return

            span.set_attribute("assistant.job_watch.status", "fired")
            await self._watch_repo.resolve(watch_id, status="fired")
            await self._append_event(
                session_id,
                event_type="job_watch.fired",
                payload={
                    "watch_id": watch_id,
                    "job_id": job_id,
                    "job_status": job_status,
                    "worker_session_id": worker_session["session_id"],
                },
            )
            logger.info(
                "job_watch fired watch_id=%s job_id=%s status=%s session_id=%s",
                watch_id,
                job_id,
                job_status,
                session_id,
            )

    async def _resolve_failed(
        self, watch: dict[str, Any], *, error: str, hint: str
    ) -> None:
        watch_id = str(watch.get("watch_id") or "")
        session_id = str(watch.get("session_id") or "")
        logger.error(
            "job_watch failed watch_id=%s job_id=%s session_id=%s error=%s hint=%s",
            watch_id,
            watch.get("job_id"),
            session_id,
            error,
            hint,
        )
        try:
            await self._watch_repo.resolve(watch_id, status="failed", last_error=error)
        except Exception as exc:
            logger.error(
                "job_watch failure-resolution write failed watch_id=%s err=%s: %s",
                watch_id,
                type(exc).__name__,
                exc,
            )
        await self._append_event(
            session_id,
            event_type="job_watch.failed",
            payload={
                "watch_id": watch_id,
                "job_id": watch.get("job_id"),
                "error": error,
                "hint": hint,
            },
        )

    async def _append_event(
        self, session_id: str, *, event_type: str, payload: dict[str, Any]
    ) -> None:
        try:
            await self._svc.repository.append_event(
                session_id=session_id,
                event_type=event_type,
                payload=payload,
            )
        except Exception as exc:
            logger.error(
                "job_watch event append failed session_id=%s event=%s err=%s: %s",
                session_id,
                event_type,
                type(exc).__name__,
                exc,
            )
