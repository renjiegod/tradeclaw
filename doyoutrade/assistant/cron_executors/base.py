from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal, Protocol


@dataclass(frozen=True)
class JobRunContext:
    """Context passed to a cron executor for a single fire."""

    cron_job_run_id: str
    job_id: str
    fired_at: datetime


@dataclass
class PreActionResult:
    """Outcome of one pre-action execution (legacy two-stage pipeline).

    ``data`` is the executor-defined structured payload (also written to
    ``cron_job_runs.pre_result_json``); each executor documents its own shape.
    """

    status: Literal["ok", "error"]
    run_id: str | None = None
    debug_session_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class JobPreActionExecutor(Protocol):
    """Legacy protocol: pre-action executors that produce data consumed by the
    cron_manager's hard-coded "render template + send_message" tail.

    Kept for backward compatibility with rows persisted before the
    ``task_kind``/``task_params_json`` schema landed. New cron logic should
    register :class:`JobTaskExecutor` against :class:`JobTaskRegistry`.
    """

    kind: ClassVar[str]

    async def execute(
        self,
        params: dict[str, Any],
        ctx: JobRunContext,
    ) -> PreActionResult: ...


class JobExecutorRegistry:
    """Legacy in-process registry for :class:`JobPreActionExecutor`."""

    def __init__(self) -> None:
        self._by_kind: dict[str, JobPreActionExecutor] = {}

    def register(self, executor: JobPreActionExecutor) -> None:
        self._by_kind[executor.kind] = executor

    def get(self, kind: str) -> JobPreActionExecutor | None:
        return self._by_kind.get(kind)

    def known_kinds(self) -> list[str]:
        return sorted(self._by_kind.keys())


# ── Task executors (current pipeline) ────────────────────────────────────────


@dataclass
class TaskResult:
    """Outcome of one whole cron task execution.

    Each task executor owns its own pipeline (data gathering + optional LLM
    invocation + delivery), so the result needs to cover all three concerns.

    Fields:
      - ``status``: terminal status of the task itself.
          * ``ok``       — task ran end-to-end (delivery may have been
                           suppressed; see ``delivery_status``).
          * ``failed``   — executor raised or returned an error; ``error``
                           is populated.
      - ``run_id``: trace/run id surfaced from any downstream artifact the
        executor created (e.g. the strategy ``cycle_run`` id, or the
        assistant LLM session id). Persisted on ``cron_job_runs.agent_session_id``
        / ``cron_job_runs.pre_run_id`` depending on which is meaningful for
        the kind.
      - ``debug_session_id``: id of a downstream debug session, if any.
      - ``agent_session_id``: id of the assistant session created by the
        executor when it invoked an LLM (cron_manager writes this to
        ``cron_job_runs.agent_session_id`` for the existing UI).
      - ``delivery_status``: outcome of the user-facing push.
          * ``delivered`` — assistant reply appended to target session.
          * ``suppressed`` — executor produced ``[SILENT]``; nothing pushed.
          * ``skipped``   — no target_session_id configured.
          * ``failed``    — push attempt raised.
          * ``none``      — task has no delivery step (e.g. dry-run kinds).
      - ``data``: executor-defined JSON payload, written to
        ``cron_job_runs.pre_result_json`` for inspection.
      - ``error``: human-readable error string when ``status == "failed"``.
    """

    status: Literal["ok", "failed"]
    run_id: str | None = None
    debug_session_id: str | None = None
    agent_session_id: str | None = None
    delivery_status: Literal[
        "delivered", "suppressed", "skipped", "failed", "none"
    ] = "none"
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # Populated when ``delivery_status == "failed"``. Carried separately
    # from ``error`` (which signals an executor-level failure) so a fire
    # whose LLM call succeeded but whose user-side push raised stays
    # ``status="ok"`` while still surfacing the delivery failure to the
    # cron run row + UI Error column.
    delivery_error: str | None = None


class JobTaskExecutor(Protocol):
    """Protocol for whole-job cron task executors.

    Each executor owns one ``task.kind`` and implements the full fire-time
    pipeline (gather data → optionally invoke agent → deliver). The cron
    manager dispatches by ``kind`` and persists the :class:`TaskResult`.

    Implementations must also expose ``validate_params(params)`` so the
    ``create_cron_job`` tool can reject bad payloads at write time rather
    than at first fire.
    """

    kind: ClassVar[str]

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """Return ``None`` if ``params`` is acceptable, otherwise an error
        dict shaped like ``{"error_code": ..., "error": ..., "field": ...}``.

        Called from the assistant tool before persistence. Executors should
        only validate structural shape here; semantic checks that require
        DB lookups (e.g. instance existence) belong in :meth:`run`."""
        ...

    async def run(
        self,
        params: dict[str, Any],
        ctx: JobRunContext,
    ) -> TaskResult: ...


class JobTaskRegistry:
    """In-process registry mapping ``task.kind`` → :class:`JobTaskExecutor`."""

    def __init__(self) -> None:
        self._by_kind: dict[str, JobTaskExecutor] = {}

    def register(self, executor: JobTaskExecutor) -> None:
        self._by_kind[executor.kind] = executor

    def get(self, kind: str) -> JobTaskExecutor | None:
        return self._by_kind.get(kind)

    def known_kinds(self) -> list[str]:
        return sorted(self._by_kind.keys())
