from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.cron.expressions import AllExpression
from apscheduler.triggers.date import DateTrigger
from jinja2 import Template

# Reject cron jobs whose next fire is more than this far out unless the
# caller explicitly opts in. Historical LLM mistakes that this catches:
#   * 字段顺序错  e.g. ``49 35 23 5 *`` — APScheduler rejects (handled by
#     ``_validate_cron_expression``), so this guard is a backstop.
#   * 时间值错位  e.g. ``28 23 23 5 *`` for "30秒后" — syntactically valid
#     but next fire is next year May 23 at 23:28 UTC.
#   * 时区漂移   e.g. minute/hour calculated in Asia/Shanghai but stored
#     ``timezone="UTC"`` — next fire ends up 8+ hours off. The
#     30-day threshold is too lax to catch this; the tighter
#     ``_CALENDAR_PIN_DISTANT_THRESHOLD`` below catches it for the
#     one-shot pattern that LLMs typically use for "fire in N
#     seconds".
_DISTANT_SCHEDULE_THRESHOLD = timedelta(days=30)

# Tighter threshold for "calendar pin" patterns (day AND month both
# specific). These fire once per year, so the LLM's typical use is
# "fire in seconds/minutes from now". Anything more than 2h away is
# overwhelmingly a timezone-drift bug (the dominant LLM failure mode
# — caller computed HH:MM from local wall clock but stored UTC).
_CALENDAR_PIN_DISTANT_THRESHOLD = timedelta(hours=2)

# Window for matching delta against the local↔configured TZ offset
# when classifying the failure as "timezone drift" rather than
# "field-order mistake". A 5-minute slack covers the seconds between
# the LLM's clock read and APScheduler's next-fire calculation.
_TIMEZONE_DRIFT_MATCH_WINDOW = timedelta(minutes=5)

# After a calendar-pin one-shot fires, the next match is by definition
# ~1 year out (same calendar instant next year). If the next fire is
# at least this far away, treat the job as "done": auto-disable to
# stop the year-later zombie fire (the LLM/user intent was a one-shot,
# not an annual reminder). 180 days catches every legitimate pin
# (max gap = 365 days) while excluding any short-period recurring
# pattern that somehow trips the pin detector.
_ONE_SHOT_AUTO_DISABLE_THRESHOLD = timedelta(days=180)

from doyoutrade.assistant.cron_executors import (
    JobExecutorRegistry,
    JobRunContext,
    JobTaskRegistry,
    PreActionResult,
    TaskResult,
)
from doyoutrade.assistant.trading_sessions import ashare_continuous_trading_skip_reason
from doyoutrade.observability import get_logger, get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)


def _format_trace_id(span: Any) -> str | None:
    """Return the span's 32-char lowercase hex trace_id, or None if untraced.

    The no-op tracer (tests / tracing disabled) reports an all-zero
    ``INVALID_TRACE_ID``; we map that to None so a NULL column always means
    "this fire was not traced" rather than a fake all-zeros trace.
    """
    try:
        ctx = span.get_span_context()
        trace_id_int = getattr(ctx, "trace_id", 0)
    except Exception:
        return None
    if not trace_id_int:
        return None
    return format(trace_id_int, "032x")


_DELETE_HINT_PATTERN = re.compile(
    r"(?:doyoutrade[- ]cli\s+cron\s+delete|cron\s+delete|"
    r"please\s+delete|删除\s*(?:这个|本)?(?:任务|job|cron)|"
    r"把(?:自己|这|本)?(?:任务|job|cron)?\s*删)",
    re.IGNORECASE,
)


def _build_cron_trigger_header(
    job: dict[str, Any], fired_at: datetime, body: str = "",
) -> str:
    """One-line, machine-parseable header prepended to every cron
    user message so the receiving agent immediately knows the
    session was cron-triggered (not a human typing).

    Without this header, the agent treats the rendered template as
    an opaque user greeting and spends a round-trip running
    ``cron list`` to recover context (observed at session
    asst-91bfd63bf186, where the agent ran ``cron list`` just to
    figure out which job's "你好" it was answering). The
    ``[cron-trigger]`` delimiter is intentionally unmistakable —
    bracket prefix, leading colon, identical to nothing a user
    would naturally type.

    Also carries an explicit anti-recursion instruction. The API
    enforces this hard (see ``X-DOYOUTRADE-Calling-Session-Id``
    header check in ``create_agent_cron_job``), but the prompt-level
    reminder keeps the agent from wasting tokens trying.

    When ``delete_after_run=true`` AND the rendered template body
    contains a "delete this job" instruction (extremely common —
    the creating LLM bakes self-cleanup into the template before
    realizing the server handles it), we surface a sharper
    "IGNORE the delete instructions in the body" line. This
    override is what stops the trigger agent from blindly running
    ``cron delete`` against a job the server is about to clean up
    on its own (observed across sessions asst-3efe1be9e4ff and
    asst-fdc950e477f0).
    """
    if job.get("delete_after_run"):
        delete_hint = (
            " This job has delete_after_run=true; the system will "
            "delete it automatically, you do NOT need to call "
            "`doyoutrade-cli cron delete`."
        )
        if body and _DELETE_HINT_PATTERN.search(body):
            delete_hint += (
                " ⚠️ The rendered template body below contains "
                "stale 'please delete me' text that the creating "
                "session baked in before the server's auto-cleanup "
                "kicked in — IGNORE those instructions. Do NOT run "
                "`doyoutrade-cli cron delete`."
            )
    else:
        delete_hint = ""
    return (
        f"[cron-trigger] job_id={job.get('id')} "
        f"name={job.get('name')!r} fired_at={fired_at.isoformat()} "
        f"— the lines below are the rendered input_template; respond "
        f"to those, not to this header. CRITICAL: this session is "
        f"cron-fired; do NOT create new cron jobs from here unless "
        f"the user EXPLICITLY asks for one (recursive cron creation "
        f"is blocked at the API anyway).{delete_hint}"
    )


