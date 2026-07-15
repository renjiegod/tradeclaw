"""Assistant channel base types and abstract interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from doyoutrade.assistant.service import AssistantService
    from doyoutrade.assistant.channels.config import BaseChannelConfig


@dataclass
class LifecycleReply:
    """生命周期命令的回复内容。"""
    type: str = "lifecycle_notification"
    title: str = ""
    content: list[dict] = field(default_factory=list)  # [{"label": str, "value": str}, ...]
    footer: str | None = None


# --- Content part types ---

class ContentPart:
    """Channel 发送内容片段的基类。"""
    pass


@dataclass
class TextContent(ContentPart):
    text: str = ""
    markdown: bool = False


@dataclass
class ImageContent(ContentPart):
    image_id: str | None = None
    url: str | None = None
    # Raw image bytes (e.g. a md2img PNG). When set and no platform ``image_id``
    # is available, the channel is responsible for uploading these bytes to the
    # platform to obtain an id/key before sending. Keeps callers (cron executors)
    # free of any platform-specific upload logic.
    data: bytes | None = None
    mime_type: str = "image/png"
    filename: str = "image.png"
    # Text shown when the channel cannot render an image (fallback / alt text).
    caption: str = ""


@dataclass
class FileContent(ContentPart):
    file_id: str | None = None
    name: str | None = None
    # Raw file bytes; channel uploads them when no platform ``file_id`` is given.
    data: bytes | None = None
    mime_type: str = "application/octet-stream"


@dataclass
class AudioContent(ContentPart):
    audio_id: str | None = None
    duration_sec: float | None = None


@dataclass
class CardContent(ContentPart):
    """卡片内容片段。"""
    card: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelAgentRequest:
    """从平台原生消息转换后的统一请求格式。"""
    session_id: str
    content: str
    sender_id: str = ""
    channel_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelDeliveryHandle:
    """A platform-specific outbound message handle that users may reply to."""

    platform_message_id: str
    platform_message_type: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelDeliveryReceipt:
    """Handles created while delivering one logical assistant reply."""

    handles: list[ChannelDeliveryHandle] = field(default_factory=list)


# --- BaseChannel ABC ---


class BaseChannel(ABC):
    """Channel 抽象基类。所有 Channel 必须实现此接口。"""

    channel_type: str = ""

    def __init__(
        self,
        assistant_service: "AssistantService" | None = None,
        *,
        channel_id: str | None = None,
    ):
        self._assistant_service = assistant_service
        self.channel_id = channel_id or self.channel_type

    @classmethod
    @abstractmethod
    def from_config(
        cls,
        assistant_service: "AssistantService",
        config: "BaseChannelConfig",
    ) -> "BaseChannel":
        raise NotImplementedError

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def build_agent_request_from_native(
        self, native_payload: Any
    ) -> ChannelAgentRequest:
        raise NotImplementedError

    @abstractmethod
    async def send(
        self,
        session_id: str,
        content: ContentPart,
        meta: dict[str, Any],
    ) -> ChannelDeliveryReceipt | None:
        raise NotImplementedError

    def resolve_session_id(self, sender_id: str, meta: dict[str, Any]) -> str:
        """从 sender_id 解析 AssistantService session_id。

        默认实现: f"channel:{channel_id}:{sender_id}"
        子类可覆盖以实现平台特定逻辑。
        """
        return f"channel:{self.channel_id}:{sender_id}"

    def create_streaming_controller(
        self, session_id: str, meta: dict[str, Any]
    ) -> Any:
        """Create a streaming card controller for this session.

        Returns None if streaming is not supported.
        Override in channel subclasses that support streaming cards.
        """
        return None

    def build_turn_context_reminder(self, meta: dict[str, Any]) -> str | None:
        """Build an ephemeral reminder injected into the current model turn."""
        return None

    def build_user_message_metadata(self, meta: dict[str, Any]) -> dict[str, Any]:
        """Return structured metadata to persist on the inbound user message."""
        return {}

    def get_reply_target_message_id(self, meta: dict[str, Any]) -> str:
        """Return the platform-native message id the inbound user message replied to."""
        return ""

    def apply_local_delivery_ref(
        self,
        meta: dict[str, Any],
        delivery_ref: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge a locally cached outbound delivery ref into inbound channel meta."""
        merged = dict(meta or {})
        return merged

    def collect_streaming_delivery_receipt(
        self,
        controller: Any,
        meta: dict[str, Any],
    ) -> ChannelDeliveryReceipt | None:
        """Collect outbound handles created by a streaming controller, if any."""
        return None

    def render_lifecycle_reply_text(self, reply: LifecycleReply) -> str:
        """Best-effort canonical text for a lifecycle reply."""
        lines: list[str] = []
        if reply.title:
            lines.append(reply.title)
        for item in reply.content:
            label = str(item.get("label") or "").strip()
            value = str(item.get("value") or "").strip()
            if label and value:
                lines.append(f"{label}: {value}")
            elif value:
                lines.append(value)
        if reply.footer:
            lines.append(str(reply.footer))
        return "\n".join(line for line in lines if line).strip()

    async def send_reply(
        self,
        session_id: str,
        reply: LifecycleReply,
        meta: dict[str, Any],
    ) -> ChannelDeliveryReceipt | None:
        """发送生命周期命令的回复。

        默认实现为空（pass），子类可覆盖以发送富文本通知。
        如果 channel 未实现，调用方应静默跳过。
        """
        return None

    def clone(self, config: "BaseChannelConfig") -> "BaseChannel":
        """用新配置克隆 Channel 实例，用于 restart_channel()。"""
        return self.from_config(self._assistant_service, config)
