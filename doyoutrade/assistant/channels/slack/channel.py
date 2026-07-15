"""Slack push channel — outbound only (incoming webhook or bot token)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from doyoutrade.assistant.channels._push_common import (
    ChannelSendError,
    OutboundPushChannel,
    http_post,
)
from doyoutrade.assistant.channels.base import (
    ChannelDeliveryReceipt,
    ContentPart,
    ImageContent,
    TextContent,
)

if TYPE_CHECKING:
    from doyoutrade.assistant.service import AssistantService
    from doyoutrade.assistant.channels.config import SlackChannelConfig

logger = logging.getLogger(__name__)


class SlackChannel(OutboundPushChannel):
    """Slack。优先 incoming webhook；否则 bot token + ``chat.postMessage``。

    图片没有免公网 URL 的简单上传面（files.upload 已废弃、外部 URL 需可公网访问），
    因此 ImageContent 一律回退发送 caption 文本，并记 warning。
    """

    channel_type = "slack"

    def __init__(
        self,
        assistant_service: "AssistantService | None" = None,
        *,
        channel_id: str | None = None,
        webhook_url: str = "",
        bot_token: str = "",
        slack_channel_id: str = "",
        api_base: str = "https://slack.com/api",
    ):
        super().__init__(assistant_service, channel_id=channel_id)
        self.webhook_url = webhook_url
        self.bot_token = bot_token
        self.slack_channel_id = slack_channel_id
        self.api_base = api_base.rstrip("/")

    @classmethod
    def from_config(
        cls,
        assistant_service: "AssistantService",
        config: "SlackChannelConfig",
    ) -> "SlackChannel":
        return cls(
            assistant_service,
            channel_id=getattr(config, "channel_id", None),
            webhook_url=config.webhook_url,
            bot_token=config.bot_token,
            slack_channel_id=config.channel_id,
            api_base=config.api_base,
        )

    async def send(
        self,
        session_id: str,
        content: ContentPart,
        meta: dict[str, Any],
    ) -> ChannelDeliveryReceipt | None:
        if not self.webhook_url and not self.bot_token:
            raise ChannelSendError(
                self.channel_type, "not_configured", "neither webhook_url nor bot_token set"
            )

        if isinstance(content, TextContent):
            text = content.text
        elif isinstance(content, ImageContent):
            if content.caption:
                logger.warning(
                    "slack image send: no direct byte-upload surface (session=%s); "
                    "sending caption text",
                    session_id,
                )
                text = content.caption
            else:
                logger.warning("slack image send: no caption to fall back to; skipped")
                return None
        else:
            logger.warning(
                "slack channel: unsupported content type %s; skipped",
                type(content).__name__,
            )
            return None

        await self._send_text(text)
        return None

    async def _send_text(self, text: str) -> None:
        if self.webhook_url:
            ok, status, detail = await http_post(self.webhook_url, json={"text": text})
            if not ok:
                raise ChannelSendError(
                    self.channel_type, "http_error", f"status={status} body={detail}"
                )
            return

        if not self.slack_channel_id:
            raise ChannelSendError(
                self.channel_type, "no_chat_id", "channel_id required with bot_token"
            )
        ok, status, detail = await http_post(
            f"{self.api_base}/chat.postMessage",
            json={"channel": self.slack_channel_id, "text": text},
            headers={"Authorization": f"Bearer {self.bot_token}"},
        )
        if not ok:
            raise ChannelSendError(
                self.channel_type, "http_error", f"status={status} body={detail}"
            )
        if '"ok":false' in detail.replace(" ", ""):
            raise ChannelSendError(
                self.channel_type, "api_error", f"slack ok=false: {detail}"
            )