class AgentCronManager:
    """
    Manages cron jobs for Assistant Agents using APScheduler.

    Each job fires on its cron schedule, creates a fresh Assistant Session,
    renders the input_template with Jinja2 (including {{now}} variable),
    sends it via the AssistantService, and records the result.
    """

    def __init__(
        self,
        assistant_service: Any,  # AssistantService — avoid circular import
        cron_repo: Any,          # SqlAlchemyCronJobRepository
        *,
        cron_run_repo: Any = None,  # SqlAlchemyCronJobRunRepository | None
        executor_registry: JobExecutorRegistry | None = None,
        task_registry: JobTaskRegistry | None = None,
        timezone: str = "UTC",
    ):
        self._svc = assistant_service
        self._repo = cron_repo
        self._run_repo = cron_run_repo
        # Legacy pre_action registry — used only when a job row lacks a
        # ``task_kind`` (rows persisted before the Task-3 pipeline landed).
        self._registry = executor_registry or JobExecutorRegistry()
        # New task-dispatch registry — each ``task.kind`` owns its own
        # full pipeline (gather → optionally invoke agent → deliver).
        # Exposed as a public attribute so the assistant tools can validate
        # ``task.params`` at write-time against the same executors that will
        # later consume them at fire time.
        self.task_registry: JobTaskRegistry = task_registry or JobTaskRegistry()
        self._scheduler = AsyncIOScheduler(timezone=timezone)
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._running = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load all enabled jobs from DB and register them with APScheduler.

        Historical bad rows (e.g. a malformed cron expression persisted by
        an older version of the assistant tool that let the model
        hand-write 5-field strings) must not take down the whole API. A
        single bad row is logged, recorded as ``last_status='error'`` on
        its DB row, and skipped — the scheduler still comes up.
        """
        if self._running:
            return
        self._running = True
        # Fetch all jobs (pass empty string to repo to get all) and filter enabled
        all_jobs = await self._repo.list_jobs("")
        skipped = 0
        for job in all_jobs:
            if not job.get("enabled"):
                continue
            try:
                await self._register(job)
            except (ValueError, LookupError, TypeError) as exc:
                skipped += 1
                job_id = job.get("id", "?")
                expr = job.get("cron_expression", "")
                tz = job.get("timezone", "")
                logger.error(
                    "Skipping cron job at startup due to invalid trigger: "
                    "id=%s expr=%r tz=%s reason=%s",
                    job_id, expr, tz, exc,
                )
                # Drop any partial registration state so a later
                # update_job/resume_job for this id starts clean.
                self._sems.pop(job_id, None)
                try:
                    await self._repo.update_job_state(
                        job_id,
                        last_status="error",
                        last_error=f"register_failed: {exc}",
                    )
                except Exception:
                    # Best-effort: don't let a failed status write also
                    # kill server boot.
                    logger.exception(
                        "Failed to mark cron job %s as register_failed", job_id,
                    )
        self._scheduler.start()
        logger.info(
            "AgentCronManager started (registered=%d skipped=%d)",
            len([j for j in all_jobs if j.get("enabled")]) - skipped,
            skipped,
        )

    async def stop(self) -> None:
        """Shutdown APScheduler gracefully."""
        if not self._running:
            return
        self._running = False
        self._scheduler.shutdown(wait=True)
        logger.info("AgentCronManager stopped")

    # ── Job Registration ────────────────────────────────────────────────────

    async def _register(self, job: dict[str, Any]) -> None:
        """Register (or re-register) a single job with APScheduler.

        Dispatches on ``schedule_kind``: ``cron`` rows build a
        ``CronTrigger`` from the 5-field expression, ``at`` rows build
        a ``DateTrigger`` from the stored ISO instant. Legacy rows
        (no ``schedule_kind``) default to cron for back-compat.
        """
        job_id = job["id"]
        self._sems[job_id] = asyncio.Semaphore(job["max_concurrency"])
        kind = (job.get("schedule_kind") or "cron")
        if kind == "at":
            at_iso = job.get("at_iso")
            if not at_iso:
                raise ValueError(
                    f"cron_job {job_id} has schedule_kind='at' but "
                    "at_iso is empty"
                )
            run_date = datetime.fromisoformat(at_iso)
            trigger: Any = DateTrigger(run_date=run_date)
            register_summary = f"at={at_iso}"
        else:
            cron_expr, weekday_rewrite = self._rewrite_unix_weekday_dow(
                job["cron_expression"],
            )
            if weekday_rewrite:
                logger.warning(
                    "Cron job id=%s cron_expression day-of-week uses "
                    "Unix-style numbering (%r); registering as %r "
                    "(Mon–Fri in APScheduler). Persist via cron update.",
                    job_id,
                    job["cron_expression"].strip().split()[-1],
                    cron_expr.strip().split()[-1],
                )
            trigger = CronTrigger.from_crontab(
                cron_expr,
                timezone=job["timezone"],
            )
            register_summary = (
                f"expr={cron_expr} tz={job['timezone']}"
            )
        self._scheduler.add_job(
            self._execute,
            trigger=trigger,
            args=[job_id],
            id=job_id,
            misfire_grace_time=60,
        )
        logger.info(
            "Registered cron job id=%s kind=%s %s",
            job_id, kind, register_summary,
        )

    async def _deregister(self, job_id: str) -> None:
        """Remove a job from APScheduler.

        Raises :class:`apscheduler.jobstores.base.JobLookupError` when the
        job is not currently registered (caller decides whether that is
        an error or expected idempotency). Callers that intentionally
        swallow the lookup-error case must log the swallow.
        """
        self._scheduler.remove_job(job_id)
        self._sems.pop(job_id, None)

    async def _deregister_best_effort(self, job_id: str, *, op: str) -> None:
        """Idempotent deregister that distinguishes ``not-registered`` (info)
        from other APScheduler errors (warning with traceback). The previous
        ``except Exception: pass`` masked both cases, so a real APScheduler
        bug (broken jobstore, timezone misconfig) used to go silent."""

        try:
            await self._deregister(job_id)
        except Exception as exc:
            # Lookup miss is normal when the caller doesn't know whether the
            # job is currently registered (paused/already-deleted). Anything
            # else is a real failure operators should see.
            exc_name = type(exc).__name__
            if exc_name == "JobLookupError":
                logger.info(
                    "Cron deregister no-op (job already absent) op=%s job_id=%s",
                    op, job_id,
                )
                self._sems.pop(job_id, None)
                return
            logger.error(
                "Cron deregister failed op=%s job_id=%s exc=%s: %s "
                "(continuing; downstream state change still applied)",
                op, job_id, exc_name, exc,
            )
            # Still clear the local semaphore so a subsequent register starts
            # from a clean slot — same invariant the old ``except: pass`` was
            # implicitly maintaining.
            self._sems.pop(job_id, None)

    # ── CRUD Operations ─────────────────────────────────────────────────────

    async def _ensure_agent_exists(self, agent_id: Any) -> None:
        """Pre-flight check so callers get a clear validation error instead of
        a database-level ``ForeignKeyViolationError`` from ``cron_jobs.agent_id``.
        """
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError("agent_id is required")
        agent_repo = getattr(self._svc, "agent_repo", None)
        if agent_repo is None:
            return
        agent = await agent_repo.get_agent(agent_id)
        if not agent:
            raise ValueError(f"Agent not found: {agent_id}")

    async def _validate_task_payload(self, task_kind: Any, params: Any) -> None:
        """Reject retired strategy cron task_kinds at write time.

        ``strategy_signal_alert`` / ``strategy_cycle`` are SUPERSEDED by the
        Task Trigger system. Any attempt to create / update a cron job with
        these kinds fails VISIBLY with a stable ``error_code`` so callers know
        to schedule strategy execution via a Task Trigger instead. Other kinds
        (``agent_chat_reply`` etc.) pass through untouched.
        """
        if task_kind in ("strategy_signal_alert", "strategy_cycle"):
            raise ValueError(
                json.dumps(
                    {
                        "error_code": "cron_strategy_kind_retired",
                        "message": (
                            "strategy cron task_kinds are retired; schedule "
                            "strategy execution via a Task Trigger "
                            "(doyoutrade-cli task trigger add ...)"
                        ),
                    },
                    ensure_ascii=False,
                )
            )

    @staticmethod
    def _validate_max_concurrency(value: Any) -> int:
        """Reject ``max_concurrency`` values that would silently freeze
        the job.

        ``Semaphore(0)`` is a valid Python object but ``.locked()`` is
        always true, so every fire takes the ``max_concurrency reached``
        skip path (see ``cron_manager.py:428``). The job appears
        registered and "running", but never invokes the agent — exactly
        the silent-failure shape this codebase keeps getting bitten by
        (cf. the next-fire-distance guard above).

        Negative values are nonsensical for a semaphore size. We allow
        ``None`` (DB default kicks in upstream — the API normalizes
        missing/zero via ``int(payload.get('max_concurrency') or 1)``,
        but direct callers / tests can still pass through).
        """
        if value is None:
            return 1
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"max_concurrency must be a positive integer, got "
                f"{type(value).__name__}: {value!r}"
            ) from exc
        if n < 1:
            raise ValueError(
                f"max_concurrency must be >= 1 (got {n}). A value of 0 "
                "would silently block every cron fire — set 1 for "
                "single-fire / no-overlap semantics."
            )
        return n

    # ── Tagged-union schedule helpers ──────────────────────────────────

    # Max future delta for a one-shot ``at`` job that wasn't explicitly
    # acknowledged. 30 days is generous (covers "remind me in two
    # weeks") while still rejecting "year off" mistakes.
    _AT_DISTANT_THRESHOLD = timedelta(days=30)

    # Floor for ``at`` schedules: an instant strictly in the past is
    # almost certainly an LLM clock-math error (e.g. computed against
    # the wrong day). We tolerate ``_AT_PAST_GRACE`` seconds of slack
    # so a job submitted 1s after its computed instant still fires
    # immediately, matching hermes' 45s one-shot grace window.
    _AT_PAST_GRACE = timedelta(seconds=45)

    _DURATION_UNITS = {
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
    }

    @classmethod
    def _parse_duration(cls, raw: Any) -> timedelta:
        """Parse ``60s`` / ``5m`` / ``2h`` / ``1d`` style durations.

        Reject unknown units / non-positive values with a clear error
        rather than silently coercing. Negative / zero durations are
        almost always an LLM mistake (e.g. ``"-30s"``).
        """
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(
                f"in_duration must be a non-empty string like '60s' / "
                f"'5m' / '2h' / '1d', got {raw!r}"
            )
        text = raw.strip().lower().replace(" ", "")
        # Find the first non-digit/dot character to split number/unit.
        for idx, ch in enumerate(text):
            if not (ch.isdigit() or ch == "."):
                num_str, unit = text[:idx], text[idx:]
                break
        else:
            raise ValueError(
                f"in_duration {raw!r} missing unit suffix. Use '60s' / "
                f"'5m' / '2h' / '1d'."
            )
        if not num_str:
            raise ValueError(
                f"in_duration {raw!r} missing numeric prefix. Use '60s' / "
                f"'5m' / '2h' / '1d'."
            )
        try:
            num = float(num_str)
        except ValueError as exc:
            raise ValueError(
                f"in_duration {raw!r}: numeric prefix {num_str!r} is "
                f"not a number"
            ) from exc
        if unit not in cls._DURATION_UNITS:
            raise ValueError(
                f"in_duration {raw!r}: unknown unit {unit!r}. Supported: "
                f"{sorted(set(cls._DURATION_UNITS.keys()))}"
            )
        seconds = num * cls._DURATION_UNITS[unit]
        if seconds <= 0:
            raise ValueError(
                f"in_duration {raw!r} resolved to {seconds}s; must be "
                "positive. A 0/negative delay is almost always a clock-"
                "math bug — recompute the target instant."
            )
        return timedelta(seconds=seconds)

    @classmethod
    def _validate_at_iso(
        cls,
        at_iso: Any,
        *,
        now: datetime | None = None,
        acknowledge_distant: bool = False,
    ) -> tuple[datetime, str]:
        """Parse + validate an ``at`` schedule's ISO instant.

        Returns the canonical ``(aware_datetime, iso_string)`` pair.
        Rejects:
          - non-string / empty input
          - unparseable ISO
          - naive instants (offset required — that's the whole point
            of ``at`` vs ``cron+timezone``)
          - past instants outside the grace window
          - future instants > 30 days unless ``acknowledge_distant``
        """
        if not isinstance(at_iso, str) or not at_iso.strip():
            raise ValueError(
                "at_iso is required for schedule_kind='at' — pass an "
                "ISO-8601 instant with explicit offset, e.g. "
                "'2026-05-24T10:23:00+08:00'"
            )
        try:
            parsed = datetime.fromisoformat(at_iso.strip())
        except ValueError as exc:
            raise ValueError(
                f"at_iso {at_iso!r} is not a valid ISO-8601 datetime: "
                f"{exc}. Example shape: '2026-05-24T10:23:00+08:00'."
            ) from exc
        if parsed.tzinfo is None:
            raise ValueError(
                f"at_iso {at_iso!r} is missing a timezone offset. "
                "Naive instants are ambiguous — append the offset that "
                "matches your local clock (e.g. '+08:00' for CST, "
                "'+00:00' for UTC)."
            )
        now_aware = now or datetime.now(timezone.utc)
        delta = parsed - now_aware
        if delta < -cls._AT_PAST_GRACE:
            raise ValueError(
                f"at_iso {parsed.isoformat()} is "
                f"{int(-delta.total_seconds())}s in the past (now is "
                f"{now_aware.isoformat()}). LLM clock-math error? "
                "Recompute the target instant against the current time."
            )
        if not acknowledge_distant and delta > cls._AT_DISTANT_THRESHOLD:
            raise ValueError(
                f"at_iso {parsed.isoformat()} is "
                f"{delta.days}d from now ({now_aware.isoformat()}), past "
                f"the 30-day one-shot threshold. For long-lead reminders "
                f"use a recurring schedule_kind='cron' or POST with "
                "acknowledge_distant_schedule=true."
            )
        # Canonicalize the stored string so the DB column is stable
        # even if the caller passed e.g. trailing whitespace.
        return parsed, parsed.isoformat()

    @classmethod
    def _resolve_schedule(
        cls,
        data: dict[str, Any],
        *,
        now: datetime | None = None,
        acknowledge_distant: bool = False,
    ) -> dict[str, Any]:
        """Normalize a create / update payload's schedule fields.

        Returns a partial dict with the canonical columns
        (``schedule_kind``, ``cron_expression``, ``timezone``,
        ``at_iso``, ``delete_after_run``) ready to merge into the
        repository write. For ``schedule_kind='at'`` callers may pass
        ``in_duration`` instead of ``at_iso``; we resolve to the same
        ``at_iso`` against ``now`` (host clock).

        Returned dict also contains a private ``_trigger`` key with
        the built APScheduler trigger so callers don't re-build it
        for ``_validate_next_fire_distance`` / register.
        """
        kind = (data.get("schedule_kind") or "").strip() or None
        # Auto-pick kind for legacy callers that pass cron_expression
        # without schedule_kind.
        if kind is None:
            if data.get("at_iso") or data.get("in_duration"):
                kind = "at"
            elif data.get("cron_expression"):
                kind = "cron"
            else:
                raise ValueError(
                    "schedule_kind is required (or pass one of "
                    "cron_expression / at_iso / in_duration so we can "
                    "infer it)"
                )
        if kind not in ("cron", "at"):
            raise ValueError(
                f"schedule_kind={kind!r} unsupported; expected 'cron' "
                "or 'at'."
            )
        out: dict[str, Any] = {"schedule_kind": kind}
        if kind == "cron":
            cron_expr, weekday_notice = cls._rewrite_unix_weekday_dow(
                data.get("cron_expression"),
            )
            trigger = cls._validate_cron_expression(
                cron_expr, data.get("timezone"),
            )
            raw_tz = data.get("timezone")
            resolved_tz = (
                raw_tz if isinstance(raw_tz, str) and raw_tz.strip()
                else "UTC"
            )
            now_aware = now or datetime.now(timezone.utc)
            next_fire = trigger.get_next_fire_time(None, now_aware)
            # ── Auto-promote calendar-pin one-shot to at-kind ─────────
            # Pattern: ``day`` AND ``month`` both pinned + next fire
            # within 24h. LLMs reach for ``--cron-expression`` because
            # cached training data tells them cron is THE way, but a
            # one-shot calendar pin is a worse representation of "fire
            # in N seconds/minutes" than ``--at`` for three reasons:
            #   1. Cron precision is 1 minute; "fire in 60s" submitted
            #      at HH:MM:54 becomes "next minute boundary" = ~6s
            #      delay (observed at session asst-70142ce43e81 where
            #      next_fire_in_seconds returned 0).
            #   2. Same pattern fires again next year → zombie row
            #      unless ``delete_after_run`` is set.
            #   3. Caller can't know "submit ``--in`` instead" without
            #      reading SKILL.md, which most sessions skip.
            # Promoting transparently fixes (1) and (2). The ``_notice``
            # field in the response educates the LLM for next time
            # without bouncing the current request.
            #
            # Caveats:
            #   * ``acknowledge_distant_schedule=true`` opts out — caller
            #     explicitly wants an annual reminder via cron-kind.
            #   * Caller-supplied ``delete_after_run`` survives the
            #     promotion (so explicit ``--keep-after-run`` works).
            should_promote = (
                cls._is_calendar_pin_one_shot(trigger)
                and not acknowledge_distant
                and next_fire is not None
                and (next_fire - now_aware) < timedelta(hours=24)
            )
            if should_promote:
                promoted_delete = bool(
                    data.get("delete_after_run", True),
                )
                out["schedule_kind"] = "at"
                # Keep the original cron_expression so the DB column
                # stays populated and the audit trail shows what the
                # LLM submitted (vs the synthetic minute/hour we
                # could derive from at_iso — same value here, but
                # carrying the original is honest).
                out["cron_expression"] = cron_expr
                out["timezone"] = resolved_tz
                out["at_iso"] = next_fire.isoformat()
                out["delete_after_run"] = promoted_delete
                out["_trigger"] = DateTrigger(run_date=next_fire)
                expr_for_msg = cron_expr
                out["_notice"] = (
                    f"Auto-promoted: cron_expression "
                    f"{expr_for_msg!r} (timezone={resolved_tz!r}) is "
                    "a one-shot calendar pin (day+month both "
                    "specified, fires once per year) whose next "
                    f"match is at {next_fire.isoformat()} — within "
                    "24h, so the system inferred this is a 'fire in "
                    "N seconds/minutes' intent. Stored as "
                    "schedule_kind='at' with delete_after_run=true; "
                    "the row will be deleted after fire — you do "
                    "NOT need a manual cleanup step. **For the next "
                    "such request, prefer `--in 60s` (or `--at "
                    "<ISO-8601+offset>`) directly** — they skip cron "
                    "expression / timezone math / minute-boundary "
                    "rounding entirely and are second-level precise."
                )
                return out
            # ── Non-promotable cron: keep as recurring ─────────────────
            out["cron_expression"] = cron_expr
            out["timezone"] = resolved_tz
            out["at_iso"] = None
            # Recurring jobs default delete_after_run=False (overridable).
            out["delete_after_run"] = bool(data.get("delete_after_run", False))
            out["_trigger"] = trigger
            if weekday_notice:
                out["_notice"] = weekday_notice
            return out
        # kind == "at"
        now_aware = now or datetime.now(timezone.utc)
        at_iso_raw = data.get("at_iso")
        in_duration = data.get("in_duration")
        if at_iso_raw and in_duration:
            raise ValueError(
                "schedule_kind='at' accepts at_iso OR in_duration, not "
                "both."
            )
        if in_duration is not None:
            delta = cls._parse_duration(in_duration)
            # Resolve against the host's local TZ so the stored
            # at_iso carries the offset the caller meant. Falling
            # back to UTC if tzlocal isn't installed.
            resolved = cls._resolve_system_iana_tz()
            if resolved is not None:
                _, local_iana = resolved
                from zoneinfo import ZoneInfo
                target = (now_aware + delta).astimezone(ZoneInfo(local_iana))
            else:
                target = now_aware + delta
            at_iso_raw = target.isoformat()
        parsed, canonical = cls._validate_at_iso(
            at_iso_raw, now=now_aware,
            acknowledge_distant=acknowledge_distant,
        )
        out["at_iso"] = canonical
        # Synthetic cron_expression so legacy schema (NOT NULL) and
        # legacy readers stay happy. The minute/hour/day/month match
        # the at instant in its native TZ; cron_manager NEVER reads
        # back this synthetic value for ``at`` jobs (it dispatches on
        # ``schedule_kind`` and builds a DateTrigger).
        out["cron_expression"] = (
            f"{parsed.minute} {parsed.hour} {parsed.day} "
            f"{parsed.month} *"
        )
        # Store the offset's IANA name when resolvable; otherwise
        # fall back to ``UTC`` so the column has a value. The
        # DateTrigger ignores this — at_iso carries the tz directly.
        if parsed.tzinfo is not None:
            offset = parsed.utcoffset() or timedelta(0)
            # Best effort: try to map back to the system local IANA
            # if offsets match; otherwise leave as ``UTC`` to avoid
            # storing meaningless abbreviations like ``+08:00``.
            sys_resolved = cls._resolve_system_iana_tz()
            if sys_resolved is not None:
                sys_tz, sys_iana = sys_resolved
                if now_aware.astimezone(sys_tz).utcoffset() == offset:
                    out["timezone"] = sys_iana
                else:
                    out["timezone"] = "UTC"
            else:
                out["timezone"] = "UTC"
        else:  # pragma: no cover — _validate_at_iso rejects naive
            out["timezone"] = "UTC"
        # One-shot defaults to auto-delete unless caller overrides.
        out["delete_after_run"] = bool(
            data.get("delete_after_run", True),
        )
        out["_trigger"] = DateTrigger(run_date=parsed)
        return out

    @staticmethod
    def _rewrite_unix_weekday_dow(expr: Any) -> tuple[str, str | None]:
        """Rewrite Unix/Vixie weekday DOW tokens for APScheduler.

        APScheduler ``from_crontab`` uses ``0=Mon .. 6=Sun``. Standard
        Unix cron uses ``0=Sun, 1=Mon .. 5=Fri``. The dominant LLM
        mistake for "工作日" is writing ``1-5``, which APScheduler
        interprets as **Tue–Sat** (Monday is skipped). Map the common
        Unix weekday literals to ``mon-fri`` instead.
        """
        if not isinstance(expr, str) or not expr.strip():
            return (expr if isinstance(expr, str) else ""), None
        parts = expr.strip().split()
        if len(parts) != 5:
            return expr, None
        dow = parts[4].replace(" ", "")
        unix_weekday_forms = frozenset({"1-5", "1,2,3,4,5"})
        if dow not in unix_weekday_forms:
            return expr, None
        rewritten = " ".join([*parts[:4], "mon-fri"])
        notice = (
            f"Auto-rewrote weekday field: day-of-week {parts[4]!r} follows "
            "Unix/Vixie numbering (0=Sun, 1=Mon..5=Fri), but DoYouTrade "
            "schedules via APScheduler where 0=Mon..6=Sun — so "
            f"{parts[4]!r} means Tue–Sat and skips Monday. Stored as "
            "'mon-fri' (Mon–Fri). For weekdays always use `mon-fri` "
            "(or APScheduler `0-4`), never bare `1-5`."
        )
        return rewritten, notice

    @staticmethod
    def _validate_cron_expression(expr: Any, tz: Any) -> CronTrigger:
        """Reject obviously-bad cron expressions before they hit the DB.

        Historically the assistant tool let the model hand-write 5-field
        cron strings, and a "30s later" intent would land "56" in the
        minute slot or "54" in the hour slot. Those rows survive in the
        database forever and blow up later (e.g. on server restart in
        ``start()``). Running the same check APScheduler will run, at
        write time, keeps the bad data out of the system entirely.

        ``tz`` is allowed to be missing — the DB column defaults to
        ``"UTC"`` and we validate against that. The cron expression is
        the only thing the model can get wrong; an unknown timezone
        comes through validators upstream (assistant tool / REST).

        Returns the built ``CronTrigger`` so callers (e.g.
        ``_validate_next_fire_distance``) can re-use it without parsing
        the expression twice.
        """
        if not isinstance(expr, str) or not expr.strip():
            raise ValueError("cron_expression is required")
        validate_tz = tz if isinstance(tz, str) and tz.strip() else "UTC"
        try:
            return CronTrigger.from_crontab(expr, timezone=validate_tz)
        except (ValueError, LookupError, TypeError) as exc:
            raise ValueError(
                f"invalid cron_expression {expr!r} (timezone={tz!r}): {exc}"
            ) from exc

    @staticmethod
    def _is_calendar_pin_one_shot(trigger: CronTrigger) -> bool:
        """True iff both ``day`` and ``month`` are pinned to specific
        values (e.g. ``19 9 24 5 *``). These patterns fire once per
        year, so the LLM's typical use is "fire in a few seconds /
        minutes from now"; anything further out is overwhelmingly a
        timezone-drift mistake (caller computed wall-clock HH:MM from
        local TZ but submitted the cron in a different TZ).

        ``RangeExpression`` subclasses ``AllExpression``, so
        ``isinstance(..., AllExpression)`` matches both wildcards
        (``*``) and concrete values (``5``); we need an exact type
        check to recognize a true ``*``. Multi-expression fields
        (e.g. ``1,15``) are also "not wildcard" — fine for our intent
        since those still pin specific calendar dates rather than
        recurring monthly.
        """
        fields = {f.name: f for f in trigger.fields}
        def _is_wildcard(field):
            return (
                len(field.expressions) == 1
                and type(field.expressions[0]) is AllExpression
            )
        day = fields.get("day")
        month = fields.get("month")
        if day is None or month is None:
            return False
        return not _is_wildcard(day) and not _is_wildcard(month)

    @staticmethod
    def _resolve_system_iana_tz() -> tuple[Any, str] | None:
        """Return ``(tzinfo, iana_name)`` for the server's local TZ,
        or ``None`` if it cannot be resolved as a real IANA key.

        We deliberately avoid ``datetime.now().astimezone().tzinfo``
        because that returns the *abbreviation* (e.g. ``CST``) which
        is NOT a valid ``ZoneInfo`` / APScheduler key — putting it in
        the error message used to send LLMs into a dead-end retry
        loop (``--timezone CST`` → "No time zone found with key
        CST"). ``tzlocal.get_localzone()`` returns a real ``ZoneInfo``
        whose ``.key`` is the IANA id (``Asia/Shanghai``), which is
        what the caller actually needs to copy-paste.
        """
        try:
            from tzlocal import get_localzone
        except ImportError:
            return None
        try:
            zi = get_localzone()
        except Exception:
            return None
        iana = getattr(zi, "key", None)
        if not iana:
            return None
        return zi, iana

    @staticmethod
    def _timezone_drift_hint(
        trigger: CronTrigger,
        tz: Any,
        next_fire: datetime,
        now_aware: datetime,
        delta: timedelta,
    ) -> str:
        """Build a TZ-drift-specific suffix for the distance error
        when the symptom matches: delta is approximately equal to the
        offset between the configured TZ and the server's local TZ.
        That's the smoking gun for "caller computed HH:MM from local
        wall clock but submitted timezone=different_tz".

        The suggested ``--timezone`` value in the hint is always a
        valid IANA key — we resolve it via ``tzlocal`` so the LLM
        can copy-paste it directly (see ``_resolve_system_iana_tz``
        for why ``astimezone().tzinfo`` is unsuitable).

        Returns "" when no drift signature matches (let the caller
        fall back to the generic "field-order or timezone mistake"
        wording).
        """
        resolved = AgentCronManager._resolve_system_iana_tz()
        if resolved is None:
            return ""
        local_tz, local_iana = resolved
        try:
            cfg_off = now_aware.astimezone(trigger.timezone).utcoffset()
            loc_off = now_aware.astimezone(local_tz).utcoffset()
        except (TypeError, ValueError):
            return ""
        if cfg_off is None or loc_off is None or cfg_off == loc_off:
            return ""
        offset_diff = abs(cfg_off - loc_off)
        # Match window covers the seconds between LLM clock read and
        # APScheduler resolving next_fire. If delta is far from this
        # diff, the bug is field-order (e.g. day/month swap), not TZ.
        if abs(delta - offset_diff) > _TIMEZONE_DRIFT_MATCH_WINDOW:
            return ""
        hours = offset_diff.total_seconds() / 3600
        try:
            cfg_label = (
                tz if isinstance(tz, str) and tz.strip()
                else str(trigger.timezone)
            )
        except Exception:
            cfg_label = "configured"
        local_wall = next_fire.astimezone(local_tz).isoformat()
        cfg_wall = next_fire.astimezone(trigger.timezone).isoformat()
        local_now_wall = now_aware.astimezone(local_tz).isoformat()
        return (
            f" The {hours:.1f}h offset exactly matches the difference "
            f"between timezone={cfg_label!r} and the system local TZ "
            f"({local_iana}); it looks like HH:MM was computed against "
            f"the local clock (now {local_now_wall}) but submitted "
            f"under {cfg_label!r}. In the configured TZ this job "
            f"fires at {cfg_wall}; in local TZ that wall clock is "
            f"{local_wall}. Either submit "
            f"--timezone {local_iana} or recompute HH:MM against "
            f"{cfg_label!r}."
        )

    @staticmethod
    def _validate_next_fire_distance(
        trigger: CronTrigger,
        expr: Any,
        tz: Any,
        *,
        acknowledge_distant: bool,
        now: datetime | None = None,
    ) -> None:
        """Backstop against syntactically-valid cron expressions whose
        next fire is far in the future — almost always a field-order
        or timezone bug.

        Two thresholds, chosen by pattern shape:

          * **Calendar-pin one-shot** (day AND month both specific,
            e.g. ``19 9 24 5 *``): tight 2-hour threshold. These
            schedules fire once per year, and the LLM's typical use
            of them is "fire in seconds/minutes from now"; the
            multi-hour-off case is the dominant TZ-drift bug
            (e.g. clock read in Asia/Shanghai, cron submitted with
            ``timezone="UTC"`` → +8h).
          * **Recurring** (everything else): the original 30-day
            threshold, catching gross "+1 year" mistakes like
            ``28 23 23 5 *`` while leaving normal "next Monday" /
            "first of every month" schedules alone.

        Real-world examples this catches:
          * ``28 23 23 5 *`` written for "30 seconds later" — pin
            pattern, next fire +1 year, trips the pin threshold.
          * ``19 9 24 5 *`` written for "30 seconds later" but in CST
            mind-state and submitted as UTC — pin pattern, next fire
            +8h, trips the pin threshold and the error includes a
            TZ-drift-specific hint (the 8h matches CST↔UTC offset).
          * Recurring ``0 9 * * *`` with bogus year-far next fire —
            falls back to the 30-day threshold.

        Intentional far-future schedules ("remind me next quarter")
        can opt out by passing ``acknowledge_distant=True`` (the API
        surface forwards a ``acknowledge_distant_schedule`` payload
        field; the CLI does not expose this flag yet — by design,
        that path is the LLM footgun we are protecting against).
        """
        if acknowledge_distant:
            return
        now_aware = now or datetime.now(timezone.utc)
        next_fire = trigger.get_next_fire_time(None, now_aware)
        if next_fire is None:
            # Trigger has no future fire (e.g. trigger.end_date in the
            # past). Let APScheduler / downstream raise on register.
            return
        delta = next_fire - now_aware
        is_pin = AgentCronManager._is_calendar_pin_one_shot(trigger)
        threshold = (
            _CALENDAR_PIN_DISTANT_THRESHOLD if is_pin
            else _DISTANT_SCHEDULE_THRESHOLD
        )
        if delta <= threshold:
            return
        # ── Boundary-miss detection (pin pattern only) ──────────────
        # When the LLM computes "now + N seconds" by picking the next
        # minute boundary and submits at the exact second the chosen
        # minute starts, APScheduler considers that match "already
        # passed" and wraps to the same instant NEXT YEAR. The error
        # then reads "next fires +8760h" which is misleading — the
        # real fault is "you picked a minute that just elapsed".
        # Detect this by asking the trigger for a match in the recent
        # past: if there's one within the last 5 minutes, the LLM
        # almost certainly meant THIS year's instance and missed by
        # seconds.
        boundary_miss_recent_match: datetime | None = None
        if is_pin and delta > timedelta(days=360):
            try:
                recent = trigger.get_next_fire_time(
                    None, now_aware - timedelta(minutes=5),
                )
            except Exception:
                recent = None
            if recent is not None and recent <= now_aware:
                boundary_miss_recent_match = recent
        if boundary_miss_recent_match is not None:
            raise ValueError(
                f"cron_expression {expr!r} (timezone={tz!r}) is a "
                f"calendar pin whose chosen minute just elapsed (last "
                f"match was at "
                f"{boundary_miss_recent_match.isoformat()}; now is "
                f"{now_aware.isoformat()}). It therefore next fires at "
                f"{next_fire.isoformat()} — wrapped to the same "
                f"instant next year, almost certainly NOT what you "
                "meant. Cron expressions only have 1-minute precision, "
                "so 'fire in 60 seconds' through cron is "
                "intrinsically unreliable. **Use `doyoutrade-cli cron "
                "create --in 60s` instead** — second-precise, "
                "delete_after_run=true (no zombie cleanup needed). "
                "If a real annual reminder is intended, callers using "
                "the in-process API (not CLI) may pass "
                "``acknowledge_distant_schedule=true``."
            )
        # ── Normal distant-fire rejection ────────────────────────────
        # Build the human-readable delta. For pin patterns we surface
        # hours; for recurring patterns we keep the "X days" style.
        if is_pin:
            hours = delta.total_seconds() / 3600
            delta_human = f"{hours:.1f}h"
            pattern_phrase = (
                "one-shot calendar pin (day+month both specific, "
                "fires once per year) — expected to fire within "
                f"{int(_CALENDAR_PIN_DISTANT_THRESHOLD.total_seconds() // 3600)}h "
                "for typical LLM 'fire in N seconds' usage"
            )
            tz_hint = AgentCronManager._timezone_drift_hint(
                trigger, tz, next_fire, now_aware, delta,
            )
            # CLI-actionable fix: --in is the right tool for sub-day
            # one-shots, --at handles a specific future instant. The
            # API-only ``acknowledge_distant_schedule`` flag is NOT
            # surfaced here — pointing LLMs at it sent them on a
            # dead-end retry loop trying ``--acknowledge-distant-
            # schedule`` as a CLI flag (observed session
            # asst-500105d61a41).
            cli_recovery = (
                " **Recommended fix**: for relative delays use "
                "`--in <duration>` (e.g. `--in 60s` for one minute, "
                "`--in 5m`, `--in 2h`); for an explicit instant use "
                "`--at <ISO-8601+offset>`. Both bypass cron expression "
                "+ timezone math entirely."
            )
        else:
            delta_human = f"{delta.days} days"
            pattern_phrase = "schedule"
            tz_hint = ""
            cli_recovery = (
                " If a far-future recurring schedule really is "
                "intentional, callers using the in-process API "
                "(not CLI) may pass "
                "``acknowledge_distant_schedule=true`` to opt out."
            )
        raise ValueError(
            f"cron_expression {expr!r} (timezone={tz!r}) next fires at "
            f"{next_fire.isoformat()}, which is {delta_human} from "
            f"now ({now_aware.isoformat()}). Pattern is a "
            f"{pattern_phrase}; this is usually a field-order or "
            f"timezone mistake — recheck minute/hour positions and "
            f"whether the time was computed in the same timezone the "
            f"job stores.{tz_hint}{cli_recovery}"
        )

    async def _post_fire_cleanup(
        self,
        job: dict[str, Any],
        fired_at: datetime,
        *,
        fire_span: Any = None,
    ) -> None:
        """Honor one-shot semantics after a fire completes.

        Two paths converge here:

          * **Explicit ``delete_after_run=True``** (the new
            tagged-union schedule path — default for ``at`` jobs,
            opt-in for ``cron`` jobs): hard-delete the row + scheduler
            entry. Cleanest semantics, no zombie state.
          * **Legacy calendar-pin pattern** (``cron`` row whose
            day+month are both specific, no explicit
            ``delete_after_run``): fall back to the soft
            auto-disable path from the 2026-05-24 PR. Leaves the row
            with ``enabled=false`` and ``last_status`` intact so
            operators can audit.

        Best-effort: errors are logged but never block the primary
        terminal-state write.
        """
        if bool(job.get("delete_after_run")):
            job_id = job.get("id")
            if not job_id:
                return
            try:
                await self._deregister_best_effort(
                    job_id, op="post_fire_delete",
                )
                await self._repo.delete_job(job_id)
            except Exception as exc:
                logger.error(
                    "post_fire_delete failed job_id=%s exc_type=%s "
                    "exc=%s — row stays in DB, may re-register at "
                    "next boot",
                    job_id, type(exc).__name__, exc,
                )
                if fire_span is not None:
                    fire_span.set_attribute(
                        "cron.post_fire.action", "delete_failed",
                    )
                return
            logger.info(
                "cron_job %s post-fire deleted "
                "(delete_after_run=true)", job_id,
            )
            if fire_span is not None:
                fire_span.set_attribute(
                    "cron.post_fire.action", "deleted",
                )
            return
        await self._maybe_auto_disable_one_shot(
            job, fired_at, fire_span=fire_span,
        )

    async def _maybe_auto_disable_one_shot(
        self,
        job: dict[str, Any],
        fired_at: datetime,
        *,
        fire_span: Any = None,
    ) -> None:
        """Auto-disable calendar-pin one-shot jobs after they fire.

        Calendar pins (day+month both specific) fire once per year by
        construction, but LLM/user intent is almost always "fire
        once" — leaving the row enabled means it zombie-fires next
        May 24 with stale context. Observed at session
        asst-91bfd63bf186: 7 pin patterns named "30秒后打招呼"
        accumulated from prior LLM tests, all still enabled to fire
        annually.

        We only act when the next next-fire (from just after this
        run) is at least ~6 months out — that confirms the schedule
        wrapped to next year and excludes any non-pin pattern that
        somehow trips the detector. We tolerate failure of the
        repository upsert / deregister so this never blocks the
        primary fire's bookkeeping.
        """
        job_id = job.get("id")
        if not job_id or not job.get("enabled", False):
            return
        try:
            trigger = self._validate_cron_expression(
                job.get("cron_expression"), job.get("timezone"),
            )
        except ValueError:
            return
        if not self._is_calendar_pin_one_shot(trigger):
            return
        # Compute the next fire AFTER this run. APScheduler's
        # ``get_next_fire_time`` returns the next match strictly
        # >= the second argument, so add ``+1s`` to skip the run we
        # just completed.
        probe_now = fired_at + timedelta(seconds=1)
        try:
            next_fire = trigger.get_next_fire_time(None, probe_now)
        except Exception:
            return
        if next_fire is None:
            return
        if (next_fire - probe_now) < _ONE_SHOT_AUTO_DISABLE_THRESHOLD:
            # Manual trigger of a future-dated pin, or schedule
            # didn't wrap to next year — don't strip an upcoming
            # legitimate fire.
            return
        try:
            await self._deregister_best_effort(
                job_id, op="auto_disable_one_shot",
            )
            await self._repo.upsert_job({
                "id": job_id, "enabled": False,
            })
        except Exception as exc:
            # Best-effort: don't block the run's terminal write on
            # this. Log loudly so operators can see the zombie row
            # still enabled.
            logger.error(
                "auto_disable_one_shot failed job_id=%s "
                "exc_type=%s exc=%s — row stays enabled, will "
                "zombie-fire next year",
                job_id, type(exc).__name__, exc,
            )
            if fire_span is not None:
                fire_span.set_attribute(
                    "cron.auto_disable_one_shot.status", "error",
                )
            return
        logger.info(
            "cron_job %s auto-disabled after fire — calendar pin "
            "one-shot (next match was %s, %.0f days away)",
            job_id, next_fire.isoformat(),
            (next_fire - probe_now).total_seconds() / 86400,
        )
        if fire_span is not None:
            fire_span.set_attribute(
                "cron.auto_disable_one_shot.status", "ok",
            )
            fire_span.set_attribute(
                "cron.auto_disable_one_shot.next_match",
                next_fire.isoformat(),
            )

    @staticmethod
    def _annotate_next_fire(
        job: dict[str, Any], trigger: CronTrigger,
    ) -> None:
        """Attach ``next_fire_time`` (ISO-8601) and
        ``next_fire_in_seconds`` (signed int relative to ``now``) to
        the job dict. The relative field exists alongside the ISO
        string because LLMs misread TZ offsets in ``+00:00``
        timestamps; the integer makes "fires 28800 seconds from now"
        impossible to misinterpret as "in 30 seconds".
        """
        now_aware = datetime.now(timezone.utc)
        next_fire = trigger.get_next_fire_time(None, now_aware)
        if next_fire is None:
            job["next_fire_time"] = None
            job["next_fire_in_seconds"] = None
            return
        job["next_fire_time"] = next_fire.isoformat()
        job["next_fire_in_seconds"] = int(
            (next_fire - now_aware).total_seconds()
        )

    async def create_job(
        self,
        data: dict[str, Any],
        *,
        acknowledge_distant_schedule: bool = False,
    ) -> dict[str, Any]:
        """Create and register a new cron job.

        Accepts a tagged-union schedule:
          * ``schedule_kind='cron'`` + ``cron_expression`` (+ optional
            ``timezone``) — recurring, existing behaviour.
          * ``schedule_kind='at'`` + ``at_iso`` OR ``in_duration`` —
            one-shot fire at the explicit instant. Eliminates TZ
            drift because the ISO carries the offset directly.

        Returns a dict with synthetic ``next_fire_time`` (ISO-8601)
        and ``next_fire_in_seconds`` (relative int) fields. LLM-safe
        sanity check at write time — if the relative-seconds doesn't
        match the caller's intent, the schedule is wrong.
        """
        await self._ensure_agent_exists(data.get("agent_id"))
        if "max_concurrency" in data:
            self._validate_max_concurrency(data.get("max_concurrency"))
        resolved = self._resolve_schedule(
            data, acknowledge_distant=acknowledge_distant_schedule,
        )
        trigger = resolved.pop("_trigger")
        # ``_notice`` is a response-only field set by
        # ``_resolve_schedule`` when it transparently transforms the
        # request (e.g. auto-promoting a calendar-pin cron to
        # at-kind). Surface it on the returned dict so the CLI/LLM
        # sees the explanation — NEVER persist underscore-prefixed
        # keys, they are not DB columns.
        notice = resolved.pop("_notice", None)
        # cron-kind needs the "next fire too far" backstop. at-kind
        # already enforced its future-window in _validate_at_iso so
        # don't double-validate. (Auto-promoted rows are at-kind by
        # the time we get here.)
        if resolved["schedule_kind"] == "cron":
            self._validate_next_fire_distance(
                trigger,
                resolved.get("cron_expression"),
                resolved.get("timezone"),
                acknowledge_distant=acknowledge_distant_schedule,
            )
        # Merge resolved schedule columns into the persistence payload.
        # ``in_duration`` is a request-only field (resolved into
        # ``at_iso``) — drop it before write so the repo doesn't
        # complain about unknown columns.
        persistence = {**data, **resolved}
        persistence.pop("in_duration", None)
        await self._validate_task_payload(
            persistence.get("task_kind"),
            persistence.get("task_params_json"),
        )
        job = await self._repo.upsert_job(persistence)
        await self._register(job)
        self._annotate_next_fire(job, trigger)
        if notice:
            job["_notice"] = notice
        return job

    async def update_job(
        self,
        job_id: str,
        updates: dict[str, Any],
        *,
        acknowledge_distant_schedule: bool = False,
    ) -> dict[str, Any]:
        """Update a cron job and re-register it.

        Any change to a schedule-related field (``schedule_kind`` /
        ``cron_expression`` / ``timezone`` / ``at_iso`` /
        ``in_duration``) re-runs the full schedule validation against
        the merged post-update view, ensuring partial updates can't
        leave the row in a kind/data mismatch (e.g. flipping kind to
        ``at`` without supplying ``at_iso``).
        """
        existing = await self._repo.get_job(job_id)
        if not existing:
            raise ValueError(f"Cron job not found: {job_id}")
        if "agent_id" in updates and updates["agent_id"] != existing.get("agent_id"):
            await self._ensure_agent_exists(updates["agent_id"])
        if "max_concurrency" in updates:
            self._validate_max_concurrency(updates.get("max_concurrency"))
        schedule_keys = (
            "schedule_kind", "cron_expression", "timezone",
            "at_iso", "in_duration",
        )
        schedule_changed = any(k in updates for k in schedule_keys)
        trigger: Any = None
        if schedule_changed:
            snapshot = {
                "schedule_kind": updates.get(
                    "schedule_kind", existing.get("schedule_kind"),
                ),
                "cron_expression": updates.get(
                    "cron_expression", existing.get("cron_expression"),
                ),
                "timezone": updates.get(
                    "timezone", existing.get("timezone"),
                ),
                "at_iso": updates.get(
                    "at_iso", existing.get("at_iso"),
                ),
                # in_duration is a request-only override; the persisted
                # row doesn't carry it. Honored only when the caller
                # passes it on THIS update.
                "in_duration": updates.get("in_duration"),
                "delete_after_run": updates.get(
                    "delete_after_run", existing.get("delete_after_run"),
                ),
            }
            resolved = self._resolve_schedule(
                snapshot,
                acknowledge_distant=acknowledge_distant_schedule,
            )
            trigger = resolved.pop("_trigger")
            update_notice = resolved.pop("_notice", None)
            for k in (
                "schedule_kind", "cron_expression", "timezone",
                "at_iso", "delete_after_run",
            ):
                updates[k] = resolved[k]
            updates.pop("in_duration", None)
            if resolved["schedule_kind"] == "cron":
                self._validate_next_fire_distance(
                    trigger,
                    resolved.get("cron_expression"),
                    resolved.get("timezone"),
                    acknowledge_distant=acknowledge_distant_schedule,
                )
        else:
            update_notice = None
        merged = {**existing, **updates, "id": job_id}
        # Reject retired strategy cron task_kinds on the merged view so an
        # update that flips an existing job onto a strategy kind still fails
        # visibly.
        await self._validate_task_payload(
            merged.get("task_kind"),
            merged.get("task_params_json"),
        )
        job = await self._repo.upsert_job(merged)
        await self._deregister_best_effort(job_id, op="update_job")
        if job["enabled"]:
            await self._register(job)
        if trigger is None:
            # Schedule untouched — rebuild trigger from the persisted
            # row for the next_fire echo. Dispatch by kind so at-rows
            # don't accidentally hit the cron parser.
            try:
                if job.get("schedule_kind") == "at":
                    run_date = datetime.fromisoformat(job["at_iso"])
                    trigger = DateTrigger(run_date=run_date)
                else:
                    trigger = self._validate_cron_expression(
                        job.get("cron_expression"), job.get("timezone"),
                    )
            except (ValueError, KeyError, TypeError) as exc:
                logger.error(
                    "update_job: rebuild trigger failed for job_id=%s "
                    "kind=%s reason=%s — omitting next_fire_time echo",
                    job_id, job.get("schedule_kind"), exc,
                )
                trigger = None
        if trigger is not None:
            self._annotate_next_fire(job, trigger)
        if update_notice:
            job["_notice"] = update_notice
        return job

    async def delete_job(self, job_id: str) -> None:
        """Delete a cron job and deregister from APScheduler."""
        await self._deregister_best_effort(job_id, op="delete_job")
        await self._repo.delete_job(job_id)

    async def pause_job(self, job_id: str) -> dict[str, Any]:
        """Pause a cron job (remove from scheduler but keep DB record)."""
        await self._deregister_best_effort(job_id, op="pause_job")
        return await self._repo.upsert_job({"id": job_id, "enabled": False})

    async def resume_job(self, job_id: str) -> dict[str, Any]:
        """Resume a paused cron job."""
        job = await self._repo.get_job(job_id)
        if not job:
            raise ValueError(f"Cron job not found: {job_id}")
        await self._repo.upsert_job({"id": job_id, "enabled": True})
        job["enabled"] = True
        await self._register(job)
        return job

    async def trigger_job(self, job_id: str) -> str:
        """Manually fire a job. Pre-creates the cron_job_runs row so the caller
        immediately receives a stable run id to poll/inspect.

        Returns the cron_job_run_id (``crun-...``). The actual job runs
        fire-and-forget via ``asyncio.create_task``.
        """
        job = await self._repo.get_job(job_id)
        if not job:
            raise ValueError(f"Cron job not found: {job_id}")

        fired_at = datetime.now(timezone.utc)
        run_id = f"crun-{uuid.uuid4().hex[:12]}"
        if self._run_repo is not None:
            await self._run_repo.create_run({
                "id": run_id,
                "job_id": job_id,
                "fired_at": fired_at,
                "started_at": fired_at,
                "status": "running",
                "pre_kind": (job.get("pre_action") or {}).get("kind"),
            })

        asyncio.create_task(
            self._execute(
                job_id,
                _prebuilt_run={"id": run_id, "fired_at": fired_at},
            )
        )
        return run_id

    async def list_jobs(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """List all jobs, optionally filtered by agent_id."""
        return await self._repo.list_jobs(agent_id or "")

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        return await self._repo.get_job(job_id)

    # ── Execution ─────────────────────────────────────────────────────────────

    async def _execute(
        self,
        job_id: str,
        *,
        _prebuilt_run: dict | None = None,
    ) -> None:
        """Fire one cron tick: optional pre_action → render template → invoke agent.

        When ``_prebuilt_run`` is provided (manual trigger path), reuse its
        ``id`` / ``fired_at`` instead of creating a fresh row, so the
        synchronous caller and this fire-and-forget task agree on a single
        ``cron_job_runs`` row per fire.
        """
        sem = self._sems.get(job_id)
        if sem is None:
            logger.warning("No semaphore for job_id=%s, skipping", job_id)
            if self._run_repo is not None and _prebuilt_run is not None:
                await self._run_repo.update_run(_prebuilt_run["id"], {
                    "status": "skipped",
                    "finished_at": datetime.now(timezone.utc),
                    "agent_error": "no_semaphore_for_job",
                })
            return

        # Concurrency guard: if the semaphore is already held, record a skipped row.
        if sem.locked():
            logger.info("Cron job %s skipped: max_concurrency reached", job_id)
            if self._run_repo is not None and _prebuilt_run is None:
                now = datetime.now(timezone.utc)
                await self._run_repo.create_run({
                    "id": f"crun-{uuid.uuid4().hex[:12]}",
                    "job_id": job_id,
                    "fired_at": now,
                    "started_at": now,
                    "status": "skipped",
                })
            elif _prebuilt_run is not None and self._run_repo is not None:
                # The trigger caller already inserted a row. Mark it as skipped.
                await self._run_repo.update_run(_prebuilt_run["id"], {
                    "status": "skipped",
                    "finished_at": datetime.now(timezone.utc),
                })
            return

        async with sem:
            job = await self._repo.get_job(job_id)
            if not job or not job.get("enabled"):
                logger.info("Cron job %s disabled or missing, skipping", job_id)
                if self._run_repo is not None and _prebuilt_run is not None:
                    await self._run_repo.update_run(_prebuilt_run["id"], {
                        "status": "skipped",
                        "finished_at": datetime.now(timezone.utc),
                        "agent_error": "job_disabled_or_missing",
                    })
                return

            if _prebuilt_run is not None:
                run_id_local = _prebuilt_run["id"]
                fired_at = _prebuilt_run["fired_at"]
            else:
                fired_at = datetime.now(timezone.utc)
                run_id_local = f"crun-{uuid.uuid4().hex[:12]}"

            pre_action = job.get("pre_action") or None
            kind_attr = (pre_action or {}).get("kind") or ""
            task_kind: str | None = job.get("task_kind") or None

            task_params = job.get("task_params_json")
            if not isinstance(task_params, dict):
                task_params = {}
            skip_meta = ashare_continuous_trading_skip_reason(
                fired_at,
                timezone=str(job.get("timezone") or "Asia/Shanghai"),
                trading_session=task_params.get("trading_session"),
                manual=_prebuilt_run is not None,
            )
            if skip_meta is not None:
                finished_at = datetime.now(timezone.utc)
                logger.info(
                    "Cron job %s skipped reason=%s hint=%s",
                    job_id,
                    skip_meta["reason"],
                    skip_meta["hint"],
                )
                if self._run_repo is not None:
                    skip_payload = {
                        "status": "skipped",
                        "finished_at": finished_at,
                        "agent_error": skip_meta["reason"],
                        "cron_task_kind": task_kind,
                    }
                    if _prebuilt_run is not None:
                        await self._run_repo.update_run(
                            run_id_local, skip_payload,
                        )
                    else:
                        await self._run_repo.create_run({
                            "id": run_id_local,
                            "job_id": job_id,
                            "fired_at": fired_at,
                            "started_at": fired_at,
                            **skip_payload,
                        })
                await self._repo.update_job_state(
                    job_id,
                    last_status="skipped",
                    last_run_at=fired_at,
                    last_error=skip_meta["reason"],
                )
                return

            if self._run_repo is not None and _prebuilt_run is None:
                await self._run_repo.create_run({
                    "id": run_id_local,
                    "job_id": job_id,
                    "fired_at": fired_at,
                    "started_at": fired_at,
                    "status": "running",
                    "pre_kind": (pre_action or {}).get("kind"),
                    "cron_task_kind": task_kind,
                })

            await self._repo.update_job_state(job_id, last_status="running", last_run_at=fired_at)

            with tracer.start_as_current_span("cron.job.fire") as fire_span:
                fire_span.set_attribute("cron.job_id", str(job_id))
                fire_span.set_attribute("cron.job_run_id", run_id_local)
                fire_span.set_attribute("cron.kind", str(kind_attr))
                fire_span.set_attribute("cron.task.kind", task_kind or "")

                # Persist the fire trace_id so an operator with only a trace_id
                # can reverse-resolve this cron run. Skip the no-op tracer's
                # all-zero / invalid trace so a NULL means "untraced", never
                # "0000…" masquerading as a real trace. Best-effort: a write
                # failure must not abort the fire, but it must be visible.
                fire_trace_id = _format_trace_id(fire_span)
                if fire_trace_id is not None and self._run_repo is not None:
                    try:
                        await self._run_repo.update_run(run_id_local, {"trace_id": fire_trace_id})
                    except Exception as exc:
                        logger.error(
                            "Cron fire trace_id persist failed job=%s run=%s trace=%s: %s: %s",
                            job_id,
                            run_id_local,
                            fire_trace_id,
                            type(exc).__name__,
                            exc,
                        )

                # ── Task pipeline (new) ──
                # When ``task_kind`` is set we dispatch the whole fire through
                # ``JobTaskRegistry`` and skip the legacy pre_action + render
                # path below entirely. Legacy rows with no ``task_kind`` fall
                # through to the historical pipeline so old data keeps
                # firing without intervention.
                if task_kind:
                    await self._execute_task_pipeline(
                        job=job,
                        task_kind=task_kind,
                        cron_job_run_id=run_id_local,
                        fired_at=fired_at,
                        fire_span=fire_span,
                    )
                    return

                # ── Pre-action ──
                pre_result: PreActionResult | None = None
                if pre_action:
                    kind = pre_action.get("kind")
                    params = pre_action.get("params") or {}
                    with tracer.start_as_current_span("cron.pre_action") as pre_span:
                        pre_span.set_attribute("cron.pre_action.kind", str(kind))
                        executor = self._registry.get(kind) if isinstance(kind, str) else None
                        if executor is None:
                            pre_result = PreActionResult(status="error", error=f"unknown_kind: {kind!r}")
                        else:
                            try:
                                ctx = JobRunContext(
                                    cron_job_run_id=run_id_local,
                                    job_id=job_id,
                                    fired_at=fired_at,
                                )
                                pre_result = await executor.execute(params, ctx)
                            except Exception as exc:
                                logger.exception(
                                    "Cron pre_action raised job=%s kind=%s", job_id, kind,
                                )
                                pre_result = PreActionResult(
                                    status="error",
                                    error=f"{type(exc).__name__}: {exc}",
                                )
                        pre_span.set_attribute("cron.pre_action.status", str(pre_result.status))
                        if pre_result.run_id:
                            pre_span.set_attribute("cron.pre_action.run_id", str(pre_result.run_id))

                    if self._run_repo is not None:
                        await self._run_repo.update_run(run_id_local, {
                            "pre_status": pre_result.status,
                            "pre_run_id": pre_result.run_id,
                            "pre_debug_session_id": pre_result.debug_session_id,
                            "pre_result_json": pre_result.data,
                            "pre_error": pre_result.error,
                        })

                # ── Render template ──
                if pre_result is None:
                    pre_block: dict[str, Any] | None = None
                else:
                    pre_block = {
                        "status": pre_result.status,
                        "run_id": pre_result.run_id,
                        "debug_session_id": pre_result.debug_session_id,
                        "data": pre_result.data,
                        "error": pre_result.error,
                    }

                try:
                    rendered_body = Template(job["input_template"]).render(
                        now=fired_at.isoformat(),
                        job={
                            "id": job["id"],
                            "name": job["name"],
                            "agent_id": job["agent_id"],
                        },
                        pre=pre_block,
                    )
                    # Prepend a delimited trigger header so the agent
                    # immediately knows this is a cron-driven session
                    # rather than a user typing. Without this, the
                    # agent burns a tool call running ``cron list`` to
                    # figure out what fired (observed at
                    # asst-91bfd63bf186).
                    rendered = _build_cron_trigger_header(
                        job, fired_at, body=rendered_body,
                    ) + "\n\n" + rendered_body
                except Exception as exc:
                    logger.exception("Cron template render failed job=%s", job_id)
                    if self._run_repo is not None:
                        await self._run_repo.update_run(run_id_local, {
                            "status": "error",
                            "finished_at": datetime.now(timezone.utc),
                            "agent_error": f"template_render_error: {exc}",
                        })
                    await self._repo.update_job_state(
                        job_id,
                        last_status="error",
                        last_error=f"template_render_error: {exc}",
                    )
                    fire_span.set_attribute("cron.terminal_status", "error")
                    return

                # ── Invoke agent ──
                session = None
                with tracer.start_as_current_span("cron.agent_dispatch") as agent_span:
                    agent_span.set_attribute("cron.agent_id", str(job.get("agent_id")))
                    try:
                        session = await self._svc.create_session(
                            agent_id=job["agent_id"],
                            title=f"[Cron] {job['name']}",
                            config={
                                # Marks this session as cron-fired so the
                                # API can block recursive cron creation
                                # (see ``create_agent_cron_job`` in
                                # doyoutrade/api/app.py).
                                "cron_origin": True,
                                "cron_origin_job_id": job["id"],
                            },
                        )
                        agent_span.set_attribute(
                            "cron.agent_session_id",
                            str((session or {}).get("session_id") or ""),
                        )
                        await self._svc.send_message(
                            session_id=session["session_id"], content=rendered,
                        )
                    except Exception as exc:
                        logger.exception("Cron agent dispatch failed job=%s", job_id)
                        agent_span.set_attribute("cron.agent_dispatch.status", "error")
                        if self._run_repo is not None:
                            await self._run_repo.update_run(run_id_local, {
                                "status": "agent_failed",
                                "finished_at": datetime.now(timezone.utc),
                                "agent_session_id": (session or {}).get("session_id"),
                                "agent_error": f"{type(exc).__name__}: {exc}",
                            })
                        await self._repo.update_job_state(
                            job_id, last_status="error", last_error=str(exc),
                        )
                        fire_span.set_attribute("cron.terminal_status", "agent_failed")
                        return
                    agent_span.set_attribute("cron.agent_dispatch.status", "ok")

                # ── Terminal bookkeeping ──
                final_status = (
                    "success"
                    if (pre_result is None or pre_result.status == "ok")
                    else "pre_failed"
                )
                fire_span.set_attribute("cron.terminal_status", final_status)
                if self._run_repo is not None:
                    await self._run_repo.update_run(run_id_local, {
                        "status": final_status,
                        "finished_at": datetime.now(timezone.utc),
                        "agent_session_id": session["session_id"],
                    })
                await self._repo.update_job_state(
                    job_id,
                    last_status=final_status,
                    last_run_session_id=session["session_id"],
                )
                logger.info(
                    "Cron job %s completed status=%s session_id=%s",
                    job_id, final_status, session["session_id"],
                )
                await self._post_fire_cleanup(
                    job, fired_at, fire_span=fire_span,
                )

    async def _execute_task_pipeline(
        self,
        *,
        job: dict[str, Any],
        task_kind: str,
        cron_job_run_id: str,
        fired_at: datetime,
        fire_span: Any,
    ) -> None:
        """Dispatch one cron fire through :class:`JobTaskRegistry`.

        The executor owns the full pipeline (gather data → invoke agent →
        deliver). cron_manager's job here is bookkeeping: build the run
        context, look up the executor, persist the :class:`TaskResult`
        onto ``cron_job_runs`` (so debug/API consumers can correlate via
        ``run_id``), and update ``cron_jobs.last_*`` state for the
        scheduler page.
        """

        job_id = job["id"]
        executor = self.task_registry.get(task_kind)
        if executor is None:
            err = f"unknown_task_kind: {task_kind!r}"
            logger.error("Cron job %s rejected: %s", job_id, err)
            fire_span.set_attribute("cron.terminal_status", "error")
            if self._run_repo is not None:
                await self._run_repo.update_run(cron_job_run_id, {
                    "status": "error",
                    "finished_at": datetime.now(timezone.utc),
                    "agent_error": err,
                })
            await self._repo.update_job_state(
                job_id, last_status="error", last_error=err,
            )
            return

        ctx = JobRunContext(
            cron_job_run_id=cron_job_run_id,
            job_id=job_id,
            fired_at=fired_at,
        )
        params = job.get("task_params_json") or {}
        if not isinstance(params, dict):
            # ``task_params_json`` is supposed to be a dict per the
            # ``cron_jobs.task_params_json`` column. A non-dict here means
            # either schema corruption or someone hand-wrote bad JSON. The
            # old code silently swapped to ``{}`` and proceeded, which made
            # the executor read defaults / its own ``agent_id`` fallback and
            # quietly succeed against the wrong configuration. Mark the run
            # failed and surface the actual stored type so it's debuggable.
            err = (
                f"task_params_json must be an object, got "
                f"{type(params).__name__}: {params!r}"
            )
            logger.error(
                "Cron job %s rejected: %s (kind=%s)",
                job_id, err, task_kind,
            )
            fire_span.set_attribute("cron.terminal_status", "error")
            if self._run_repo is not None:
                await self._run_repo.update_run(cron_job_run_id, {
                    "status": "error",
                    "finished_at": datetime.now(timezone.utc),
                    "agent_error": f"invalid_task_params_json: {err}",
                })
            await self._repo.update_job_state(
                job_id, last_status="error", last_error=err,
            )
            return

        # task_params_json may legitimately be missing ``agent_id`` if the
        # caller omitted it; cron_manager treats the row-level ``agent_id``
        # as the authoritative composer for the LLM step, so we merge it
        # forward without clobbering an explicit override.
        params = dict(params)
        params.setdefault("agent_id", job.get("agent_id"))

        result: TaskResult
        try:
            result = await executor.run(params, ctx)
        except Exception as exc:
            logger.exception(
                "Cron task executor raised job=%s kind=%s", job_id, task_kind,
            )
            result = TaskResult(
                status="failed",
                error=f"executor_raised: {type(exc).__name__}: {exc}",
            )

        terminal_status = "success" if result.status == "ok" else "agent_failed"
        fire_span.set_attribute("cron.terminal_status", terminal_status)
        fire_span.set_attribute(
            "cron.delivery.status", str(result.delivery_status or "none"),
        )

        # Promote a delivery failure into the run row's ``agent_error`` so it
        # surfaces in the existing History modal Error column without needing
        # a new schema column. A run that "succeeded" (LLM produced text) but
        # whose user-side push raised is still a problem the operator needs
        # to see — the dedicated ``delivery_status`` tag answers *what*
        # happened, this string answers *why*.
        agent_error_for_row = result.error
        if result.delivery_status == "failed":
            fire_span.set_attribute(
                "cron.delivery.error", str(result.delivery_error or "")[:500],
            )
            # §错误可见性: a fire whose LLM call succeeded but whose user-side
            # push raised must be visible in the LOGS, not only stored on the
            # run row. Previously the delivery_error was promoted to
            # agent_error and traced, but never logged — so operators saw
            # "推送失败" only by drilling into cron_job_runs. Log it loudly.
            logger.error(
                "Cron delivery failed job=%s kind=%s run_id=%s delivery_error=%s",
                job_id,
                task_kind,
                cron_job_run_id,
                result.delivery_error,
            )
            if not agent_error_for_row:
                agent_error_for_row = (
                    f"delivery_failed: {result.delivery_error or 'unknown'}"
                )

        if self._run_repo is not None:
            await self._run_repo.update_run(cron_job_run_id, {
                "status": terminal_status,
                "finished_at": datetime.now(timezone.utc),
                "agent_session_id": result.agent_session_id,
                "agent_error": agent_error_for_row,
                "cron_task_kind": task_kind,
                "delivery_status": result.delivery_status,
                # Re-use the pre_* columns to surface the executor's
                # downstream artifacts: ``pre_run_id`` carries the
                # cycle_run / strategy run id, ``pre_debug_session_id``
                # the debug session, ``pre_result_json`` the executor's
                # data payload. This keeps the debug UI's existing
                # column map working without a new schema.
                "pre_run_id": result.run_id,
                "pre_debug_session_id": result.debug_session_id,
                "pre_result_json": result.data,
            })

        await self._repo.update_job_state(
            job_id,
            last_status=terminal_status,
            last_run_session_id=result.agent_session_id,
            last_error=agent_error_for_row,
        )
        # A failed fire (terminal_status != success) is logged at ERROR so it
        # is not buried among routine info-level completions; successful fires
        # stay at info.
        _completion_log = (
            logger.info if terminal_status == "success" else logger.error
        )
        _completion_log(
            "Cron job %s task=%s completed status=%s delivery=%s "
            "agent_session_id=%s",
            job_id, task_kind, terminal_status, result.delivery_status,
            result.agent_session_id,
        )
        await self._post_fire_cleanup(
            job, fired_at, fire_span=fire_span,
        )
