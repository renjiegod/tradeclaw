"""HTTP Channel — for debugging and manual message injection."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from doyoutrade.assistant.channels.base import (
    BaseChannel,
    ChannelAgentRequest,
    ContentPart,
)

if TYPE_CHECKING:
    from doyoutrade.assistant.service import AssistantService
    from doyoutrade.assistant.channels.config import HttpChannelConfig


class HttpChannel(BaseChannel):
    """HTTP Channel — for debugging / manual triggering.

    Messages are handled directly by FastAPI route handlers calling
    AssistantService.send_message(). This channel's send() is never invoked.
    """

    channel_type = "http"

    @classmethod
    def from_config(
        cls,
        assistant_service: "AssistantService",
        config: "HttpChannelConfig",
    ) -> "HttpChannel":
        return cls(assistant_service, channel_id=getattr(config, "channel_id", None))

    def __init__(
        self,
        assistant_service: "AssistantService | None" = None,
        *,
        channel_id: str | None = None,
    ):
        super().__init__(assistant_service, channel_id=channel_id)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def build_agent_request_from_native(
        self, native_payload: Any
    ) -> ChannelAgentRequest:
        if isinstance(native_payload, dict):
            return ChannelAgentRequest(
                session_id=native_payload.get("session_id", ""),
                content=native_payload.get("content", ""),
                sender_id=native_payload.get("sender_id", ""),
                channel_meta=native_payload.get("meta", {}),
            )
        return ChannelAgentRequest(session_id="", content=str(native_payload))

    async def send(
        self,
        session_id: str,
        content: ContentPart,
        meta: dict[str, Any],
    ):
        raise NotImplementedError(
            "HttpChannel.send() should not be called — "
            "use AssistantService.send_message() directly in API routes"
        )
