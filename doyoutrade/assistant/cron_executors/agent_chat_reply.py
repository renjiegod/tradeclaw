"""``agent_chat_reply`` cron task executor.

Use case: user asks the agent "提醒我 X 分钟后做 Y" / "每天 9 点跟我说早安".
At fire time we want the agent to *compose a message to push to the user*,
not to receive the user's original phrase a second time as if it were a new
user turn. This executor:

  1. Renders ``cron_framing.j2`` with the original user request + delivery
     instructions, so the LLM knows it is responding inside a scheduled
     fire and that its reply will be auto-pushed.
  2. Creates a fresh assistant session (titled ``[Cron] <job.name>``) so
     repeated fires never pollute each other's history.
  3. Calls ``AssistantService.send_message`` once — the framing is the
     user-role content of that single turn.
  4. Extracts the LLM's final assistant text and hands it to
     :func:`deliver_assistant_message_to_session`, which appends it as
     ``role=assistant`` (with ``metadata.source=cron``) on the user's
     real session, triggering the frontend push.

Params (validated by :meth:`validate_params`):

  - ``user_request`` (str, required) — verbatim user phrase that motivated
    the schedule; preserved end-to-end so trace consumers can attribute
    the push back to the original intent.
  - ``target_session_id`` (str | None) — assistant session to push the
    reply into. Null is allowed (executor returns
    ``delivery_status='skipped'``) so a job can be created for diagnostic
    fires before the user session is known.
  - ``agent_id`` (str, required) — the agent that does the composing;
    normally identical to the job's owning agent.
"""

from __future__ import annotations

from typing import Any, ClassVar

from doyoutrade.assistant.prompt_templates import render_cron_framing
from doyoutrade.observability import get_logger, get_tracer

from ._deliver import deliver_assistant_message_to_session
from .base import JobRunContext, TaskResult

logger = get_logger(__name__)
tracer = get_tracer(__name__)


KIND = "agent_chat_reply"


class AgentChatReplyExecutor:
    """Task executor for the ``agent_chat_reply`` kind."""

    kind: ClassVar[str] = KIND

    def __init__(self, *, assistant_service: Any, cron_job_repository: Any):
        self._svc = assistant_service
        # We need the job name for framing + the session title. cron_manager
        # already fetches the row, but threading it in via ctx would couple
        # the protocol to internal call shapes — the repo lookup is one
        # cached query per fire and keeps the protocol clean.
        self._cron_repo = cron_job_repository

    # --- contract validation ----------------------------------------------

    def validate_params(self, params: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(params, dict):
            return {
                "error_code": "invalid_task_params",
                "error": "params must be an object",
            }
        user_request = params.get("user_request")
        if not isinstance(user_request, str) or not user_request.strip():
            return {
                "error_code": "missing_user_request",
                "error": "agent_chat_reply.params.user_request is required",
                "field": "user_request",
            }
        agent_id = params.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id.strip():
            return {
                "error_code": "missing_agent_id",
                "error": "agent_chat_reply.params.agent_id is required",
                "field": "agent_id",
            }
        target = params.get("target_session_id")
        if target is not None and (not isinstance(target, str) or not target.strip()):
            return {
                "error_code": "invalid_target_session_id",
                "error": "target_session_id must be a string or null",
                "field": "target_session_id",
            }
        return None

    # --- runtime ----------------------------------------------------------

    async def run(self, params: dict[str, Any], ctx: JobRunContext) -> TaskResult:
        with tracer.start_as_current_span("cron.task.run") as span:
            span.set_attribute("cron.task.kind", self.kind)
            span.set_attribute("cron.job_id", ctx.job_id)
            span.set_attribute("cron.job_run_id", ctx.cron_job_run_id)

            user_request = str(params.get("user_request") or "").strip()
            agent_id = str(params.get("agent_id") or "").strip()
            target_session_id = params.get("target_session_id")
            if target_session_id is not None:
                target_session_id = str(target_session_id).strip() or None

            job = await self._cron_repo.get_job(ctx.job_id)
            if not job:
                # Defensive: cron_manager already checks this, but the
                # registry dispatch makes it cheap to verify again.
                err = f"cron job not found: {ctx.job_id}"
                span.set_attribute("cron.task.status", "failed")
                return TaskResult(status="failed", error=err)

            framing = render_cron_framing(
                job={"id": job["id"], "name": job["name"]},
                task_kind=self.kind,
                fired_at=ctx.fired_at.isoformat(),
                user_request=user_request,
                target_session_id=target_session_id,
                pre_data=None,
            )

            agent_session_id: str | None = None
            reply_text: str = ""
            try:
                session = await self._svc.create_session(
                    agent_id=agent_id,
                    title=f"[Cron] {job['name']}",
                )
                agent_session_id = session["session_id"]
                span.set_attribute("cron.agent_session_id", agent_session_id)
                result = await self._svc.send_message(
                    session_id=agent_session_id,
                    content=framing,
                )
                # send_message returns {"messages": [user_message, assistant_message], ...}
                messages = result.get("messages") if isinstance(result, dict) else None
                if isinstance(messages, list) and messages:
                    assistant_msg = messages[-1]
                    if isinstance(assistant_msg, dict):
                        reply_text = str(assistant_msg.get("content") or "").strip()
            except Exception as exc:
                logger.exception(
                    "agent_chat_reply LLM call failed job_id=%s run_id=%s",
                    ctx.job_id, ctx.cron_job_run_id,
                )
                span.set_attribute("cron.task.status", "failed")
                span.set_attribute("cron.task.error", f"{type(exc).__name__}: {exc}")
                return TaskResult(
                    status="failed",
                    agent_session_id=agent_session_id,
                    error=f"agent_invocation_failed: {type(exc).__name__}: {exc}",
                )

            if not reply_text:
                # The LLM produced an empty answer (rare but possible).
                # Treat as suppressed delivery rather than failure: there is
                # nothing meaningful to push.
                span.set_attribute("cron.task.status", "ok")
                span.set_attribute("cron.delivery.status", "suppressed")
                return TaskResult(
                    status="ok",
                    agent_session_id=agent_session_id,
                    delivery_status="suppressed",
                    data={"reason": "empty_reply"},
                )

            delivery_status, delivery_info = await deliver_assistant_message_to_session(
                self._svc,
                target_session_id=target_session_id,
                content=reply_text,
                cron_job_id=ctx.job_id,
                cron_job_run_id=ctx.cron_job_run_id,
                cron_task_kind=self.kind,
            )
            span.set_attribute("cron.delivery.status", delivery_status)
            span.set_attribute("cron.task.status", "ok")
            delivery_error: str | None = None
            if delivery_status == "failed" and isinstance(delivery_info, dict):
                delivery_error = str(delivery_info.get("error") or "")
            return TaskResult(
                status="ok",
                agent_session_id=agent_session_id,
                delivery_status=delivery_status,
                delivery_error=delivery_error,
                data={"target_session_id": target_session_id},
            )
