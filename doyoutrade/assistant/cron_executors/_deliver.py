"""Shared delivery helper for cron task executors.

Centralises how a cron-generated reply lands on the user's session so every
``JobTaskExecutor`` produces the same metadata shape on the persisted
assistant message. The frontend uses ``metadata.source == "cron"`` plus
``metadata.cron_job_id`` / ``cron_job_run_id`` to render the message as a
push and link it back to the originating job/run.

When the target session was originally driven by an external channel
(Lark / HTTP / etc.), the helper also forwards the persisted message to
the running channel via ``BaseChannel.send`` so the user actually sees the
push on their platform instead of only on the web UI. The forward is
best-effort and does not change the ``DeliveryStatus`` — the message is
already persisted in the session log either way — but its outcome is
surfaced via the ``channel_forward_status`` field on ``info`` and on a
``cron.delivery.channel_forward`` child span so the failure mode is
visible from logs, trace, and the cron_job_runs row.
"""

from __future__ import annotations

import traceback
from typing import Any, Literal

from opentelemetry.trace import Status, StatusCode

from doyoutrade.observability import get_logger, get_tracer

logger = get_logger(__name__)
tracer = get_tracer(__name__)


SILENT_SENTINEL = "[SILENT]"
_SILENT_SENTINEL_VARIANTS = frozenset({
    SILENT_SENTINEL,
    "[SILENT",
})


DeliveryStatus = Literal["delivered", "suppressed", "skipped", "failed"]

# Possible values for ``info['channel_forward_status']`` emitted alongside a
# ``delivered`` status. Kept as a Literal so trace consumers / tests can
# enumerate the failure modes — per CLAUDE.md §错误可见性, channel-forward
# outcomes must be distinguishable by structured field, not free text.
ChannelForwardStatus = Literal[
    "forwarded",            # channel.send succeeded
    "no_channel_binding",   # session has no config.channel — nothing to push
    "channel_disabled",     # channel binding exists but the live channel is
                            # not registered with the running ChannelManager
                            # (e.g. disabled, stopped, or never started)
    "forward_failed",       # channel.send raised; message still persisted
]


def is_silent_reply(content: str | None) -> bool:
    """Return True when the LLM signalled "nothing to push this fire"."""

    if not isinstance(content, str):
        return False
    normalized = content.strip()
    # Some model routes have been observed to emit the cron sentinel
    # without the closing bracket even when the prompt requests the exact
    # token. Treat that malformed variant as equivalent so the user
    # session doesn't get spammed with literal ``[SILENT`` rows.
    return normalized in _SILENT_SENTINEL_VARIANTS


def _format_exception(exc: BaseException) -> str:
    """Compact ``Type: message\\n<tail>`` blob for delivery_error / span attr."""

    tail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-2000:]
    return f"{type(exc).__name__}: {exc}\n{tail}"


def _resolve_live_channel(assistant_service: Any, channel_id: str) -> Any | None:
    """Return the running ``BaseChannel`` instance for ``channel_id``, or None.

    Channels are wired into a ``ChannelManager`` at bootstrap (see
    ``doyoutrade/bootstrap.py``); the manager owns the live ``BaseChannel``
    instance per ``channel_id`` and exposes ``.get(channel_id)``. The manager
    is attached to ``assistant_service`` as ``assistant_service.channel_manager``
    so cron executors can resolve channels without threading the manager
    through every executor constructor.

    Returning ``None`` (instead of raising) is intentional: a missing
    channel binding or a stopped channel maps to a structured
    ``channel_forward_status`` rather than an exception, because cron
    delivery must remain best-effort once the message is persisted.
    """

    manager = getattr(assistant_service, "channel_manager", None)
    if manager is None:
        return None
    getter = getattr(manager, "get", None)
    if not callable(getter):
        return None
    return getter(channel_id)


