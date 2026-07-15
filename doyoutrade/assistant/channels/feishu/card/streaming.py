"""流式卡片状态机控制器。

状态转换:
    idle → creating → streaming → completed / aborted / terminated

参考 openclaw-lark StreamingCardController 的状态机设计。

IMPORTANT: When modifying card update logic or streaming behavior, always refer to:
https://open.feishu.cn/document/feishu-cards/card-json-v2-components/component-json-v2-overview

Streaming updates use:
- CardKit element content API: PUT /cardkit/v1/cards/{card_id}/elements/{element_id}/content
- IM patch message: PATCH /im/v1/messages/{message_id}

Throttle constants (THROTTLE_CARDKIT_MS, THROTTLE_PATCH_MS) control update frequency
to respect API rate limits documented in the official CardKit API reference.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from enum import Enum
from typing import Any, Callable, Coroutine

from .cardkit import CardKitClient
from .builder import (
    STREAMING_ELEMENT_ID,
    build_approval_card,
    build_ask_user_card,
    build_complete_card,
    build_streaming_card,
    build_thinking_card,
    build_tool_call_card,
)

logger = logging.getLogger(__name__)

# 节流常量
THROTTLE_CARDKIT_MS = 300
THROTTLE_PATCH_MS = 500


def _preview_indicates_error(preview: str | None) -> bool:
    if not preview:
        return False
    try:
        parsed = json.loads(preview)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    return str(parsed.get("status") or "").lower() == "error" or bool(parsed.get("is_error"))


# 状态机
class CardPhase(str, Enum):
    IDLE = "idle"
    CREATING = "creating"
    STREAMING = "streaming"
    COMPLETED = "completed"
    ABORTED = "aborted"
    TERMINATED = "terminated"
    CREATION_FAILED = "creation_failed"


# 合法的状态转换
PHASE_TRANSITIONS: dict[CardPhase, set[CardPhase]] = {
    CardPhase.IDLE: {CardPhase.CREATING, CardPhase.COMPLETED, CardPhase.ABORTED, CardPhase.TERMINATED},
    CardPhase.CREATING: {CardPhase.STREAMING, CardPhase.TERMINATED, CardPhase.CREATION_FAILED},
    CardPhase.STREAMING: {CardPhase.COMPLETED, CardPhase.ABORTED, CardPhase.TERMINATED},
    CardPhase.COMPLETED: set(),
    CardPhase.ABORTED: set(),
    CardPhase.TERMINATED: set(),
    CardPhase.CREATION_FAILED: set(),
}


class FlushController:
    """控制卡片更新的节流控制器。"""

    def __init__(self, flush_fn: Callable[[], Any], cardkit_ms: int = THROTTLE_CARDKIT_MS, patch_ms: int = THROTTLE_PATCH_MS):
        self._flush_fn = flush_fn
        self._cardkit_ms = cardkit_ms
        self._patch_ms = patch_ms
        self._pending_flush: asyncio.Task | None = None
        self._last_flush_time = 0.0
        self._canceled = False
        self._completed = False

    def set_card_message_ready(self, ready: bool) -> None:
        pass

    def cancel_pending_flush(self) -> None:
        self._canceled = True
        if self._pending_flush and not self._pending_flush.done():
            self._pending_flush.cancel()

    def complete(self) -> None:
        self._completed = True

    async def throttled_update(self, delay_ms: int) -> None:
        """节流更新：延迟 delay_ms 后执行 flush。"""
        if self._canceled or self._completed:
            return
        if self._pending_flush and not self._pending_flush.done():
            return
        self._pending_flush = asyncio.create_task(self._delayed_flush(delay_ms / 1000))

    async def _delayed_flush(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if not self._canceled:
                await self._flush_fn()
        except asyncio.CancelledError:
            pass

    async def wait_for_flush(self) -> None:
        if self._pending_flush:
            try:
                await self._pending_flush
            except asyncio.CancelledError:
                pass


class StreamingCardController:
    """流式卡片状态机控制器。"""

    def __init__(
        self,
        cardkit_client: CardKitClient,
        chat_id: str,
        receive_id: str,
        receive_id_type: str = "open_id",
        reply_to_message_id: str | None = None,
        show_tool_use: bool = True,
        session_key: str = "",
        precreated_cards: dict[str, str] | None = None,
    ):
        self._cardkit = cardkit_client
        self._chat_id = chat_id
        self._receive_id = receive_id
        self._receive_id_type = receive_id_type
        self._reply_to_message_id = reply_to_message_id
        self._show_tool_use = show_tool_use
        self._session_key = session_key
        self._precreated_cards = precreated_cards or {}
        self._current_content_type: str | None = None  # 当前卡片的内容类型

        self._phase: CardPhase = CardPhase.IDLE
        self._card_id: str | None = None
        self._message_id: str | None = None
        self._sequence = 0
        self._original_card_id: str | None = None
        self._reasoning_card_id: str | None = None
        self._reasoning_message_id: str | None = None
        self._reasoning_sequence = 0

        self._accumulated_text = ""
        self._streaming_prefix = ""
        self._last_partial_text = ""
        self._last_flushed_text = ""
        self._completed_text = ""
        # Each contiguous run of streamed text is its own card. ``_is_text_phase``
        # tracks whether a text card is currently open; ``_text_interrupted`` marks
        # that a tool/reasoning event closed it, so the next ``on_partial_reply``
        # starts a fresh card instead of growing the previous segment. Mirrors the
        # ``_is_reasoning_phase`` / ``_reasoning_interrupted`` pair below.
        self._is_text_phase = False
        self._text_interrupted = False

        self._reasoning_text = ""
        self._reasoning_start_time: float | None = None
        self._is_reasoning_phase = False
        self._reasoning_interrupted = False
        self._has_auxiliary_cards = False
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._tool_order: list[str] = []
        self._tool_card_ids: dict[str, str | None] = {}
        self._tool_message_ids: dict[str, str | None] = {}
        self._tool_sequences: dict[str, int] = {}

        self._dispatch_start_time = time.time()

        self._op_queue: asyncio.Queue[
            tuple[Callable[[], Coroutine[Any, Any, None]], asyncio.Future[None]] | None
        ] = asyncio.Queue()
        self._op_worker_task: asyncio.Task | None = None
        self._op_running = False

    @property
    def message_id(self) -> str | None:
        return self._message_id

    @property
    def is_terminal_phase(self) -> bool:
        return self._phase in {
            CardPhase.COMPLETED,
            CardPhase.ABORTED,
            CardPhase.TERMINATED,
            CardPhase.CREATION_FAILED,
        }

    def _transition(self, to: CardPhase, source: str) -> bool:
        """执行状态转换。"""
        from_ = self._phase
        if from_ == to:
            return False
        if to not in PHASE_TRANSITIONS.get(from_, set()):
            logger.warning("Phase transition rejected: %s -> %s from %s", from_, to, source)
            return False
        self._phase = to
        logger.info("Card phase: %s -> %s (%s)", from_, to, source)
        return True

    def _get_card_id_for_content_type(self, content_type: str) -> str | None:
        """返回给定内容类型对应的预创建 card_id。"""
        return self._precreated_cards.get(content_type)

    async def on_partial_reply(self, text: str) -> None:
        """处理流式部分回复。"""
        if self.is_terminal_phase:
            return
        if not text.strip():
            return

        self._current_content_type = "rich_text"
        if self._is_reasoning_phase:
            await self._finalize_reasoning_stream()
        self._capture_reasoning_time()
        # A tool/reasoning event closed the previous text card: this delta begins a
        # new segment, so rotate to a fresh card instead of appending to the old one.
        if self._text_interrupted:
            await self._rotate_text_card()
        self._is_text_phase = True
        # 检测回复边界：文本长度缩短意味着新回复开始
        if self._last_partial_text and len(text) < len(self._last_partial_text):
            self._streaming_prefix += ("\n\n" if self._streaming_prefix else "") + self._last_partial_text

        self._last_partial_text = text
        self._accumulated_text = (
            (self._streaming_prefix + "\n\n" + text) if self._streaming_prefix else text
        )

        await self._ensure_card_created()
        if self.is_terminal_phase:
            return
        if not self._message_id:
            return
        await self._throttled_card_update()

    async def on_reasoning_stream(self, text: str) -> None:
        """处理推理流。"""
        if self.is_terminal_phase:
            return
        if not text.strip():
            return

        await self._close_active_text_segment()
        if self._reasoning_interrupted:
            self._reasoning_text = ""
            self._reasoning_card_id = None
            self._reasoning_message_id = None
            self._reasoning_sequence = 0
            self._reasoning_interrupted = False

        self._current_content_type = "thinking"
        self._has_auxiliary_cards = True
        if not self._reasoning_start_time:
            self._reasoning_start_time = time.time()
        self._is_reasoning_phase = True
        self._reasoning_text = text

        await self._update_reasoning_card()

    async def on_tool_start(
        self,
        name: str,
        *,
        tool_call_id: str | None = None,
        arguments: dict[str, Any] | None = None,
        category: str | None = None,
    ) -> None:
        """工具开始执行。"""
        if self.is_terminal_phase:
            return
        if not self._show_tool_use:
            return

        self._current_content_type = "tool_call"
        self._has_auxiliary_cards = True
        await self._close_active_text_segment()
        if self._is_reasoning_phase:
            await self._finalize_reasoning_stream()
            self._is_reasoning_phase = False
            self._reasoning_interrupted = True
        call_id = tool_call_id or f"tool_{len(self._tool_order) + 1}"
        if call_id not in self._tool_calls:
            self._tool_order.append(call_id)
        self._tool_calls[call_id] = {
            "id": call_id,
            "name": name,
            "category": category,
            "input": arguments or {},
            "status": "running",
        }

        await self._update_tool_card(call_id)

    async def on_tool_result(
        self,
        tool_call_id: str,
        *,
        name: str | None = None,
        preview: str | None = None,
        is_error: bool = False,
    ) -> None:
        """工具完成执行。"""
        if self.is_terminal_phase:
            return
        if not self._show_tool_use:
            return

        self._current_content_type = "tool_call"
        self._has_auxiliary_cards = True
        await self._close_active_text_segment()
        if self._is_reasoning_phase:
            await self._finalize_reasoning_stream()
            self._is_reasoning_phase = False
            self._reasoning_interrupted = True
        effective_is_error = is_error or _preview_indicates_error(preview)
        call_id = tool_call_id or f"tool_{len(self._tool_order) + 1}"
        existing = self._tool_calls.get(call_id)
        if existing is None:
            self._tool_order.append(call_id)
            existing = {
                "id": call_id,
                "name": name or "tool",
                "category": None,
                "input": {},
            }
        existing = {
            **existing,
            "name": name or existing.get("name") or "tool",
            "status": "error" if effective_is_error else "completed",
            "result": {
                "output": preview or "",
                "is_error": effective_is_error,
            },
        }
        self._tool_calls[call_id] = existing
        await self._update_tool_card(call_id)

    async def on_approval_request(self, payload: dict[str, Any]) -> None:
        """发送审批卡片（独立消息）。发送失败必须抛出——service 侧会落
        approval.delivery_failed 事件，等待仍继续（Web 端可 resolve）。"""
        card = build_approval_card(payload)
        message_id = await asyncio.to_thread(
            self._cardkit.send_card_json,
            card=card,
            receive_id=self._receive_id,
            receive_id_type=self._receive_id_type,
            reply_to_message_id=self._reply_to_message_id,
        )
        direct_error = getattr(self._cardkit, "last_error", None)
        if not message_id:
            card_id = await asyncio.to_thread(self._cardkit.create_card, card)
            create_error = getattr(self._cardkit, "last_error", None)
            if card_id:
                message_id = await asyncio.to_thread(
                    self._cardkit.send_card_by_card_id,
                    card_id=card_id,
                    receive_id=self._receive_id,
                    receive_id_type=self._receive_id_type,
                    reply_to_message_id=self._reply_to_message_id,
                )
            fallback_error = getattr(self._cardkit, "last_error", None)
            if message_id:
                logger.info(
                    "StreamingCardController: approval card sent via card_id "
                    "approval_id=%s message_id=%s card_id=%s direct_error=%s",
                    payload.get("approval_id"),
                    message_id,
                    card_id,
                    direct_error,
                )
        if not message_id:
            details = {
                "direct_error": direct_error,
                "create_error": locals().get("create_error"),
                "fallback_error": locals().get("fallback_error"),
            }
            raise RuntimeError(
                "approval card send failed "
                f"approval_id={payload.get('approval_id')} details={details}"
            )
        logger.info(
            "StreamingCardController: approval card sent approval_id=%s message_id=%s",
            payload.get("approval_id"),
            message_id,
        )

    async def on_user_question(self, pending: dict[str, Any]) -> None:
        """发送 ask_user_question 的交互卡片（独立消息，不影响流式卡片）。

        ``pending`` 是 service 持久化的 pending_user_question payload。
        发送失败必须抛出 —— service 侧会落 user_question.delivery_failed
        事件并打 ERROR 日志，用户绝不能在没看到问题的情况下被等待。
        """
        card = build_ask_user_card(pending)
        message_id = await asyncio.to_thread(
            self._cardkit.send_card_json,
            card=card,
            receive_id=self._receive_id,
            receive_id_type=self._receive_id_type,
        )
        if not message_id:
            raise RuntimeError(
                f"ask_user card send failed question_id={pending.get('question_id')}"
            )
        logger.info(
            "StreamingCardController: ask_user card sent question_id=%s message_id=%s",
            pending.get("question_id"),
            message_id,
        )

    async def on_idle(self) -> None:
        """流式结束，发送终态卡片。"""
        # CREATION_FAILED is *not* skipped here: a text card that failed to send
        # mid-stream still gets one authoritative final delivery attempt (and a
        # raised error on failure, so the manager can fall back to plain text).
        if self._phase in {CardPhase.COMPLETED, CardPhase.ABORTED, CardPhase.TERMINATED}:
            return

        elapsed_ms = int((time.time() - self._dispatch_start_time) * 1000)

        async def do_finalize() -> None:
            display_text = self._completed_text or self._accumulated_text or "已完成"
            reasoning_elapsed_ms = (
                int((time.time() - self._reasoning_start_time) * 1000)
                if self._reasoning_start_time
                else None
            )

            card = build_complete_card(
                text=display_text,
                show_tool_use=self._show_tool_use,
                reasoning_text=self._reasoning_text or None,
                reasoning_elapsed_ms=reasoning_elapsed_ms,
                elapsed_ms=elapsed_ms,
            )

            if self._card_id:
                self._sequence += 1
                streaming_mode_disabled = await asyncio.to_thread(
                    self._cardkit.set_streaming_mode,
                    self._card_id,
                    False,
                    self._sequence,
                )
                self._sequence += 1
                updated = await asyncio.to_thread(
                    self._cardkit.update_card,
                    self._card_id,
                    card,
                    self._sequence,
                )
                delivered = bool(streaming_mode_disabled) and bool(updated)
            elif self._message_id:
                delivered = bool(
                    await asyncio.to_thread(self._cardkit.patch_message, self._message_id, card)
                )
            else:
                card_id, message_id = await self._send_card_json_or_create_card(card)
                self._card_id = card_id
                self._message_id = message_id
                self._sequence = 1 if card_id else 0
                delivered = bool(message_id)

            if not delivered or not self._message_id:
                raise RuntimeError("final card delivery failed")

            self._transition(CardPhase.COMPLETED, "on_idle")
            self._stop_queue_worker()
            logger.info("StreamingCardController: reply completed, card finalized")

        await self._enqueue(do_finalize)

    async def abort_card(self) -> None:
        """中止流式卡片。"""
        elapsed_ms = int((time.time() - self._dispatch_start_time) * 1000)
        if not self._transition(CardPhase.ABORTED, "abort_card"):
            return

        async def do_abort() -> None:
            display_text = self._accumulated_text or "Aborted."
            card = build_complete_card(
                text=display_text,
                show_tool_use=self._show_tool_use,
                reasoning_text=self._reasoning_text or None,
                elapsed_ms=elapsed_ms,
                is_aborted=True,
            )

            if self._card_id:
                await asyncio.to_thread(self._cardkit.set_streaming_mode, self._card_id, False, self._sequence)
                await asyncio.to_thread(self._cardkit.update_card, self._card_id, card, self._sequence + 1)
            elif self._message_id:
                await asyncio.to_thread(self._cardkit.patch_message, self._message_id, card)

            self._stop_queue_worker()

        await self._enqueue(do_abort)

    # ---- Internal ----

    def _capture_reasoning_time(self) -> None:
        if not self._reasoning_start_time:
            self._reasoning_start_time = time.time()
        if self._is_reasoning_phase:
            self._is_reasoning_phase = False
            self._reasoning_interrupted = True

    async def _finalize_reasoning_stream(self) -> None:
        if not self._reasoning_card_id:
            return

        async def do_finalize() -> None:
            if not self._reasoning_card_id:
                return
            self._reasoning_sequence += 1
            await asyncio.to_thread(
                self._cardkit.set_streaming_mode,
                self._reasoning_card_id,
                False,
                self._reasoning_sequence,
            )

        await self._enqueue(do_finalize)

    async def _close_active_text_segment(self) -> None:
        """Finalize the open text card (if any) so the next text delta opens a new
        one. Called when a tool/reasoning event interrupts a run of streamed text."""
        if not self._is_text_phase:
            return
        self._is_text_phase = False
        self._text_interrupted = True
        await self._finalize_text_stream()

    async def _finalize_text_stream(self) -> None:
        if not self._card_id:
            # IM/message-based text cards have no streaming mode to disable; the
            # last patch already shows the segment's final text.
            return
        card_id = self._card_id

        async def do_finalize() -> None:
            self._sequence += 1
            await asyncio.to_thread(
                self._cardkit.set_streaming_mode,
                card_id,
                False,
                self._sequence,
            )

        await self._enqueue(do_finalize)

    async def _rotate_text_card(self) -> None:
        """Reset text-card state so ``_ensure_card_created`` builds a fresh card for
        the next segment. The previous card was already finalized in
        ``_close_active_text_segment``."""
        self._card_id = None
        self._message_id = None
        self._original_card_id = None
        self._sequence = 0
        self._accumulated_text = ""
        self._streaming_prefix = ""
        self._last_partial_text = ""
        self._last_flushed_text = ""
        self._text_interrupted = False
        # Re-arm the phase machine for another create cycle. Set directly rather
        # than via ``_transition`` because STREAMING→IDLE is not a normal edge.
        if self._phase in {CardPhase.STREAMING, CardPhase.CREATING}:
            self._phase = CardPhase.IDLE

    async def _ensure_card_created(self) -> None:
        """确保卡片已创建并发送。"""
        if self._message_id or self._phase == CardPhase.CREATION_FAILED:
            return
        if not self._transition(CardPhase.CREATING, "ensure_card_created"):
            return

        async def do_create() -> None:
            await self._do_create_card()

        await self._enqueue(do_create)

    async def _do_create_card(self) -> None:
        """执行卡片创建。"""
        try:
            content_type = self._current_content_type or "rich_text"
            precreated_id = self._get_card_id_for_content_type(content_type)

            if precreated_id:
                self._card_id = precreated_id
                self._original_card_id = precreated_id
                self._sequence = 1
                message_id = await asyncio.to_thread(
                    self._cardkit.send_card_by_card_id,
                    card_id=precreated_id,
                    receive_id=self._receive_id,
                    receive_id_type=self._receive_id_type,
                )
                if message_id:
                    self._message_id = message_id
                    self._transition(CardPhase.STREAMING, "create_card_ok")
                    logger.info("CardKit precreated card sent: card_id=%s message_id=%s", precreated_id, message_id)
                    return
                raise Exception("Precreated card_id send failed")

            fallback_card = build_streaming_card(
                self._build_display_text(),
                show_tool_use=self._show_tool_use,
                tool_calls=self._ordered_tool_calls(),
                session_id=self._session_key or None,
            )
            card_id, message_id = await self._send_card_json_or_create_card(fallback_card)
            self._card_id = card_id
            self._sequence = 1 if card_id else 0
            if not message_id:
                raise Exception("IM fallback card send failed")
            self._message_id = message_id
            self._transition(CardPhase.STREAMING, "im_fallback")
            logger.info("IM fallback card sent: message_id=%s", message_id)
            return

        except Exception as e:
            logger.warning("CardKit flow failed, falling back to IM card: %s", e)
            self._card_id = None
            self._original_card_id = None

            fallback_card = build_streaming_card(
                "",
                show_tool_use=False,
                session_id=self._session_key or None,
            )
            card_id, message_id = await self._send_card_json_or_create_card(fallback_card)
            self._card_id = card_id
            self._sequence = 1 if card_id else 0
            if not message_id:
                self._transition(CardPhase.CREATION_FAILED, "im_fallback_failed")
                logger.warning("IM fallback card failed without message_id")
                return
            self._message_id = message_id
            self._transition(CardPhase.STREAMING, "im_fallback")
            logger.info("IM fallback card sent: message_id=%s", message_id)

    async def _perform_flush(self) -> None:
        """执行一次卡片更新。"""
        if not self._message_id or self.is_terminal_phase:
            return
        if self._card_id is None and self._original_card_id is not None:
            # CardKit 流式被禁用，但还有 originalCardKitCardId，跳过中间态更新
            return

        display = self._build_display_text()
        if self._card_id:
            if display != self._last_flushed_text:
                self._sequence += 1
                await asyncio.to_thread(
                    self._cardkit.stream_card_content,
                    self._card_id,
                    STREAMING_ELEMENT_ID,
                    display,
                    self._sequence,
                )
                self._last_flushed_text = display
        elif self._message_id:
            card = build_streaming_card(
                display,
                show_tool_use=False,
                session_id=self._session_key or None,
            )
            await asyncio.to_thread(self._cardkit.patch_message, self._message_id, card)

    def _build_display_text(self) -> str:
        if self._is_reasoning_phase and self._reasoning_text:
            reasoning_display = f"💭 **Thinking...**\n\n{self._reasoning_text}"
            return (self._accumulated_text + "\n\n" + reasoning_display) if self._accumulated_text else reasoning_display
        return self._accumulated_text

    def _ordered_tool_calls(self) -> list[dict[str, Any]]:
        return [self._tool_calls[call_id] for call_id in self._tool_order if call_id in self._tool_calls]

    async def _update_reasoning_card(self) -> None:
        async def do_update() -> None:
            card = build_thinking_card(self._reasoning_text)
            if self._reasoning_card_id:
                self._reasoning_sequence += 1
                await asyncio.to_thread(
                    self._cardkit.stream_card_content,
                    self._reasoning_card_id,
                    STREAMING_ELEMENT_ID,
                    self._reasoning_text,
                    self._reasoning_sequence,
                )
                return
            if self._reasoning_message_id:
                await asyncio.to_thread(self._cardkit.patch_message, self._reasoning_message_id, card)
                return
            card_id, message_id = await self._create_standalone_card(card)
            self._reasoning_card_id = card_id
            self._reasoning_message_id = message_id
            self._reasoning_sequence = 1 if card_id else 0

        await self._enqueue(do_update)

    async def _update_tool_card(self, call_id: str) -> None:
        async def do_update() -> None:
            tool_call = self._tool_calls.get(call_id)
            if tool_call is None:
                return
            card = build_tool_call_card(tool_call)
            card_id = self._tool_card_ids.get(call_id)
            message_id = self._tool_message_ids.get(call_id)
            if card_id:
                sequence = self._tool_sequences.get(call_id, 1) + 1
                self._tool_sequences[call_id] = sequence
                await asyncio.to_thread(self._cardkit.update_card, card_id, card, sequence)
                return
            if message_id:
                await asyncio.to_thread(self._cardkit.patch_message, message_id, card)
                return
            card_id, message_id = await self._create_standalone_card(card)
            self._tool_card_ids[call_id] = card_id
            self._tool_message_ids[call_id] = message_id
            self._tool_sequences[call_id] = 1 if card_id else 0

        await self._enqueue(do_update)

    async def _create_standalone_card(self, card: dict[str, Any]) -> tuple[str | None, str | None]:
        try:
            card_id = await asyncio.to_thread(self._cardkit.create_card, card)
            if card_id:
                message_id = await asyncio.to_thread(
                    self._cardkit.send_card_by_card_id,
                    card_id=card_id,
                    receive_id=self._receive_id,
                    receive_id_type=self._receive_id_type,
                    reply_to_message_id=self._reply_to_message_id,
                )
                return card_id, message_id
        except Exception:
            logger.exception("standalone CardKit card failed, falling back to IM card")
        message_id = await asyncio.to_thread(
            self._cardkit.send_card_json,
            card=card,
            receive_id=self._receive_id,
            receive_id_type=self._receive_id_type,
            reply_to_message_id=self._reply_to_message_id,
        )
        return None, message_id

    async def _send_card_json_or_create_card(self, card: dict[str, Any]) -> tuple[str | None, str | None]:
        message_id = await asyncio.to_thread(
            self._cardkit.send_card_json,
            card=card,
            receive_id=self._receive_id,
            receive_id_type=self._receive_id_type,
            reply_to_message_id=self._reply_to_message_id,
        )
        if message_id:
            return None, message_id
        return await self._create_standalone_card(card)

    async def _throttled_card_update(self) -> None:
        delay_ms = THROTTLE_CARDKIT_MS if self._card_id else THROTTLE_PATCH_MS

        async def do_flush() -> None:
            # throttled_update 会内部 sleep，这里直接入队
            await asyncio.sleep(delay_ms / 1000)
            await self._perform_flush()

        await self._enqueue(do_flush)

    def _start_queue_worker(self) -> None:
        if self._op_worker_task is None:
            self._op_running = True
            self._op_worker_task = asyncio.create_task(self._run_op_worker())

    async def _run_op_worker(self) -> None:
        """单 worker 循环：从队列取任务、执行、完成，再取下一个。"""
        while self._op_running:
            item = None
            try:
                item = await self._op_queue.get()
                if item is None:
                    self._op_queue.task_done()
                    break
                op, future = item
                await op()
                if not future.done():
                    future.set_result(None)
            except asyncio.CancelledError:
                if item is not None:
                    _, future = item
                    if not future.done():
                        future.set_exception(asyncio.CancelledError())
                break
            except Exception as exc:
                logger.exception("FIFO op worker error")
                if item is not None:
                    _, future = item
                    if not future.done():
                        future.set_exception(exc)
            finally:
                if item is not None:
                    self._op_queue.task_done()

    async def _enqueue(self, op: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """将操作入队并等待队列处理完毕（串行化点）。"""
        self._start_queue_worker()
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await self._op_queue.put((op, future))
        await future

    def _stop_queue_worker(self) -> None:
        if self._op_worker_task is None:
            return
        self._op_running = False
        if asyncio.current_task() is self._op_worker_task:
            return
        self._op_queue.put_nowait(None)

    async def _wait_queue_empty(self) -> None:
        """等待队列排空。"""
        await self._op_queue.join()
