"""ChannelManager — manages channel lifecycle and message routing."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from doyoutrade.assistant.lifecycle_commands import parse_lifecycle_command
from doyoutrade.assistant.main_agent import MAIN_AGENT_ID

if TYPE_CHECKING:
    from doyoutrade.assistant.channels.base import BaseChannel
    from doyoutrade.assistant.service import AssistantService

logger = logging.getLogger(__name__)

# The channel→agent fallback is the code-fixed main agent.
DEFAULT_AGENT_ID = MAIN_AGENT_ID


class ChannelManager:
    """管理所有 Channel 的生命周期和消息路由。"""

    def __init__(self, assistant_service: "AssistantService", default_agent_id: str = DEFAULT_AGENT_ID):
        self._channels: dict[str, BaseChannel] = {}
        self._agent_ids: dict[str, str] = {}
        self._active_peer_sessions: dict[tuple[str, str], str] = {}
        self._assistant_service = assistant_service
        self._default_agent_id = default_agent_id

    def register(self, channel: BaseChannel, *, agent_id: str | None = None) -> None:
        channel_id = getattr(channel, "channel_id", None) or channel.channel_type
        channel.channel_id = channel_id
        if channel_id in self._channels:
            raise ValueError(
                f"Channel '{channel_id}' already registered"
            )
        self._channels[channel_id] = channel
        self._agent_ids[channel_id] = agent_id or self._default_agent_id

    def get(self, channel_id: str) -> BaseChannel | None:
        return self._channels.get(channel_id)

    @property
    def channel_types(self) -> list[str]:
        return [channel.channel_type for channel in self._channels.values()]

    @property
    def channel_ids(self) -> list[str]:
        return list(self._channels.keys())

    async def start_all(self) -> dict[str, BaseException | None]:
        """启动所有 channel，返回 channel_id -> exception (None 表示成功)。"""
        results = await asyncio.gather(
            *[ch.start() for ch in self._channels.values()],
            return_exceptions=True,
        )
        return dict(zip(self._channels.keys(), results))

    async def stop_all(self) -> dict[str, BaseException | None]:
        """停止所有 channel，返回 channel_id -> exception (None 表示成功)。"""
        results = await asyncio.gather(
            *[ch.stop() for ch in self._channels.values()],
            return_exceptions=True,
        )
        return dict(zip(self._channels.keys(), results))

    async def start(self, channel_id: str) -> BaseException | None:
        channel = self._channels.get(channel_id)
        if channel:
            logger.info("ChannelManager.start: channel_id=%s", channel_id)
            try:
                await channel.start()
                return None
            except Exception as exc:
                logger.exception("failed to start channel_id=%s", channel_id)
                return exc
        else:
            logger.warning("ChannelManager.start: channel not found channel_id=%s", channel_id)
            return None

    async def stop(self, channel_id: str) -> BaseException | None:
        channel = self._channels.get(channel_id)
        if channel:
            logger.info("ChannelManager.stop: channel_id=%s", channel_id)
            try:
                await channel.stop()
                return None
            except Exception as exc:
                logger.exception("failed to stop channel_id=%s", channel_id)
                return exc
        else:
            logger.warning("ChannelManager.stop: channel not found channel_id=%s", channel_id)
            return None

    async def enqueue(
        self, channel_id: str, native_payload: Any, agent_id: str | None = None
    ) -> None:
        channel = self._channels.get(channel_id)
        if not channel:
            return
        request = channel.build_agent_request_from_native(native_payload)
        route_session_id = await self._resolve_route_session_id(channel_id, request.session_id)
        asyncio.create_task(
            self._deliver_message(
                route_session_id,
                request.content,
                request.sender_id,
                request.channel_meta,
                agent_id=agent_id or self._agent_ids.get(channel_id),
                channel=channel,
                channel_id=channel_id,
                peer_session_id=request.session_id,
            )
        )

    async def _resolve_route_session_id(self, channel_id: str, peer_session_id: str) -> str:
        """Resolve which session a peer's message lands in, honoring a prior ``/new``.

        ``_active_peer_sessions`` is a hot in-memory cache. On a cache miss (e.g.
        right after a restart) the durable mapping is consulted so a ``/new``
        rebinding survives restarts instead of silently snapping the peer back to
        the old session. A lookup failure is surfaced (warning) and degrades to the
        peer's deterministic session rather than swallowing the error.
        """
        key = (channel_id, peer_session_id)
        cached = self._active_peer_sessions.get(key)
        if cached is not None:
            return cached
        try:
            persisted = await self._assistant_service.get_active_channel_peer_session(
                channel_id, peer_session_id
            )
        except Exception as exc:
            logger.warning(
                "channel peer-session lookup failed channel_id=%s peer=%s err=%s: %s; "
                "falling back to peer session",
                channel_id,
                peer_session_id,
                type(exc).__name__,
                exc,
            )
            return peer_session_id
        if persisted:
            self._active_peer_sessions[key] = persisted
            logger.info(
                "channel peer-session rebind restored from store channel_id=%s peer=%s active=%s",
                channel_id,
                peer_session_id,
                persisted,
            )
            return persisted
        return peer_session_id

    async def _persist_peer_rebind(
        self, channel_id: str, peer_session_id: str, new_session_id: str
    ) -> None:
        """Persist a ``/new`` rebinding; never block the user reply on a store error."""
        self._active_peer_sessions[(channel_id, peer_session_id)] = new_session_id
        try:
            await self._assistant_service.set_active_channel_peer_session(
                channel_id, peer_session_id, new_session_id
            )
        except Exception as exc:
            # The rebinding still holds for this process via the cache above, but
            # won't survive a restart. Surface it loudly — a silent failure here is
            # exactly the bug this method fixes.
            logger.warning(
                "channel peer-session persist failed channel_id=%s peer=%s new_session_id=%s "
                "err=%s: %s; rebinding is in-memory only and will not survive restart",
                channel_id,
                peer_session_id,
                new_session_id,
                type(exc).__name__,
                exc,
            )

    async def _deliver_message(
        self,
        session_id: str,
        content: str,
        sender_id: str,
        channel_meta: dict[str, Any],
        agent_id: str | None = None,
        channel: "BaseChannel | None" = None,
        channel_id: str | None = None,
        peer_session_id: str | None = None,
    ) -> None:
        streaming_controller = None
        streaming_delivery_receipt = None
        answer_text = ""
        try:
            is_lifecycle_command = parse_lifecycle_command(content) is not None
            session = await self._assistant_service.get_or_create_session(
                session_id=session_id,
                agent_id=agent_id or self._default_agent_id,
                title="",
            )
            update_session_config = getattr(self._assistant_service, "update_session_config", None)
            if callable(update_session_config) and isinstance(session, dict):
                session_config = dict(session.get("config") or {})
                existing_channel = dict(session_config.get("channel") or {})
                normalized_channel = {
                    "channel_id": channel_id or existing_channel.get("channel_id"),
                    "channel_type": getattr(channel, "channel_type", None) or existing_channel.get("channel_type"),
                    "sender_id": sender_id or existing_channel.get("sender_id"),
                }
                if channel_meta:
                    normalized_channel["meta"] = dict(channel_meta)
                if existing_channel != normalized_channel:
                    await update_session_config(session_id, {"channel": normalized_channel})
            effective_channel_meta = dict(channel_meta or {})
            if channel is not None:
                reply_target_message_id = str(
                    channel.get_reply_target_message_id(effective_channel_meta) or ""
                ).strip()
                if reply_target_message_id:
                    local_delivery_ref = await self._assistant_service.resolve_channel_delivery_ref(
                        session_id,
                        channel_type=getattr(channel, "channel_type", ""),
                        platform_message_id=reply_target_message_id,
                    )
                    if local_delivery_ref is not None:
                        effective_channel_meta = channel.apply_local_delivery_ref(
                            effective_channel_meta,
                            local_delivery_ref,
                        )
            # Try to create streaming controller for this channel/session
            if channel is not None and not is_lifecycle_command:
                streaming_controller = channel.create_streaming_controller(
                    session_id,
                    effective_channel_meta,
                )
            send_kwargs = {"session_id": session_id, "content": content}
            if channel is not None:
                turn_context_reminder = channel.build_turn_context_reminder(effective_channel_meta)
                if turn_context_reminder:
                    send_kwargs["turn_context_reminder"] = turn_context_reminder
                user_message_metadata = channel.build_user_message_metadata(effective_channel_meta)
                if user_message_metadata:
                    send_kwargs["user_message_metadata"] = user_message_metadata
            if streaming_controller is not None:
                send_kwargs["streaming_controller"] = streaming_controller
            result = await self._assistant_service.send_message(**send_kwargs)
            lifecycle_command = result.get("lifecycle_command") if isinstance(result, dict) else None
            outbound_session_id = session_id
            if (
                isinstance(lifecycle_command, dict)
                and lifecycle_command.get("command") == "new"
                and channel_id
                and peer_session_id
            ):
                new_session_id = str(
                    lifecycle_command.get("new_session_id")
                    or lifecycle_command.get("session_id")
                    or result.get("session", {}).get("session_id")
                    or ""
                )
                if new_session_id:
                    outbound_session_id = new_session_id
                    await self._persist_peer_rebind(channel_id, peer_session_id, new_session_id)
            # Send lifecycle reply notification via channel.send_reply()
            reply = result.get("reply") if isinstance(result, dict) else None
            messages = result.get("messages", []) if isinstance(result, dict) else []
            assistant_message_id = ""
            for msg in reversed(messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    assistant_message_id = str(msg.get("message_id") or "")
                    answer_text = msg["content"]
                    break
            if reply is not None and channel is not None:
                from doyoutrade.assistant.channels.base import LifecycleReply
                lifecycle_reply = LifecycleReply(
                    type=reply.get("type", "lifecycle_notification"),
                    title=reply.get("title", ""),
                    content=reply.get("content", []),
                    footer=reply.get("footer"),
                )
                try:
                    delivery_receipt = await channel.send_reply(
                        session_id,
                        lifecycle_reply,
                        effective_channel_meta,
                    )
                    if delivery_receipt is not None:
                        await self._assistant_service.register_channel_delivery_refs(
                            outbound_session_id,
                            channel_type=getattr(channel, "channel_type", ""),
                            handles=list(delivery_receipt.handles or []),
                            canonical_text=channel.render_lifecycle_reply_text(lifecycle_reply),
                            source="lifecycle_reply",
                        )
                    logger.info("ChannelManager: lifecycle reply sent session_id=%s", session_id)
                except Exception:
                    logger.exception("ChannelManager: send_reply failed, ignoring")
            # Finalize streaming card if streaming was active
            if streaming_controller is not None and not streaming_controller.is_terminal_phase:
                try:
                    await streaming_controller.on_idle()
                    if channel is not None:
                        streaming_delivery_receipt = channel.collect_streaming_delivery_receipt(
                            streaming_controller,
                            effective_channel_meta,
                        )
                    if hasattr(streaming_controller, "message_id") and getattr(
                        streaming_controller, "message_id", None
                    ) is None:
                        raise RuntimeError("streaming finalize completed without message_id")
                except Exception:
                    logger.exception(
                        "streaming finalize failed session_id=%s sender_id=%s; falling back to plain text",
                        session_id,
                        sender_id,
                    )
                    if channel is None or not answer_text:
                        raise
                    from doyoutrade.assistant.channels.base import TextContent

                    delivery_receipt = await channel.send(
                        session_id,
                        TextContent(text=answer_text),
                        effective_channel_meta,
                    )
                    if delivery_receipt is not None:
                        await self._assistant_service.register_channel_delivery_refs(
                            outbound_session_id,
                            channel_type=getattr(channel, "channel_type", ""),
                            handles=list(delivery_receipt.handles or []),
                            canonical_text=answer_text,
                            source="assistant_message",
                            assistant_message_id=assistant_message_id,
                        )
                    logger.info("ChannelManager: fallback plain-text reply sent session_id=%s", session_id)
            if channel is not None and streaming_delivery_receipt is not None and answer_text:
                await self._assistant_service.register_channel_delivery_refs(
                    outbound_session_id,
                    channel_type=getattr(channel, "channel_type", ""),
                    handles=list(streaming_delivery_receipt.handles or []),
                    canonical_text=answer_text,
                    source="assistant_message",
                    assistant_message_id=assistant_message_id,
                )
            # Push response back via channel.send() only for non-streaming channels.
            # Streaming channels render and finalize the reply through their controller.
            if channel is not None and streaming_controller is None:
                if answer_text:
                    from doyoutrade.assistant.channels.base import TextContent
                    delivery_receipt = await channel.send(
                        session_id,
                        TextContent(text=answer_text),
                        effective_channel_meta,
                    )
                    if delivery_receipt is not None:
                        await self._assistant_service.register_channel_delivery_refs(
                            outbound_session_id,
                            channel_type=getattr(channel, "channel_type", ""),
                            handles=list(delivery_receipt.handles or []),
                            canonical_text=answer_text,
                            source="assistant_message",
                            assistant_message_id=assistant_message_id,
                        )
                    logger.info("Feishu: response sent via channel.send session_id=%s", session_id)
        except Exception:
            # Abort streaming card on error
            if streaming_controller is not None and not streaming_controller.is_terminal_phase:
                try:
                    await streaming_controller.abort_card()
                except Exception:
                    logger.debug("abort_card failed during error handling")
            logger.exception(
                "failed to deliver message session_id=%s sender_id=%s content=%r",
                session_id,
                sender_id,
                content[:100] if content else "",
            )