async def deliver_assistant_message_to_session(
    assistant_service: Any,
    *,
    target_session_id: str | None,
    content: str,
    cron_job_id: str,
    cron_job_run_id: str,
    cron_task_kind: str,
    extra_metadata: dict[str, Any] | None = None,
    source: str = "cron",
) -> tuple[DeliveryStatus, dict[str, Any] | None]:
    """Append a cron-generated reply as ``role=assistant`` to a user session.

    Returns ``(status, info)`` where ``info`` is either the persisted message
    row (on ``delivered``) or a ``{"error": <text>}`` dict (on ``failed``);
    callers must propagate that error text into ``TaskResult.delivery_error``
    so the cron_manager can land it on the run row's ``agent_error`` column.

    When ``status == "delivered"``, ``info`` additionally carries
    ``channel_forward_status`` (one of :data:`ChannelForwardStatus`) and
    ``channel_forward_error`` (string or ``None``). The forward outcome
    never changes the ``DeliveryStatus`` literal — channel forwarding is
    orthogonal to message persistence — but it is visible on the
    ``cron.delivery.channel_forward`` child span and the structured log.

    Statuses:

      - ``delivered`` — message persisted on ``target_session_id``.
      - ``suppressed`` — content was the ``[SILENT]`` sentinel; no write.
      - ``skipped``   — no ``target_session_id`` configured for this job.
      - ``failed``    — repository write raised. The exception is logged,
                        recorded on the surrounding ``cron.delivery`` span,
                        AND returned to the caller in the ``info`` dict so
                        the failure stays visible from three independent
                        surfaces (logs / trace / cron_job_runs row).
    """

    # One ``cron.delivery`` span per call so the TraceViewer surfaces the
    # post-LLM push as its own node. Without this the executor's
    # ``cron.task.run`` span stays green when only the delivery fails — the
    # exception is caught and converted to a return value before the
    # executor's span sees it.
    with tracer.start_as_current_span("cron.delivery") as span:
        span.set_attribute("cron.job_id", cron_job_id)
        span.set_attribute("cron.job_run_id", cron_job_run_id)
        span.set_attribute("cron.task.kind", cron_task_kind)
        if target_session_id:
            span.set_attribute("cron.delivery.target_session_id", target_session_id)
        span.set_attribute("cron.delivery.content_length", len(content) if isinstance(content, str) else 0)

        if is_silent_reply(content):
            span.set_attribute("cron.delivery.status", "suppressed")
            logger.info(
                "cron delivery suppressed by [SILENT] sentinel job_id=%s run_id=%s",
                cron_job_id, cron_job_run_id,
            )
            return "suppressed", None

        if not target_session_id:
            span.set_attribute("cron.delivery.status", "skipped")
            span.set_attribute("cron.delivery.skip_reason", "no_target_session_id")
            logger.info(
                "cron delivery skipped (no target_session_id) job_id=%s run_id=%s",
                cron_job_id, cron_job_run_id,
            )
            return "skipped", None

        repo = getattr(assistant_service, "repository", None)
        if repo is None:
            err = "assistant_service has no `repository` attribute"
            span.set_attribute("cron.delivery.status", "failed")
            span.set_attribute("cron.delivery.error_type", "ConfigurationError")
            span.set_attribute("cron.delivery.error_message", err)
            span.set_status(Status(StatusCode.ERROR, err))
            logger.error(
                "cron delivery failed: %s job_id=%s run_id=%s",
                err, cron_job_id, cron_job_run_id,
            )
            return "failed", {"error": err}

        metadata: dict[str, Any] = {
            "source": source,
            "cron_job_id": cron_job_id,
            "cron_job_run_id": cron_job_run_id,
            "cron_task_kind": cron_task_kind,
        }
        if extra_metadata:
            metadata.update(extra_metadata)

        try:
            message = await repo.append_message(
                session_id=target_session_id,
                role="assistant",
                content=content,
                linked_attempt_id=None,
                metadata=metadata,
            )
        except Exception as exc:
            error_text = _format_exception(exc)
            span.set_attribute("cron.delivery.status", "failed")
            span.set_attribute("cron.delivery.error_type", type(exc).__name__)
            # Truncate the message attribute — the full traceback already
            # lives on the span via ``record_exception`` below.
            span.set_attribute(
                "cron.delivery.error_message",
                f"{type(exc).__name__}: {exc}"[:500],
            )
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, f"{type(exc).__name__}: {exc}"))
            logger.exception(
                "cron delivery failed appending message session_id=%s "
                "job_id=%s run_id=%s",
                target_session_id, cron_job_id, cron_job_run_id,
            )
            return "failed", {"error": error_text}

        # Best-effort frontend notification: emit an event on the session so the
        # web UI can refresh without waiting on long-poll cadence. A failure here
        # does not flip the delivery to "failed" — the message is already
        # persisted. The exception path is logged but does not propagate.
        try:
            await repo.append_event(
                session_id=target_session_id,
                event_type="cron.message.pushed",
                payload={
                    "cron_job_id": cron_job_id,
                    "cron_job_run_id": cron_job_run_id,
                    "cron_task_kind": cron_task_kind,
                    "message_id": message.get("message_id") if isinstance(message, dict) else None,
                },
            )
        except Exception:
            logger.exception(
                "cron delivery event append failed (message still persisted) "
                "session_id=%s job_id=%s run_id=%s",
                target_session_id, cron_job_id, cron_job_run_id,
            )

        # ── Channel forward (best-effort) ───────────────────────────────────
        # If the session was originally driven by an external channel,
        # forward the persisted message to that channel so the user actually
        # sees the push on Lark / HTTP / etc. — not only on the web UI.
        # Failure here is logged + traced but does NOT flip the delivery
        # status to "failed": the message is already on the session log,
        # and the four ChannelForwardStatus values stay distinguishable from
        # structured fields per CLAUDE.md §错误可见性.
        channel_forward_status: ChannelForwardStatus
        channel_forward_error: str | None = None
        channel_block: dict[str, Any] | None = None
        try:
            session = await assistant_service.get_session(target_session_id)
        except Exception as exc:
            # Looking up the session for channel resolution failed. The
            # message is already persisted, so treat this exactly like a
            # missing binding from the forward's perspective — but log
            # loudly with the exception type and target_session_id so
            # operators can spot the lookup failure independently.
            logger.exception(
                "cron delivery channel lookup failed (session get raised) "
                "session_id=%s job_id=%s run_id=%s",
                target_session_id, cron_job_id, cron_job_run_id,
            )
            session = None

        if isinstance(session, dict):
            raw_channel = (session.get("config") or {}).get("channel")
            if isinstance(raw_channel, dict):
                channel_block = raw_channel

        channel_id: str = ""
        channel_type: str = ""
        if channel_block:
            channel_id = str(channel_block.get("channel_id") or "").strip()
            channel_type = str(channel_block.get("channel_type") or "").strip()

        with tracer.start_as_current_span("cron.delivery.channel_forward") as fwd_span:
            if channel_id:
                fwd_span.set_attribute("cron.channel.id", channel_id)
            if channel_type:
                fwd_span.set_attribute("cron.channel.type", channel_type)

            if not channel_id:
                channel_forward_status = "no_channel_binding"
                fwd_span.set_attribute(
                    "cron.channel.forward_status", channel_forward_status,
                )
                logger.info(
                    "cron delivery channel forward skipped reason=no_channel_binding "
                    "session_id=%s job_id=%s run_id=%s",
                    target_session_id, cron_job_id, cron_job_run_id,
                )
            else:
                channel = _resolve_live_channel(assistant_service, channel_id)
                if channel is None:
                    # The session is bound to a channel that the running
                    # ChannelManager does not know about — typically the
                    # channel is disabled, stopped, or this process never
                    # started the channel set. Surface as a distinct
                    # status (not "forward_failed") so the operator can
                    # tell "wiring problem" from "send raised".
                    channel_forward_status = "channel_disabled"
                    fwd_span.set_attribute(
                        "cron.channel.forward_status", channel_forward_status,
                    )
                    logger.info(
                        "cron delivery channel forward skipped reason=channel_disabled "
                        "channel_id=%s session_id=%s job_id=%s run_id=%s",
                        channel_id, target_session_id, cron_job_id, cron_job_run_id,
                    )
                else:
                    # Build the TextContent + carry-forward the session's
                    # channel meta (chat_id / open_id / etc.) so the
                    # channel can route to the same conversation the
                    # original message came from. We deliberately do not
                    # mutate ``channel_block.meta`` in place.
                    from doyoutrade.assistant.channels.base import CardContent, TextContent

                    raw_meta = channel_block.get("meta") if channel_block else None
                    send_meta: dict[str, Any] = (
                        dict(raw_meta) if isinstance(raw_meta, dict) else {}
                    )
                    # Channels with native interactive-card support (Feishu) get a
                    # rendered card so cron pushes look like first-class messages,
                    # not raw text. Other channels fall back to plain text.
                    outgoing: Any = TextContent(text=content)
                    if getattr(channel, "channel_type", "") == "feishu":
                        try:
                            from doyoutrade.assistant.channels.feishu.card.builder import (
                                build_complete_card,
                            )

                            outgoing = CardContent(
                                card=build_complete_card(content, show_tool_use=False)
                            )
                        except Exception as card_exc:  # noqa: BLE001
                            # Card build is best-effort; fall back to text but make
                            # the downgrade visible rather than silently swallowing.
                            logger.warning(
                                "cron delivery card build failed channel_id=%s session_id=%s "
                                "job_id=%s err=%s: %s; falling back to text",
                                channel_id, target_session_id, cron_job_id,
                                type(card_exc).__name__, card_exc,
                            )
                    try:
                        await channel.send(
                            target_session_id,
                            outgoing,
                            send_meta,
                        )
                    except Exception as exc:
                        channel_forward_status = "forward_failed"
                        channel_forward_error = f"{type(exc).__name__}: {exc}"
                        fwd_span.set_attribute(
                            "cron.channel.forward_status", channel_forward_status,
                        )
                        fwd_span.set_attribute(
                            "cron.channel.error_type", type(exc).__name__,
                        )
                        fwd_span.set_attribute(
                            "cron.channel.error_message",
                            channel_forward_error[:500],
                        )
                        fwd_span.record_exception(exc)
                        fwd_span.set_status(
                            Status(
                                StatusCode.ERROR,
                                channel_forward_error,
                            )
                        )
                        # ``logger.exception`` here, not ``logger.warning``:
                        # the message is persisted but the user will not
                        # see it on their channel until this is fixed —
                        # operators should treat this as a real incident.
                        logger.exception(
                            "cron delivery channel forward failed channel_id=%s "
                            "channel_type=%s session_id=%s job_id=%s run_id=%s",
                            channel_id, channel_type, target_session_id,
                            cron_job_id, cron_job_run_id,
                        )
                    else:
                        channel_forward_status = "forwarded"
                        fwd_span.set_attribute(
                            "cron.channel.forward_status", channel_forward_status,
                        )
                        logger.info(
                            "cron delivery channel forward ok channel_id=%s "
                            "channel_type=%s session_id=%s job_id=%s run_id=%s",
                            channel_id, channel_type, target_session_id,
                            cron_job_id, cron_job_run_id,
                        )

        span.set_attribute("cron.delivery.status", "delivered")
        span.set_attribute(
            "cron.delivery.channel_forward_status", channel_forward_status,
        )

        # Build the info dict. ``message`` from ``append_message`` is a
        # dict in production; we extend it (without mutating the original
        # row) so callers see both the persisted row and the forward
        # outcome from a single return value.
        info: dict[str, Any]
        if isinstance(message, dict):
            info = dict(message)
        else:
            # Defensive: ``append_message`` should return a dict. If it
            # ever returns something else, surface that fact instead of
            # silently coercing — operators need to see schema drift.
            info = {"message": message}
        info["channel_forward_status"] = channel_forward_status
        info["channel_forward_error"] = channel_forward_error

        return "delivered", info
