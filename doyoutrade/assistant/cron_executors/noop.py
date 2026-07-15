from __future__ import annotations

from typing import Any, ClassVar

from .base import JobRunContext, PreActionResult


class NoopExecutor:
    """No-op pre-action.

    Used when a cron job has no deterministic prep work — equivalent to the
    pre-Task-3 assistant-only cron behaviour. Returns ``status="ok"`` with no
    ``data`` and never raises.
    """

    kind: ClassVar[str] = "noop"

    async def execute(
        self,
        params: dict[str, Any],
        ctx: JobRunContext,
    ) -> PreActionResult:
        return PreActionResult(status="ok")
