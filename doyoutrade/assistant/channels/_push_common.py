"""Shared scaffolding for outbound-only push channels.

Email / WeCom / DingTalk / Telegram / Slack all share the same shape: they are
outbound (cron pushes + assistant replies forwarded to them) and do not run an
inbound receive loop. This module factors out that common surface so each
channel only implements :meth:`send`.

Error-visibility: HTTP helpers never swallow — a non-2xx or transport error is
returned as a structured ``(ok, status, detail)`` tuple; callers log a warning
with the channel type + status and raise ``ChannelSendError`` so ``_deliver``
records ``forward_failed`` on the span. Nothing here does ``except: pass``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from doyoutrade.assistant.channels.base import (
    BaseChannel,
    ChannelAgentRequest,
)

if TYPE_CHECKING:
    from doyoutrade.assistant.service import AssistantService

logger = logging.getLogger(__name__)


class ChannelSendError(RuntimeError):
    """Raised when an outbound channel fails to deliver a message.

    Carries the channel type and a stable ``reason`` so ``_deliver`` /
    telemetry can distinguish failure modes rather than parsing free text.
    """

    def __init__(self, channel_type: str, reason: str, message: str):
        self.channel_type = channel_type
        self.reason = reason
        super().__init__(f"[{channel_type}] {reason}: {message}")


async def http_post(
    url: str,
    *,
    json: dict[str, Any] | None = None,
    data: Any = None,
    files: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> tuple[bool, int, str]:
    """POST via httpx.AsyncClient. Returns ``(ok, status_code, response_text)``.

    ``ok`` is True only for a 2xx response. Transport errors return
    ``(False, 0, "<ExcType>: <msg>")`` — never raised here so the caller decides
    how to surface them (they always do, loudly).
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx is a core dep
        return False, 0, f"httpx missing: {exc}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                url, json=json, data=data, files=files, headers=headers
            )
    except Exception as exc:  # noqa: BLE001 - transport error, reported structurally
        return False, 0, f"{type(exc).__name__}: {exc}"

    return (200 <= resp.status_code < 300), resp.status_code, resp.text[:1000]


class OutboundPushChannel(BaseChannel):
    """Base for outbound-only channels. Subclasses implement :meth:`send`.

    ``start``/``stop`` are no-ops (no inbound loop). Inbound conversion is not
    supported and raises with a clear message rather than silently returning an
    empty request.
    """

    def __init__(
        self,
        assistant_service: "AssistantService | None" = None,
        *,
        channel_id: str | None = None,
    ):
        super().__init__(assistant_service, channel_id=channel_id)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def build_agent_request_from_native(self, native_payload: Any) -> ChannelAgentRequest:
        raise NotImplementedError(
            f"{self.channel_type} is an outbound-only push channel; "
            "it does not accept inbound messages"
        )
