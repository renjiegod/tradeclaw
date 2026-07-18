"""Blocking ask_user_question waits (approval-gate future pattern).

``ask_user_question`` used to be non-blocking: the tool stored the pending
question in ``session.config`` and ended the turn; the user's answer arrived
as the NEXT user message via the ``/ask_user <id> <answer>`` protocol, which
the service parsed and rewrote into a synthetic user bubble.

This module replaces that with the same future-broker skeleton the approval
gate uses (:mod:`doyoutrade.assistant.approvals`): when the model calls
``ask_user_question``, the dispatch loop publishes the card and then suspends
the tool *inside its execution slot* on a pending ``asyncio.Future``. A card
click (web / Feishu) or a free-typed reply resolves the future through
:class:`QuestionBroker.resolve`; the answer is fed back as this tool call's
``tool_result`` and the SAME run continues — no synthetic user message, no
new attempt. This mirrors CopilotKit/AG-UI HITL semantics (tool_call ↔
tool_result paired by call id).

Like approvals, pending questions are in-memory: the suspended turn dies with
the process, so persisted rows would be orphans. Audit happens through the
persisted ``user_question.*`` session events.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from doyoutrade.observability import get_logger

logger = get_logger(__name__)

# Users need longer to answer a question than an operator needs to approve a
# known command, but an in-memory suspended turn should not hang forever.
DEFAULT_QUESTION_TIMEOUT_SECONDS = 900.0


@dataclass(frozen=True)
class QuestionResolution:
    """The outcome of a pending question.

    ``selected`` are the chosen option labels (one for single-select, many for
    multi-select, empty when the user answered only with free text). ``custom``
    is free-form text the user typed instead of / in addition to picking
    options. ``source`` records which surface answered.
    """

    selected: tuple[str, ...] = ()
    custom: str = ""
    source: str = ""  # "option_click" | "free_text" | "timeout"
    resolver_id: str = ""
    timed_out: bool = False

    def is_empty(self) -> bool:
        return not self.selected and not self.custom.strip()


@dataclass
class QuestionRequest:
    """A pending ask_user_question — a suspended tool waiting for an answer."""

    question_id: str
    session_id: str
    attempt_id: str
    run_id: str
    question: str
    header: str | None
    options: tuple[dict[str, Any], ...]
    multi_select: bool
    timeout_seconds: float
    created_at: str
    _future: asyncio.Future[QuestionResolution] = field(repr=False, default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._future is None:
            self._future = asyncio.get_event_loop().create_future()

    async def wait(self) -> QuestionResolution:
        """Suspend until answered; a timeout resolves to ``timed_out=True``."""
        try:
            return await asyncio.wait_for(
                asyncio.shield(self._future), timeout=self.timeout_seconds
            )
        except asyncio.TimeoutError:
            resolution = QuestionResolution(source="timeout", timed_out=True)
            if not self._future.done():
                self._future.set_result(resolution)
            return self._future.result()

    def resolve(self, resolution: QuestionResolution) -> bool:
        """Complete the future; False when already resolved / timed out."""
        if self._future.done():
            return False
        self._future.set_result(resolution)
        return True

    def payload(self) -> dict[str, Any]:
        """Serializable view for events / cards / SSE / pending listing."""
        return {
            "question_id": self.question_id,
            "session_id": self.session_id,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "question": self.question,
            "header": self.header,
            "options": [dict(option) for option in self.options],
            "multi_select": self.multi_select,
            "timeout_seconds": self.timeout_seconds,
            "created_at": self.created_at,
        }


class QuestionBroker:
    """In-memory registry of pending ask_user_question waits for one process.

    Pending questions do not survive a restart by design (see module docstring).
    Audit happens through persisted ``user_question.*`` session events.
    """

    def __init__(self) -> None:
        self._pending: dict[str, QuestionRequest] = {}

    def create(
        self,
        *,
        question_id: str,
        session_id: str,
        attempt_id: str,
        run_id: str,
        question: str,
        header: str | None,
        options: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        multi_select: bool,
        timeout_seconds: float = DEFAULT_QUESTION_TIMEOUT_SECONDS,
    ) -> QuestionRequest:
        request = QuestionRequest(
            question_id=question_id,
            session_id=session_id,
            attempt_id=attempt_id,
            run_id=run_id,
            question=question,
            header=header,
            options=tuple(dict(option) for option in options),
            multi_select=multi_select,
            timeout_seconds=timeout_seconds,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._pending[question_id] = request
        return request

    def get(self, question_id: str) -> QuestionRequest | None:
        return self._pending.get(question_id)

    def list_pending(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [
            request.payload()
            for request in self._pending.values()
            if not request._future.done()
            and (session_id is None or request.session_id == session_id)
        ]

    def resolve(
        self,
        question_id: str,
        *,
        selected: list[str] | tuple[str, ...] = (),
        custom: str = "",
        source: str,
        resolver_id: str = "",
    ) -> bool:
        """Resolve a pending question. False = unknown / already resolved.

        Callers (answer endpoint, card handler, free-text reply) must surface a
        False return to the user — the question may have timed out or been
        answered on another surface.
        """
        request = self._pending.get(question_id)
        if request is None:
            logger.info(
                "user_question resolve ignored (unknown id) question_id=%s", question_id
            )
            return False
        accepted = request.resolve(
            QuestionResolution(
                selected=tuple(selected),
                custom=custom,
                source=source,
                resolver_id=resolver_id,
            )
        )
        if not accepted:
            logger.info(
                "user_question resolve ignored (already resolved) question_id=%s source=%s",
                question_id,
                source,
            )
        return accepted

    def discard(self, question_id: str) -> None:
        self._pending.pop(question_id, None)
