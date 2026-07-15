"""Telegram bot push channel — outbound only."""
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
    from doyoutrade.assistant.channels.config import TelegramChannelConfig

logger = logging.getLogger(__name__)


class TelegramChannel(OutboundPushChannel):
    """Telegram Bot API。文本走 ``sendMessage``，图片走 ``sendPhoto``（multipart）。"""

    channel_type = "telegram"

    def __init__(
        self,
        assistant_service: "AssistantService | None" = None,
        *,
        channel_id: str | None = None,
        bot_token: str = "",
        chat_id: str = "",
        message_thread_id: str = "",
        api_base: str = "https://api.telegram.org",
    ):
        super().__init__(assistant_service, channel_id=channel_id)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.message_thread_id = message_thread_id
        self.api_base = api_base.rstrip("/")

    @classmethod
    def from_config(
        cls,
        assistant_service: "AssistantService",
        config: "TelegramChannelConfig",
    ) -> "TelegramChannel":
        return cls(
            assistant_service,
            channel_id=getattr(config, "channel_id", None),
            bot_token=config.bot_token,
            chat_id=config.chat_id,
            message_thread_id=config.message_thread_id,
            api_base=config.api_base,
        )

    async def send(
        self,
        session_id: str,
        content: ContentPart,
        meta: dict[str, Any],
    ) -> ChannelDeliveryReceipt | None:
        if not self.bot_token:
            raise ChannelSendError(self.channel_type, "not_configured", "bot_token is empty")
        if not self.chat_id:
            raise ChannelSendError(self.channel_type, "no_chat_id", "chat_id is empty")

        if isinstance(content, TextContent):
            await self._send_message(content.text)
        elif isinstance(content, ImageContent):
            if content.data:
                await self._send_photo(content.data, content.filename, content.caption)
            elif content.caption:
                logger.warning(
                    "telegram image send: no image data (session=%s); sending caption text",
                    session_id,
                )
                await self._send_message(content.caption)
            else:
                logger.warning("telegram image send: no data and no caption; skipped")
                return None
        else:
            logger.warning(
                "telegram channel: unsupported content type %s; skipped",
                type(content).__name__,
            )
            return None
        return None

    def _api_url(self, method: str) -> str:
        return f"{self.api_base}/bot{self.bot_token}/{method}"

    def _base_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": self.chat_id}
        if self.message_thread_id:
            payload["message_thread_id"] = self.message_thread_id
        return payload

    async def _send_message(self, text: str) -> None:
        payload = self._base_payload()
        payload["text"] = text
        ok, status, detail = await http_post(self._api_url("sendMessage"), json=payload)
        self._check_response(ok, status, detail)

    async def _send_photo(self, data: bytes, filename: str, caption: str) -> None:
        fields = {str(k): str(v) for k, v in self._base_payload().items()}
        if caption:
            fields["caption"] = caption[:1024]
        ok, status, detail = await http_post(
            self._api_url("sendPhoto"),
            data=fields,
            files={"photo": (filename or "image.png", data)},
        )
        self._check_response(ok, status, detail)

    def _check_response(self, ok: bool, status: int, detail: str) -> None:
        if not ok:
            raise ChannelSendError(
                self.channel_type, "http_error", f"status={status} body={detail}"
            )
        # Telegram returns {"ok": false, ...} with HTTP 200 in some proxy setups.
        if '"ok":false' in detail.replace(" ", ""):
            raise ChannelSendError(
                self.channel_type, "api_error", f"telegram ok=false: {detail}"
            )
