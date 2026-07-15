"""DingTalk (钉钉) group-bot push channel — outbound only."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
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
    from doyoutrade.assistant.channels.config import DingtalkChannelConfig

logger = logging.getLogger(__name__)


class DingtalkChannel(OutboundPushChannel):
    """钉钉群机器人。支持加签(sign_secret)与 markdown。

    钉钉 markdown 仅支持通过 URL 引用图片，无直接上传，故 ``ImageContent`` 回退为
    发送其 ``caption`` 文本(缺失则跳过并 log)。
    """

    channel_type = "dingtalk"

    def __init__(
        self,
        assistant_service: "AssistantService | None" = None,
        *,
        channel_id: str | None = None,
        webhook_url: str = "",
        sign_secret: str = "",
        msg_type: str = "markdown",
    ):
        super().__init__(assistant_service, channel_id=channel_id)
        self.webhook_url = webhook_url
        self.sign_secret = sign_secret
        self.msg_type = msg_type

    @classmethod
    def from_config(
        cls,
        assistant_service: "AssistantService",
        config: "DingtalkChannelConfig",
    ) -> "DingtalkChannel":
        return cls(
            assistant_service,
            channel_id=getattr(config, "channel_id", None),
            webhook_url=config.webhook_url,
            sign_secret=config.sign_secret,
            msg_type=config.msg_type,
        )

    def _signed_url(self) -> str:
        if not self.sign_secret:
            return self.webhook_url
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.sign_secret}"
        hmac_code = hmac.new(
            self.sign_secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in self.webhook_url else "?"
        return f"{self.webhook_url}{sep}timestamp={timestamp}&sign={sign}"

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
            if content.caption:
                logger.warning(
                    "dingtalk image send: no native upload; sending caption text (session=%s)",
                    session_id,
                )
                body = self._text_body(content.caption, markdown=False)
            else:
                logger.warning("dingtalk image send: no caption to fall back to; skipped")
                return None
        else:
            logger.warning(
                "dingtalk channel: unsupported content type %s; skipped",
                type(content).__name__,
            )
            return None

        ok, status, detail = await http_post(self._signed_url(), json=body)
        if not ok:
            raise ChannelSendError(
                self.channel_type, "http_error", f"status={status} body={detail}"
            )
        if '"errcode":0' not in detail.replace(" ", "") and '"errcode": 0' not in detail:
            raise ChannelSendError(
                self.channel_type, "api_error", f"non-zero errcode: {detail}"
            )
        return None

    def _text_body(self, text: str, markdown: bool) -> dict[str, Any]:
        if markdown or self.msg_type == "markdown":
            title = (text or "").strip().splitlines()[0].lstrip("#").strip() if text.strip() else "通知"
            return {"msgtype": "markdown", "markdown": {"title": title[:64] or "通知", "text": text}}
        return {"msgtype": "text", "text": {"content": text}}
