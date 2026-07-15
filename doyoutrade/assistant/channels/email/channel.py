"""Email (SMTP) push channel — outbound only."""
from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from doyoutrade.assistant.channels._push_common import (
    ChannelSendError,
    OutboundPushChannel,
)
from doyoutrade.assistant.channels.base import (
    ChannelDeliveryHandle,
    ChannelDeliveryReceipt,
    ContentPart,
    ImageContent,
    TextContent,
)

if TYPE_CHECKING:
    from doyoutrade.assistant.service import AssistantService
    from doyoutrade.assistant.channels.config import EmailChannelConfig

logger = logging.getLogger(__name__)


class EmailChannel(OutboundPushChannel):
    """Send notifications over SMTP. Supports text/markdown body + inline image."""

    channel_type = "email"

    def __init__(
        self,
        assistant_service: "AssistantService | None" = None,
        *,
        channel_id: str | None = None,
        smtp_host: str = "",
        smtp_port: int = 465,
        use_tls: bool = True,
        use_starttls: bool = False,
        username: str = "",
        password: str = "",
        from_addr: str = "",
        to_addrs: list[str] | None = None,
        subject_prefix: str = "[Doyoutrade]",
    ):
        super().__init__(assistant_service, channel_id=channel_id)
        self.smtp_host = smtp_host
        self.smtp_port = int(smtp_port)
        self.use_tls = use_tls
        self.use_starttls = use_starttls
        self.username = username
        self.password = password
        self.from_addr = from_addr or username
        self.to_addrs = list(to_addrs or [])
        self.subject_prefix = subject_prefix

    @classmethod
    def from_config(
        cls,
        assistant_service: "AssistantService",
        config: "EmailChannelConfig",
    ) -> "EmailChannel":
        return cls(
            assistant_service,
            channel_id=getattr(config, "channel_id", None),
            smtp_host=config.smtp_host,
            smtp_port=config.smtp_port,
            use_tls=config.use_tls,
            use_starttls=config.use_starttls,
            username=config.username,
            password=config.password,
            from_addr=config.from_addr,
            to_addrs=config.to_addrs,
            subject_prefix=config.subject_prefix,
        )

    def _subject_for(self, text: str) -> str:
        first_line = (text or "").strip().splitlines()[0] if text.strip() else "通知"
        # Strip common markdown heading markers from the subject line.
        first_line = first_line.lstrip("#").strip() or "通知"
        return f"{self.subject_prefix} {first_line}"[:200]

    def _recipients(self, meta: dict[str, Any]) -> list[str]:
        override = meta.get("email_to") if isinstance(meta, dict) else None
        if isinstance(override, str) and override.strip():
            return [a.strip() for a in override.split(",") if a.strip()]
        if isinstance(override, list) and override:
            return [str(a).strip() for a in override if str(a).strip()]
        return self.to_addrs

    async def send(
        self,
        session_id: str,
        content: ContentPart,
        meta: dict[str, Any],
    ) -> ChannelDeliveryReceipt | None:
        if not self.smtp_host:
            raise ChannelSendError(self.channel_type, "not_configured", "smtp_host is empty")
        recipients = self._recipients(meta)
        if not recipients:
            raise ChannelSendError(
                self.channel_type, "no_recipients", "no to_addrs configured"
            )

        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(recipients)

        if isinstance(content, TextContent):
            msg["Subject"] = self._subject_for(content.text)
            self._set_text_body(msg, content.text, content.markdown)
        elif isinstance(content, ImageContent):
            caption = content.caption or "报告图片"
            msg["Subject"] = self._subject_for(caption)
            msg.set_content(caption)
            if content.data:
                maintype, _, subtype = (content.mime_type or "image/png").partition("/")
                msg.add_attachment(
                    content.data,
                    maintype=maintype or "image",
                    subtype=subtype or "png",
                    filename=content.filename or "image.png",
                )
            else:
                logger.warning(
                    "email image send: no image data (session=%s); sending caption only",
                    session_id,
                )
        else:
            # Unknown content type — surface as a skip rather than crashing.
            logger.warning(
                "email channel: unsupported content type %s (session=%s); skipped",
                type(content).__name__,
                session_id,
            )
            return None

        await self._smtp_send(msg)
        return ChannelDeliveryReceipt(
            handles=[ChannelDeliveryHandle(platform_message_id=msg["Subject"], platform_message_type="email")]
        )

    def _set_text_body(self, msg: EmailMessage, text: str, markdown: bool) -> None:
        if markdown:
            try:
                import markdown as _md

                html = _md.markdown(text, extensions=["tables", "fenced_code"])
                msg.set_content(text)  # plaintext fallback
                msg.add_alternative(f"<html><body>{html}</body></html>", subtype="html")
                return
            except ImportError as exc:
                logger.warning(
                    "email markdown->html unavailable (%s: %s); sending plaintext",
                    type(exc).__name__,
                    exc,
                )
        msg.set_content(text)

    async def _smtp_send(self, msg: EmailMessage) -> None:
        try:
            import aiosmtplib
        except ImportError as exc:
            raise ChannelSendError(
                self.channel_type,
                "dependency_missing",
                f"aiosmtplib not installed ({exc}); pip install 'doyoutrade[report]'",
            ) from exc

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.username or None,
                password=self.password or None,
                use_tls=self.use_tls,
                start_tls=self.use_starttls,
            )
        except Exception as exc:  # noqa: BLE001 - report structurally, do not swallow
            raise ChannelSendError(
                self.channel_type, "smtp_error", f"{type(exc).__name__}: {exc}"
            ) from exc
