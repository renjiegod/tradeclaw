"""WeCom (企业微信) group-bot push channel — outbound only."""
from __future__ import annotations

import base64
import hashlib
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
    from doyoutrade.assistant.channels.config import WecomChannelConfig

logger = logging.getLogger(__name__)


class WecomChannel(OutboundPushChannel):
    """企业微信群机器人。webhook 携带 bot key。支持 markdown 文本与图片。"""

    channel_type = "wecom"

    def __init__(
        self,
        assistant_service: "AssistantService | None" = None,
        *,
        channel_id: str | None = None,
        webhook_url: str = "",
        msg_type: str = "markdown",
    ):
        super().__init__(assistant_service, channel_id=channel_id)
        self.webhook_url = webhook_url
        self.msg_type = msg_type

    @classmethod
    def from_config(
        cls,
        assistant_service: "AssistantService",
        config: "WecomChannelConfig",
    ) -> "WecomChannel":
        return cls(
            assistant_service,
            channel_id=getattr(config, "channel_id", None),
            webhook_url=config.webhook_url,
            msg_type=config.msg_type,
        )

    async def send(
        self,
        session_id: str,
        content: ContentPart,
        meta: dict[str, Any],
    ) -> ChannelDeliveryReceipt | None:
        if not self.webhook_url:
            raise ChannelSendError(self.channel_type, "not_configured", "webhook_url is empty")

        if isinstance(content, TextContent):
            body = self._text_body(content.text, content.markdown)
        elif isinstance(content, ImageContent):
            if content.data:
                body = self._image_body(content.data)
            elif content.caption:
                logger.warning(
                    "wecom image send: no image data (session=%s); sending caption text",
                    session_id,
                )
                body = self._text_body(content.caption, markdown=False)
            else:
                logger.warning("wecom image send: no data and no caption; skipped")
                return None
        else:
            logger.warning(
                "wecom channel: unsupported content type %s; skipped",
                type(content).__name__,
            )
            return None

        ok, status, detail = await http_post(self.webhook_url, json=body)
        if not ok:
            raise ChannelSendError(
                self.channel_type, "http_error", f"status={status} body={detail}"
            )
        # WeCom returns errcode in the JSON body even on HTTP 200.
        if '"errcode":0' not in detail.replace(" ", "") and '"errcode": 0' not in detail:
            raise ChannelSendError(
                self.channel_type, "api_error", f"non-zero errcode: {detail}"
            )
        return None

    def _text_body(self, text: str, markdown: bool) -> dict[str, Any]:
        if markdown or self.msg_type == "markdown":
            return {"msgtype": "markdown", "markdown": {"content": text}}
        return {"msgtype": "text", "text": {"content": text}}

    def _image_body(self, data: bytes) -> dict[str, Any]:
        return {
            "msgtype": "image",
            "image": {
                "base64": base64.b64encode(data).decode("ascii"),
                "md5": hashlib.md5(data).hexdigest(),
            },
        }
