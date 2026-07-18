"""Feishu Channel — WebSocket long connection via lark-oapi."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import httpx
from typing import TYPE_CHECKING, Any

from doyoutrade.assistant.channels.base import (
    AudioContent,
    BaseChannel,
    CardContent,
    ChannelAgentRequest,
    ChannelDeliveryHandle,
    ChannelDeliveryReceipt,
    ContentPart,
    FileContent,
    ImageContent,
    LifecycleReply,
    TextContent,
)

if TYPE_CHECKING:
    from doyoutrade.assistant.service import AssistantService
    from doyoutrade.assistant.channels.config import FeishuChannelConfig

logger = logging.getLogger(__name__)

# WebSocket reconnection constants
_FEISHU_WS_INITIAL_RETRY_DELAY = 1.0
_FEISHU_WS_MAX_RETRY_DELAY = 60.0
_FEISHU_WS_BACKOFF_FACTOR = 2.0
_FEISHU_TENANT_TOKEN_TTL = 7200

# Reaction emoji stamped on an inbound message the instant it arrives, as an
# immediate "received, working on it" acknowledgement. Must be a valid Feishu
# emoji_type (see the reaction 表情文案说明 doc); "Typing" renders the typing
# indicator face.
_FEISHU_TYPING_EMOJI = "Typing"


class _EventLoopProxy:
    """Resolve ``lark_oapi.ws.client.loop`` to the calling thread's loop.

    lark-oapi's ws client accesses ``loop.run_until_complete()`` from the
    WebSocket thread. Patching the module-level attribute lets each thread
    transparently use its own event loop without the SDK knowing about it.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return getattr(asyncio.get_running_loop(), name)
        except RuntimeError:
            return getattr(asyncio.get_event_loop(), name)


class FeishuChannel(BaseChannel):
    """飞书 Channel — WebSocket 长连接。"""

    channel_type = "feishu"

    def __init__(
        self,
        assistant_service: "AssistantService | None" = None,
        channel_id: str | None = None,
        app_id: str = "",
        app_secret: str = "",
        encrypt_key: str = "",
        verification_token: str = "",
        domain: str = "feishu",
        trade_approval_gate: Any = None,
    ):
        super().__init__(assistant_service, channel_id=channel_id)
        self.app_id = app_id
        self.app_secret = app_secret
        self.encrypt_key = encrypt_key
        self.verification_token = verification_token
        self.domain = domain
        self._base_url = "https://open.larksuite.com" if domain == "lark" else "https://open.feishu.cn"
        # Execution-side trade-approval gate (QueuedApprovalGate). Distinct from
        # the assistant tool-call ApprovalBroker on ``assistant_service`` — the
        # ``trade_approval_resolve`` card action routes here. May be None if the
        # runtime had no gate to inject; the action branch degrades loudly.
        self._trade_approval_gate = trade_approval_gate
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._manager: Any = None  # set by ChannelManager
        self._asyncio_loop: asyncio.AbstractEventLoop | None = None
        self._lark_client: Any = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._stop_event = threading.Event()
        self._precreated_cards: dict[str, str] = {}  # content_type -> created_card_id
        self._bot_open_id: str = ""  # lazily resolved via bot/v3/info; used to strip the bot's own @
        self._tenant_access_token: str | None = None
        self._tenant_token_expires_at: float = 0.0
        self._tenant_token_lock = threading.Lock()

    @classmethod
    def from_config(
        cls,
        assistant_service: "AssistantService",
        config: "FeishuChannelConfig",
    ) -> "FeishuChannel":
        channel = cls(
            assistant_service=assistant_service,
            channel_id=getattr(config, "channel_id", None),
            app_id=config.app_id,
            app_secret=config.app_secret,
            encrypt_key=config.encrypt_key,
            verification_token=config.verification_token,
            domain=config.domain,
        )
        if config.has_any_card_id():
            channel._precreate_cards_from_config(config)
        return channel

    def _precreate_cards_from_config(self, config: "FeishuChannelConfig") -> None:
        """启动时预创建卡片。

        如果任何一张卡片创建失败，channel 无法启动。
        """
        from .card.cardkit import CardKitClient

        cardkit = CardKitClient(app_id=self.app_id, app_secret=self.app_secret, domain=self.domain)

        card_configs = {}
        for content_type in ["thinking", "tool_call", "rich_text"]:
            card_id = config.get_card_id_for_content_type(content_type)
            if card_id:
                card_configs[content_type] = card_id

        if not card_configs:
            return

        try:
            created = cardkit.precreate_cards(card_configs)
            self._precreated_cards = created
            logger.info("FeishuChannel: precreated cards: %s", created)
        except Exception as e:
            logger.error("FeishuChannel: card precreation failed: %s", e)
            raise

    def _get_tenant_access_token(self) -> str | None:
        with self._tenant_token_lock:
            now = time.time()
            if self._tenant_access_token and now < self._tenant_token_expires_at:
                return self._tenant_access_token

            url = f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal"
            payload = {"app_id": self.app_id, "app_secret": self.app_secret}
            try:
                with httpx.Client(timeout=15.0) as client:
                    resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning(
                    "Feishu: tenant token fetch error type=%s msg=%s",
                    type(exc).__name__,
                    exc,
                )
                return None

            if data.get("code") != 0:
                logger.warning(
                    "Feishu: tenant token fetch failed code=%s msg=%s",
                    data.get("code"),
                    data.get("msg"),
                )
                return None

            self._tenant_access_token = str(data.get("tenant_access_token") or "") or None
            self._tenant_token_expires_at = now + _FEISHU_TENANT_TOKEN_TTL - 60
            return self._tenant_access_token

    def _tenant_headers(self) -> dict[str, str]:
        token = self._get_tenant_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    @staticmethod
    def _normalize_mentions(raw_mentions: Any) -> list[dict[str, Any]]:
        mentions: list[dict[str, Any]] = []
        for mention in raw_mentions or []:
            if isinstance(mention, dict):
                mention_id = mention.get("id") or {}
                mentions.append(
                    {
                        "key": mention.get("key") or "",
                        "open_id": (mention_id.get("open_id") or "") if isinstance(mention_id, dict) else "",
                        "name": mention.get("name") or "",
                    }
                )
                continue
            mention_id = getattr(mention, "id", {}) or {}
            mentions.append(
                {
                    "key": getattr(mention, "key", "") or "",
                    "open_id": getattr(mention_id, "open_id", "") or "",
                    "name": getattr(mention, "name", "") or "",
                }
            )
        return mentions

    @staticmethod
    def _extract_reply_target(message_id: str, root_id: str, parent_id: str) -> tuple[str, str]:
        if parent_id and parent_id != message_id:
            relation = "root" if root_id and root_id == parent_id else "parent"
            return parent_id, relation
        if root_id and root_id != message_id:
            return root_id, "root"
        return "", ""

    def _fetch_message_brief(self, message_id: str) -> dict[str, str] | None:
        if not message_id:
            return None
        url = f"{self._base_url}/open-apis/im/v1/messages/{message_id}"
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.get(url, headers=self._tenant_headers())
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            logger.warning(
                "Feishu: reply-target fetch error type=%s msg=%s target_message_id=%s",
                type(exc).__name__,
                exc,
                message_id,
            )
            return None

        if body.get("code") != 0:
            logger.warning(
                "Feishu: reply-target fetch failed code=%s msg=%s target_message_id=%s",
                body.get("code"),
                body.get("msg"),
                message_id,
            )
            return None

        data = body.get("data") or {}
        if isinstance(data.get("message"), dict):
            message = data.get("message") or {}
        elif isinstance(data.get("item"), dict):
            message = data.get("item") or {}
        elif isinstance(data.get("items"), list) and data.get("items"):
            message = data["items"][0] or {}
        else:
            message = data if isinstance(data, dict) else {}
        if not isinstance(message, dict):
            logger.warning(
                "Feishu: reply-target fetch returned unexpected shape target_message_id=%s data_type=%s",
                message_id,
                type(data).__name__,
            )
            return None

        from doyoutrade.assistant.channels.feishu.utils import parse_feishu_message_content

        msg_type = str(message.get("msg_type") or "text")
        body_payload = message.get("body") if isinstance(message.get("body"), dict) else {}
        content = (
            message.get("content")
            or body_payload.get("content")
            or "{}"
        )
        mentions = self._normalize_mentions(message.get("mentions") or [])
        parts = parse_feishu_message_content(msg_type, content, mentions=mentions, bot_open_id="")
        text = "\n".join(part.text for part in parts if isinstance(part, TextContent)).strip()
        if not text:
            if msg_type in {"image", "file", "audio"}:
                text = f"[{msg_type} message]"
            else:
                text = str(content)
        if len(text) > 500:
            text = text[:497] + "..."
        return {
            "message_id": str(message.get("message_id") or message_id),
            "msg_type": msg_type,
            "content": text,
        }

    async def start(self) -> None:
        """启动 WebSocket 长连接。"""
        logger.info("FeishuChannel.start: channel_id=%s app_id=%s", self.channel_id, self.app_id)
        import lark_oapi as lark
        self._closed = False
        self._stop_event.clear()

        event_handler = (
            lark.EventDispatcherHandler.builder(
                self.encrypt_key,
                self.verification_token,
            )
            .register_p2_im_message_receive_v1(self._on_message_sync)
            .register_p2_im_message_reaction_created_v1(lambda _evt: None)
            .register_p2_im_message_reaction_deleted_v1(lambda _evt: None)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._on_p2p_chat_entered)
            .register_p2_card_action_trigger(self._on_card_action_trigger)
            .register_p2_im_message_message_read_v1(lambda _evt: None)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
            domain=lark.LARK_DOMAIN if self.domain == "lark" else lark.FEISHU_DOMAIN,
            auto_reconnect=False,
        )
        from lark_oapi.client import ClientBuilder

        self._lark_client = (
            ClientBuilder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .log_level(lark.LogLevel.INFO)
            .domain(lark.LARK_DOMAIN if self.domain == "lark" else lark.FEISHU_DOMAIN)
            .build()
        )
        self._asyncio_loop = asyncio.get_running_loop()
        self._ws_thread = threading.Thread(
            target=self._run_ws_forever, daemon=True
        )
        self._ws_thread.start()

    def _run_ws_forever(self) -> None:
        """Run WebSocket with exponential-backoff reconnection in a dedicated thread."""
        import lark_oapi.ws.client as _ws_mod

        retry_delay = _FEISHU_WS_INITIAL_RETRY_DELAY

        while not getattr(self, "_closed", False):
            self._ws_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ws_loop)
            connection_started = False
            try:
                # lark_oapi accesses _ws_mod.loop internally; our proxy resolves
                # to whatever loop is running in this thread.
                _ws_mod.loop = _EventLoopProxy()

                async def _drive() -> None:
                    nonlocal connection_started
                    before_connect_tasks = set(asyncio.all_tasks(self._ws_loop))
                    await self._ws_client._connect()
                    connection_started = True
                    after_connect_tasks = set(asyncio.all_tasks(self._ws_loop))
                    receive_tasks = [
                        task for task in after_connect_tasks - before_connect_tasks
                        if not task.done()
                    ]
                    if not receive_tasks:
                        receive_tasks.append(
                            self._ws_loop.create_task(self._ws_client._receive_message_loop())
                        )
                    ping_task = self._ws_loop.create_task(self._ws_client._ping_loop())
                    done, _pending = await asyncio.wait(
                        [*receive_tasks, ping_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in done:
                        task.result()

                self._ws_loop.run_until_complete(_drive())
            except RuntimeError as e:
                if "Event loop stopped" in str(e):
                    logger.debug("feishu WebSocket stopped normally: %s", e)
                else:
                    logger.exception("feishu WebSocket RuntimeError: %s", e)
            except Exception:
                logger.exception("feishu WebSocket thread failed")
            finally:
                if self._ws_loop and not self._ws_loop.is_closed():
                    try:
                        if self._ws_client and hasattr(self._ws_client, "_disconnect"):
                            try:
                                self._ws_loop.run_until_complete(
                                    self._ws_client._disconnect(),
                                )
                            except Exception:
                                logger.debug("feishu ws disconnect failed", exc_info=True)
                        pending = [
                            t for t in asyncio.all_tasks(self._ws_loop)
                            if not t.done()
                        ]
                        for task in pending:
                            task.cancel()
                        if pending:
                            self._ws_loop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True),
                            )
                        self._ws_loop.run_until_complete(
                            self._ws_loop.shutdown_asyncgens(),
                        )
                    except Exception:
                        logger.debug("feishu ws cleanup failed", exc_info=True)
                try:
                    if self._ws_loop and not self._ws_loop.is_closed():
                        self._ws_loop.close()
                except Exception:
                    logger.debug("feishu ws loop close failed", exc_info=True)
                self._ws_loop = None

            # Reconnect with backoff unless closed
            if getattr(self, "_closed", False):
                break
            if connection_started:
                retry_delay = _FEISHU_WS_INITIAL_RETRY_DELAY
            else:
                logger.info(
                    "feishu WebSocket reconnecting in %.1fs...",
                    retry_delay,
                )
                if self._stop_event.wait(retry_delay):
                    break
                retry_delay = min(
                    retry_delay * _FEISHU_WS_BACKOFF_FACTOR,
                    _FEISHU_WS_MAX_RETRY_DELAY,
                )

    async def stop(self) -> None:
        """停止 WebSocket 连接。"""
        logger.info("FeishuChannel.stop: channel_id=%s", self.channel_id)
        self._closed = True  # signal the reconnect loop to exit
        self._stop_event.set()
        if self._ws_loop and not self._ws_loop.is_closed():
            try:
                self._ws_loop.call_soon_threadsafe(
                    self._ws_loop.stop,
                )
            except Exception:
                pass
        if self._ws_client:
            try:
                self._ws_client.stop()
            except Exception:
                logger.debug("Feishu ws_client.stop() failed", exc_info=True)
        if (
            self._ws_thread
            and self._ws_thread.is_alive()
            and self._ws_thread is not threading.current_thread()
        ):
            self._ws_thread.join(timeout=2.0)
            if self._ws_thread.is_alive():
                logger.warning("Feishu WebSocket thread did not stop within timeout")
        if self._ws_client:
            self._ws_client = None

    def _on_message_sync(self, data: Any) -> None:
        logger.debug("Feishu: received raw event data type=%s", type(data).__name__)
        payload = self._parse_event(data)
        if payload is None:
            logger.warning("Feishu: _parse_event returned None — check getattr vs dict access")
            return
        if self._asyncio_loop and self._manager:
            # Stamp a Typing reaction on the original message the instant it
            # arrives, before the (potentially slow) agent turn runs. Fire it on
            # the same loop as enqueue; it is best-effort and never blocks the
            # message from being processed.
            message_id = (payload.get("meta") or {}).get("feishu_message_id", "")
            if message_id:
                asyncio.run_coroutine_threadsafe(
                    self._add_typing_reaction(message_id),
                    self._asyncio_loop,
                )
            asyncio.run_coroutine_threadsafe(
                self._manager.enqueue(self.channel_id, payload),
                self._asyncio_loop,
            )
            logger.info(
                "Feishu: message enqueued session_id=%s content=%s",
                payload.get("session_id"),
                payload.get("content", "")[:50],
            )
        else:
            logger.warning(
                "Feishu: no asyncio_loop=%s manager=%s — message dropped",
                self._asyncio_loop,
                self._manager,
            )

    async def _add_typing_reaction(self, message_id: str) -> None:
        """Add a "Typing" reaction to the inbound message as instant feedback.

        Best-effort: a failure here must never block message processing, but it
        must stay visible. Per the error-visibility rules we distinguish the
        three failure modes — no client / API non-zero code / request exception —
        with the exception type, message and ``message_id`` in the log, never a
        silent swallow.

        Reference: POST /open-apis/im/v1/messages/:message_id/reactions
        """
        if self._lark_client is None or not message_id:
            logger.warning(
                "Feishu: skip typing reaction - lark_client=%s message_id=%r",
                self._lark_client,
                message_id,
            )
            return

        from lark_oapi.api.im.v1.model import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        req = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(
                    Emoji.builder().emoji_type(_FEISHU_TYPING_EMOJI).build()
                )
                .build()
            )
            .build()
        )
        try:
            resp = await self._lark_client.im.v1.message_reaction.acreate(req)
        except Exception as e:
            logger.warning(
                "Feishu: typing reaction request error type=%s msg=%s message_id=%s",
                type(e).__name__,
                e,
                message_id,
            )
            return
        if resp.code != 0:
            logger.warning(
                "Feishu: typing reaction failed code=%s msg=%s message_id=%s",
                resp.code,
                resp.msg,
                message_id,
            )
        else:
            logger.info("Feishu: typing reaction added message_id=%s", message_id)

    def _on_p2p_chat_entered(self, data: Any) -> None:
        """Handle user entering a P2P chat with the bot (bot_p2p_chat_entered_v1).

        Reference: hermes-agent/gateway/platforms/feishu.py:_on_p2p_chat_entered
        """
        logger.debug("Feishu: user entered P2P chat with bot")

    def _on_card_action_trigger(self, data: Any) -> None:
        """Handle card interactive button clicks (card.action.trigger).

        Parses the action value and injects a SyntheticMessage into ChannelManager
        so the agent can decide how to respond.
        """
        def _pick(value: Any, key: str, default: Any = None) -> Any:
            if isinstance(value, dict):
                return value.get(key, default)
            return getattr(value, key, default)

        try:
            event = _pick(data, "event") or _pick(data, "EVENT") or _pick(data, "event", {})
            # lark_oapi P2CardActionTriggerData shape: the button payload is at
            # event.action.value, the clicker at event.operator.open_id, and the
            # form payload at event.action.form_value. The earlier code read flat
            # event.action_value / event.user_id, which DON'T EXIST on this model
            # (lark_oapi >= card 2.0) — every click parsed empty and fell through
            # to the synthetic-message branch. Navigate the real nesting, keeping
            # the flat names as a fallback for any other event shape.
            action_obj = _pick(event, "action", {}) or {}
            action_value = _pick(action_obj, "value", None)
            if not action_value:
                action_value = _pick(event, "action_value", {}) or {}
            action_value = action_value or {}
            operator = _pick(event, "operator", {}) or {}
            user_id = (
                _pick(operator, "open_id", "")
                or _pick(operator, "user_id", "")
                or _pick(event, "user_id", "")
                or ""
            )
            context = _pick(event, "context", {}) or {}
            open_message_id = _pick(context, "open_message_id", "") or ""
            open_chat_id = _pick(context, "open_chat_id", "") or ""

            action = _pick(action_value, "action", "") or ""
            operation_id = _pick(action_value, "operation_id", "") or ""
            ask_user_id = _pick(action_value, "ask_user_id", "") or ""
            form_value = _pick(action_obj, "form_value", None) or _pick(event, "form_value", {}) or {}

            logger.info(
                "Feishu card action: action=%s operation_id=%s ask_user_id=%s user_id=%s",
                action, operation_id, ask_user_id, user_id
            )

            if action == "approval_resolve":
                # Blocking tool-call approval: resolve the in-process future
                # directly — the suspended turn continues by itself, so a
                # synthetic message round-trip would be wrong (the session is
                # mid-attempt and send_message would collide with it). The
                # lark SDK invokes this handler off-loop, and the future's
                # set_result must run on the loop thread.
                import functools

                approval_id = str(_pick(action_value, "approval_id", "") or "")
                decision = str(_pick(action_value, "decision", "") or "")
                card_id = (
                    str(_pick(event, "card_id", "") or "")
                    or str(_pick(action_value, "card_id", "") or "")
                    or open_message_id
                )
                broker = getattr(self._assistant_service, "approval_broker", None)
                allowed = (
                    "approve_once",
                    "approve_always",
                    "approve_persist",
                    "reject",
                )
                if (
                    broker is None
                    or self._asyncio_loop is None
                    or decision not in allowed
                ):
                    logger.error(
                        "Feishu approval action unusable approval_id=%s decision=%r "
                        "broker=%s loop=%s",
                        approval_id,
                        decision,
                        type(broker).__name__ if broker else None,
                        bool(self._asyncio_loop),
                    )
                    return

                def _form_field(name: str) -> str:
                    raw = form_value.get(name) if isinstance(form_value, dict) else None
                    if isinstance(raw, dict):
                        return str(raw.get("value") or "").strip()
                    if raw is None:
                        return ""
                    return str(raw).strip()

                command_prefix = _form_field("approval_command_prefix")
                if not command_prefix:
                    command_prefix = str(
                        _pick(action_value, "suggested_prefix", "") or ""
                    ).strip()
                reason = _form_field("approval_reject_reason")

                self._asyncio_loop.call_soon_threadsafe(
                    functools.partial(
                        broker.resolve,
                        approval_id,
                        action=decision,
                        source="feishu_card",
                        resolver_id=user_id or "",
                        reason=reason,
                        command_prefix=command_prefix,
                    )
                )
                asyncio.run_coroutine_threadsafe(
                    self._update_tool_approval_card(
                        approval_id=approval_id,
                        decision=decision,
                        resolver_id=user_id or "",
                        card_id=card_id,
                        action_value=action_value,
                        reason=reason,
                        command_prefix=command_prefix,
                    ),
                    self._asyncio_loop,
                )
                logger.info(
                    "Feishu approval resolve dispatched approval_id=%s decision=%s user_id=%s",
                    approval_id,
                    decision,
                    user_id,
                )
                return

            if action == "trade_approval_resolve":
                # Execution-side LIVE trade approval. Distinct from the assistant
                # broker above: the decision is delivered to the QueuedApprovalGate
                # (gate.approve/reject are async coroutines, so we use
                # run_coroutine_threadsafe — not call_soon_threadsafe). The lark
                # SDK invokes this handler off the asyncio loop thread.
                approval_id = str(_pick(action_value, "approval_id", "") or "")
                decision = str(_pick(action_value, "decision", "") or "")
                gate = self._trade_approval_gate
                card_id = (
                    str(_pick(event, "card_id", "") or "")
                    or str(_pick(action_value, "card_id", "") or "")
                    or open_message_id
                )
                if (
                    gate is None
                    or self._asyncio_loop is None
                    or decision not in ("approve", "reject")
                ):
                    logger.warning(
                        "Feishu trade approval action unusable approval_id=%s "
                        "decision=%r gate=%s loop=%s — trade approval not available",
                        approval_id,
                        decision,
                        type(gate).__name__ if gate else None,
                        bool(self._asyncio_loop),
                    )
                    return
                asyncio.run_coroutine_threadsafe(
                    self._resolve_trade_approval(
                        gate=gate,
                        approval_id=approval_id,
                        decision=decision,
                        resolver_id=user_id or "",
                        card_id=card_id,
                        action_value=action_value,
                    ),
                    self._asyncio_loop,
                )
                logger.info(
                    "Feishu trade approval resolve dispatched approval_id=%s "
                    "decision=%s user_id=%s",
                    approval_id,
                    decision,
                    user_id,
                )
                return

            if action == "stop_attempt":
                # Streaming-card 停止 button: abort the in-flight assistant attempt.
                # Unlike the approval branches this carries no future/gate — it just
                # sets the session's abort event (AssistantService.stop_attempt). The
                # suspended turn raises AssistantStoppedError, propagates to
                # ChannelManager._deliver_message's except block, and the streaming
                # card finalizes to 已终止 via abort_card(). The lark SDK invokes this
                # handler off the asyncio loop thread, so dispatch onto the loop.
                session_id = str(_pick(action_value, "session_id", "") or "")
                stop_fn = getattr(self._assistant_service, "stop_attempt", None)
                if not session_id or self._asyncio_loop is None or not callable(stop_fn):
                    logger.warning(
                        "Feishu stop_attempt action unusable session_id=%r loop=%s "
                        "stop_attempt=%s — stop not dispatched",
                        session_id,
                        bool(self._asyncio_loop),
                        callable(stop_fn),
                    )
                    return
                asyncio.run_coroutine_threadsafe(stop_fn(session_id), self._asyncio_loop)
                logger.info(
                    "Feishu stop_attempt dispatched session_id=%s user_id=%s",
                    session_id,
                    user_id,
                )
                return

            ask_user_answer = ""
            if action in ("confirm_write", "reject_write", "preview_write"):
                synthetic_content = f"/confirm {action} {operation_id}"
            elif action in ("ask_user_select", "ask_user_text"):
                option_label = str(_pick(action_value, "option_label", "") or "")
                if option_label:
                    ask_user_answer = option_label
                elif form_value:
                    input_values = []
                    for k, v in form_value.items():
                        if isinstance(v, dict):
                            input_values.append(str(v.get("value", "")))
                    ask_user_answer = "\n".join(input_values).strip()
                synthetic_content = f"/ask_user {ask_user_id}"
                if ask_user_answer:
                    synthetic_content = f"{synthetic_content} {ask_user_answer}"
            else:
                synthetic_content = f"/card_action {action}"

            payload = {
                "channel_id": self.channel_id,
                "channel_type": self.channel_type,
                "sender_id": user_id or "bot",
                "user_id": user_id or "bot",
                "session_id": self.resolve_session_id(user_id or "bot", {}),
                "content": synthetic_content,
                "meta": {
                    "feishu_action": action,
                    "feishu_operation_id": operation_id,
                    "feishu_ask_user_id": ask_user_id,
                    "feishu_form_value": form_value,
                    "feishu_chat_id": open_chat_id,
                    "sender_open_id": user_id,
                    "is_synthetic": True,
                },
            }

            if self._asyncio_loop and self._manager:
                asyncio.run_coroutine_threadsafe(
                    self._manager.enqueue(self.channel_id, payload),
                    self._asyncio_loop,
                )
                logger.info("Feishu: card action enqueued session_id=%s action=%s", payload["session_id"], action)

            if (
                action in ("ask_user_select", "ask_user_text")
                and open_message_id
                and self._asyncio_loop
            ):
                asyncio.run_coroutine_threadsafe(
                    self._finalize_ask_user_card(
                        message_id=open_message_id,
                        answer=ask_user_answer,
                        submitted=(action == "ask_user_text"),
                    ),
                    self._asyncio_loop,
                )
        except Exception:
            logger.exception("Feishu: _on_card_action_trigger failed")

    async def _finalize_ask_user_card(
        self, *, message_id: str, answer: str, submitted: bool
    ) -> None:
        """Best-effort lock the ask_user card to a terminal answered state.

        Removes the buttons/input so the operator cannot click it again. Runs
        regardless of whether the answer is stale w.r.t. the current pending
        question — the clicked card itself was answered and must reflect that.
        """
        from .card.builder import build_ask_user_answered_card
        from .card.cardkit import CardKitClient

        card = build_ask_user_answered_card(answer, submitted=submitted)
        cardkit = CardKitClient(
            app_id=self.app_id, app_secret=self.app_secret, domain=self.domain
        )
        try:
            ok = await asyncio.to_thread(cardkit.patch_message, message_id, card)
        except Exception as exc:  # noqa: BLE001 — visual lock is best-effort
            logger.warning(
                "Feishu ask_user card finalize raised type=%s msg=%s message_id=%s",
                type(exc).__name__,
                exc,
                message_id,
            )
            return
        if not ok:
            logger.warning(
                "Feishu ask_user card finalize failed message_id=%s", message_id
            )
            return
        logger.info(
            "Feishu ask_user card finalized message_id=%s submitted=%s",
            message_id,
            submitted,
        )

    async def _update_tool_approval_card(
        self,
        *,
        approval_id: str,
        decision: str,
        resolver_id: str,
        card_id: str,
        action_value: Any,
        reason: str = "",
        command_prefix: str = "",
    ) -> None:
        """Best-effort visual refresh for assistant tool-call approval cards."""
        from .card.builder import build_approval_resolved_card
        from .card.cardkit import CardKitClient

        def _pick(value: Any, key: str, default: Any = None) -> Any:
            if isinstance(value, dict):
                return value.get(key, default)
            return getattr(value, key, default)

        if not card_id:
            logger.warning(
                "Feishu approval resolved but no card_id/message_id to update "
                "approval_id=%s decision=%s",
                approval_id,
                decision,
            )
            return

        payload = {
            "approval_id": approval_id,
            "description": str(_pick(action_value, "description", "") or ""),
            "command_preview": str(_pick(action_value, "command_preview", "") or ""),
        }
        card = build_approval_resolved_card(
            payload,
            decision=decision,
            resolver=resolver_id,
            reason=reason,
            command_prefix=command_prefix,
        )
        cardkit = CardKitClient(
            app_id=self.app_id, app_secret=self.app_secret, domain=self.domain
        )
        try:
            if card_id.startswith("om_"):
                ok = await asyncio.to_thread(cardkit.patch_message, card_id, card)
            else:
                ok = await asyncio.to_thread(cardkit.update_card, card_id, card, 2)
        except Exception as exc:  # noqa: BLE001 — visual refresh is best-effort
            logger.warning(
                "Feishu approval card update raised type=%s msg=%s approval_id=%s card_id=%s",
                type(exc).__name__,
                exc,
                approval_id,
                card_id,
            )
            return
        if not ok:
            logger.warning(
                "Feishu approval card update failed approval_id=%s card_id=%s",
                approval_id,
                card_id,
            )
            return
        logger.info(
            "Feishu approval card updated to terminal approval_id=%s card_id=%s",
            approval_id,
            card_id,
        )

    async def _resolve_trade_approval(
        self,
        *,
        gate: Any,
        approval_id: str,
        decision: str,
        resolver_id: str,
        card_id: str,
        action_value: Any,
    ) -> None:
        """Drive an execution-side trade approval decision and refresh the card.

        Calls ``gate.approve``/``gate.reject`` (async). On success the original
        card is updated to the terminal ``build_trade_approval_resolved_card``.
        Known failure modes — already-resolved (``StateConflictError``) and
        missing/expired (``RecordNotFoundError``) — also refresh the card with a
        terminal "已被处理 / 已过期" notice and log a warning. No silent swallow
        (CLAUDE.md §错误可见性): every branch logs with approval_id / decision /
        error type.
        """
        from doyoutrade.persistence.errors import RecordNotFoundError, StateConflictError

        from .card.builder import build_trade_approval_resolved_card

        def _detail_payload() -> dict[str, Any]:
            # SAFETY INVARIANT (do NOT break): the values returned here are
            # operator-VISIBLE copies pulled from the clicked button's callback
            # value, used ONLY to re-render the terminal display card
            # (build_trade_approval_resolved_card → _update_card). They can drift
            # from the persisted intent and MUST NEVER be routed into
            # gate.approve/reject or order dispatch. The actual order is keyed by
            # approval_id alone → the resume sweep re-dispatches the persisted
            # deterministic OrderIntent (intent_from_json(intent_payload)). Never
            # feed this dict into a write/execution path.
            def _pick(value: Any, key: str, default: Any = None) -> Any:
                if isinstance(value, dict):
                    return value.get(key, default)
                return getattr(value, key, default)

            # The order side travels under ``side`` — the ``action`` key on the
            # button value is the callback action ("trade_approval_resolve"), NOT
            # buy/sell. Reading ``action`` for the side (the old code) mislabeled
            # the terminal card. rationale/signal_tag/strategy_tag carry the 信号
            # context so the terminal card matches the pending 信号+审批 card.
            return {
                "approval_id": approval_id,
                "task_id": str(_pick(action_value, "task_id", "") or ""),
                "intent_id": str(_pick(action_value, "intent_id", "") or ""),
                "symbol": str(_pick(action_value, "symbol", "") or ""),
                "symbol_name": str(_pick(action_value, "symbol_name", "") or ""),
                "action": str(_pick(action_value, "side", "") or ""),
                "notional": str(_pick(action_value, "notional", "") or ""),
                "strategy_tag": str(_pick(action_value, "strategy_tag", "") or ""),
                "signal_tag": str(_pick(action_value, "signal_tag", "") or ""),
                "rationale": str(_pick(action_value, "rationale", "") or ""),
                "created_at": str(_pick(action_value, "created_at", "") or ""),
                "price_reference": str(_pick(action_value, "price_reference", "") or ""),
                "order_type": str(_pick(action_value, "order_type", "") or ""),
                "tif": str(_pick(action_value, "tif", "") or ""),
                "exit_reason": str(_pick(action_value, "exit_reason", "") or ""),
                "last_price": str(_pick(action_value, "last_price", "") or ""),
                "pct_change": str(_pick(action_value, "pct_change", "") or ""),
                "direction": str(_pick(action_value, "direction", "") or ""),
                # Display-ONLY Agent narration carried in the button value so the
                # terminal card re-renders the same 🤖 AI 解读 the operator saw on
                # the pending card. Per the SAFETY INVARIANT above this is NEVER
                # routed into gate.approve/reject or dispatch.
                "narration": str(_pick(action_value, "narration", "") or ""),
            }

        async def _update_card(card: dict[str, Any]) -> None:
            if not card_id:
                logger.warning(
                    "Feishu trade approval resolved but no card_id to update "
                    "approval_id=%s decision=%s",
                    approval_id,
                    decision,
                )
                return
            from .card.cardkit import CardKitClient

            cardkit = CardKitClient(
                app_id=self.app_id, app_secret=self.app_secret, domain=self.domain
            )
            try:
                # cardkit uses a blocking httpx.Client; offload to a thread so the
                # asyncio loop is not stalled (mirrors StreamingCardController).
                # The trade-approval card is delivered via im.v1.message.create
                # (send_trade_approval_card), so the click event carries an
                # open_message_id (``om_...``) — update the MESSAGE via
                # PATCH /im/v1/messages/{id}. CardKit.update_card only works for
                # CardKit-entity cards (``cd_...``); using it on a message id is
                # exactly why the terminal refresh used to 400.
                if card_id.startswith("om_"):
                    ok = await asyncio.to_thread(cardkit.patch_message, card_id, card)
                else:
                    ok = await asyncio.to_thread(cardkit.update_card, card_id, card, 2)
            except Exception as exc:  # noqa: BLE001 — best-effort card refresh
                logger.warning(
                    "Feishu trade approval card update raised type=%s msg=%s "
                    "approval_id=%s card_id=%s",
                    type(exc).__name__,
                    exc,
                    approval_id,
                    card_id,
                )
                return
            if not ok:
                logger.warning(
                    "Feishu trade approval card update failed approval_id=%s card_id=%s",
                    approval_id,
                    card_id,
                )
            else:
                logger.info(
                    "Feishu trade approval card updated to terminal approval_id=%s card_id=%s",
                    approval_id,
                    card_id,
                )

        try:
            if decision == "approve":
                await gate.approve(
                    approval_id,
                    resolver_id=resolver_id,
                    decision_source="feishu_card",
                )
            else:
                await gate.reject(
                    approval_id,
                    resolver_id=resolver_id,
                    decision_source="feishu_card",
                )
        except StateConflictError as exc:
            logger.warning(
                "Feishu trade approval already resolved approval_id=%s decision=%s "
                "error_type=%s msg=%s",
                approval_id,
                decision,
                type(exc).__name__,
                exc,
            )
            payload = _detail_payload()
            card = build_trade_approval_resolved_card(
                payload, decision="reject", resolver=resolver_id
            )
            # Overwrite the status line so the operator sees it was a no-op, not
            # the outcome of this click.
            card["body"]["elements"][0]["text"]["content"] = (
                "该订单已被处理（可能已由他人审批或已过期），本次操作未生效。"
            )
            await _update_card(card)
            return
        except RecordNotFoundError as exc:
            logger.warning(
                "Feishu trade approval not found / expired approval_id=%s decision=%s "
                "error_type=%s msg=%s",
                approval_id,
                decision,
                type(exc).__name__,
                exc,
            )
            payload = _detail_payload()
            card = build_trade_approval_resolved_card(
                payload, decision="reject", resolver=resolver_id
            )
            card["body"]["elements"][0]["text"]["content"] = (
                "该订单已过期或不存在，本次操作未生效。"
            )
            await _update_card(card)
            return
        except Exception as exc:  # noqa: BLE001 — surface unexpected gate failures loudly
            logger.warning(
                "Feishu trade approval resolve failed approval_id=%s decision=%s "
                "error_type=%s msg=%s",
                approval_id,
                decision,
                type(exc).__name__,
                exc,
            )
            return

        logger.info(
            "Feishu trade approval resolved approval_id=%s decision=%s resolver_id=%s",
            approval_id,
            decision,
            resolver_id,
        )
        payload = _detail_payload()
        card = build_trade_approval_resolved_card(
            payload, decision=decision, resolver=resolver_id
        )
        await _update_card(card)

    async def send_trade_approval_card(
        self, chat_id: str, payload: dict[str, Any], narration: str | None = None
    ) -> str | None:
        """Send a LIVE trade-approval card to ``chat_id``; return its message_id.

        ``payload`` carries the execution-side approval snapshot (see
        ``build_trade_approval_card``). ``narration`` (optional) is the
        Agent-composed body for prose mode; when absent the deterministic rich
        card is sent. Sends the interactive card via the same lark-oapi path as
        ``_send_card`` but returns the resulting ``message_id`` so the caller can
        correlate a later ``update_card``. Best-effort: a send failure logs a
        warning and returns None (no raise) so the scheduler delivery loop keeps
        running.
        """
        import json as _json

        from lark_oapi.api.im.v1.model import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        from .card.builder import build_trade_approval_card

        card = build_trade_approval_card(payload, narration)
        client = self._ensure_lark_client()
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(_json.dumps(card))
                .build()
            )
            .build()
        )
        try:
            resp = await client.im.v1.message.acreate(req)
        except Exception as exc:  # noqa: BLE001 — best-effort delivery
            logger.warning(
                "Feishu send trade approval card request error chat_id=%s "
                "approval_id=%s error_type=%s msg=%s",
                chat_id,
                str(payload.get("approval_id") or ""),
                type(exc).__name__,
                exc,
            )
            return None
        if resp.code != 0:
            logger.warning(
                "Feishu send trade approval card failed code=%s msg=%s chat_id=%s "
                "approval_id=%s",
                resp.code,
                resp.msg,
                chat_id,
                str(payload.get("approval_id") or ""),
            )
            return None
        message_id = getattr(getattr(resp, "data", None), "message_id", None)
        logger.info(
            "Feishu trade approval card sent chat_id=%s approval_id=%s message_id=%s",
            chat_id,
            str(payload.get("approval_id") or ""),
            message_id,
        )
        return message_id

    async def send_trade_approval_result_card(
        self, chat_id: str, payload: dict[str, Any], *, outcome: str
    ) -> str | None:
        """Push the post-dispatch order-result receipt card to ``chat_id``.

        ``payload`` carries the deterministic outcome facts (symbol / symbol_name /
        action / strategy_tag / task_id / approval_id / run_id, plus ``fill_*`` on a
        fill or ``error`` on a non-fill). ``outcome`` ∈ {``filled``, ``failed``,
        ``abandoned``}. A NEW message (not an update of the approval card) so the
        operator gets an explicit "did it actually fill" notification. Best-effort:
        a send failure logs a warning and returns None so the resume sweep keeps
        running.
        """
        import json as _json

        from lark_oapi.api.im.v1.model import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        from .card.builder import build_trade_approval_result_card

        card = build_trade_approval_result_card(payload, outcome=outcome)
        client = self._ensure_lark_client()
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(_json.dumps(card))
                .build()
            )
            .build()
        )
        approval_id = str(payload.get("approval_id") or "")
        try:
            resp = await client.im.v1.message.acreate(req)
        except Exception as exc:  # noqa: BLE001 — best-effort delivery
            logger.warning(
                "Feishu send trade result card request error chat_id=%s approval_id=%s "
                "outcome=%s error_type=%s msg=%s",
                chat_id,
                approval_id,
                outcome,
                type(exc).__name__,
                exc,
            )
            return None
        if resp.code != 0:
            logger.warning(
                "Feishu send trade result card failed code=%s msg=%s chat_id=%s "
                "approval_id=%s outcome=%s",
                resp.code,
                resp.msg,
                chat_id,
                approval_id,
                outcome,
            )
            return None
        message_id = getattr(getattr(resp, "data", None), "message_id", None)
        logger.info(
            "Feishu trade result card sent chat_id=%s approval_id=%s outcome=%s message_id=%s",
            chat_id,
            approval_id,
            outcome,
            message_id,
        )
        return message_id

    def _get_bot_open_id_cached(self) -> str:
        """Return the bot's own open_id, fetching once and caching on success.

        Failures are not cached so a transient error self-heals on the next
        mentioned message; each failed attempt is logged by ``_fetch_bot_open_id``.
        """
        if self._bot_open_id:
            return self._bot_open_id
        fetched = self._fetch_bot_open_id()
        if fetched:
            self._bot_open_id = fetched
        return fetched

    def _fetch_bot_open_id(self) -> str:
        """Resolve the bot's open_id via GET /open-apis/bot/v3/info.

        Returns "" on any failure (and logs why) so the caller can degrade to
        stripping all @ placeholders rather than silently mishandling commands.
        """
        if self._lark_client is None:
            logger.warning("Feishu: cannot resolve bot open_id - lark client not initialized")
            return ""
        try:
            import json as _json

            from lark_oapi.core.enum import AccessTokenType, HttpMethod
            from lark_oapi.core.model import BaseRequest

            req = (
                BaseRequest.builder()
                .http_method(HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({AccessTokenType.TENANT})
                .build()
            )
            resp = self._lark_client.request(req)
            if resp.raw is None or resp.raw.content is None:
                logger.warning("Feishu: bot/v3/info returned empty response")
                return ""
            data = _json.loads(resp.raw.content)
            if data.get("code") != 0:
                logger.warning(
                    "Feishu: bot/v3/info failed code=%s msg=%s",
                    data.get("code"),
                    data.get("msg"),
                )
                return ""
            open_id = (data.get("bot") or {}).get("open_id", "") or ""
            logger.info("Feishu: resolved bot open_id=%s", open_id)
            return open_id
        except Exception as e:
            logger.warning(
                "Feishu: bot/v3/info request error type=%s msg=%s",
                type(e).__name__,
                e,
            )
            return ""

    def _parse_event(self, data: Any) -> dict[str, Any] | None:
        # lark-oapi uses object-style event payloads, while tests and some
        # integrations may pass dict-shaped payloads. Support both forms.
        def _pick(value: Any, key: str, default: Any = None) -> Any:
            if isinstance(value, dict):
                return value.get(key, default)
            return getattr(value, key, default)

        try:
            event = _pick(data, "event")
            if event is None:
                return None
            sender = _pick(event, "sender", {}) or {}
            message = _pick(event, "message", {}) or {}
            sender_id_obj = _pick(sender, "sender_id", {}) or {}
            sender_id = _pick(sender_id_obj, "open_id", "") or ""
            chat_id = _pick(message, "chat_id", "") or ""
            chat_type = _pick(message, "chat_type", "p2p") or "p2p"
            msg_type = _pick(message, "msg_type", "text") or "text"
            content = _pick(message, "content", "{}") or "{}"
            message_id = _pick(message, "message_id", "") or ""
            root_id = _pick(message, "root_id", "") or ""
            parent_id = _pick(message, "parent_id", "") or ""
            # Group @-mentions arrive as placeholder tokens (@_user_N) in the text;
            # the real identities live in message.mentions. Normalize to plain dicts.
            raw_mentions = _pick(message, "mentions", []) or []
            mentions = self._normalize_mentions(raw_mentions)
            # Extract sessionWebhook (for Webhook push reply)
            # Check multiple possible locations; lark-oapi uses camelCase sessionWebhook
            header = _pick(data, "header", {}) or {}
            incoming_message = _pick(event, "incoming_message", {}) or {}
            session_webhook = (
                _pick(message, "sessionWebhook", "") or
                _pick(message, "session_webhook", "") or
                _pick(event, "sessionWebhook", "") or
                _pick(event, "session_webhook", "") or
                _pick(header, "sessionWebhook", "") or
                _pick(header, "session_webhook", "") or
                _pick(incoming_message, "sessionWebhook", "") or
                _pick(incoming_message, "session_webhook", "") or
                _pick(data, "sessionWebhook", "") or
                _pick(data, "session_webhook", "") or
                ""
            )
        except Exception:
            return None

        if not sender_id:
            return None

        # Only pay the bot/v3/info round-trip when there is actually an @ to resolve.
        bot_open_id = self._get_bot_open_id_cached() if mentions else ""
        if mentions and not bot_open_id:
            logger.warning(
                "Feishu: bot open_id unresolved; stripping all @ placeholders "
                "(other-user mentions lose their name) chat_id=%s message_id=%s mentions=%s",
                chat_id,
                message_id,
                [m.get("key") for m in mentions],
            )

        from doyoutrade.assistant.channels.feishu.utils import parse_feishu_message_content
        content_parts = parse_feishu_message_content(
            msg_type, content, mentions=mentions, bot_open_id=bot_open_id
        )
        text_content = "\n".join(
            cp.text for cp in content_parts if isinstance(cp, TextContent)
        ) or str(content)
        if mentions:
            logger.info(
                "Feishu: resolved mentions message_id=%s bot_open_id=%s cleaned_text=%r",
                message_id,
                bot_open_id,
                text_content,
            )

        reply_target_message_id, reply_relation = self._extract_reply_target(
            message_id,
            root_id,
            parent_id,
        )
        reply_target = self._fetch_message_brief(reply_target_message_id) if reply_target_message_id else None

        meta = {
            "feishu_message_id": message_id,
            "feishu_chat_id": chat_id,
            "feishu_chat_type": chat_type,
            "sender_open_id": sender_id,
            "session_webhook": session_webhook,
            "feishu_reply_to_message_id": message_id,
            "feishu_mentions": mentions,
            "feishu_bot_open_id": bot_open_id,
            "feishu_root_message_id": root_id,
            "feishu_parent_message_id": parent_id,
            "feishu_reply_target_message_id": reply_target_message_id,
            "feishu_reply_target_relation": reply_relation,
        }
        if reply_target is not None:
            meta["feishu_reply_target_msg_type"] = reply_target.get("msg_type", "")
            meta["feishu_reply_target_content"] = reply_target.get("content", "")
        logger.info(
            "Feishu: parsed inbound message_id=%s reply_target=%s relation=%s session_webhook=%s",
            message_id,
            reply_target_message_id,
            reply_relation,
            bool(session_webhook),
        )
        return {
            "channel_id": self.channel_id,
            "channel_type": self.channel_type,
            "sender_id": sender_id,
            "user_id": sender_id,
            "session_id": self.resolve_session_id(sender_id, meta),
            "content": text_content,
            "meta": meta,
        }

    def build_agent_request_from_native(self, native_payload: Any) -> ChannelAgentRequest:
        if isinstance(native_payload, dict):
            return ChannelAgentRequest(
                session_id=native_payload.get("session_id", ""),
                content=native_payload.get("content", ""),
                sender_id=native_payload.get("sender_id", ""),
                channel_meta=native_payload.get("meta", {}),
            )
        return ChannelAgentRequest(session_id="", content=str(native_payload))

    @staticmethod
    def _reply_to_message_id_from_meta(meta: dict[str, Any]) -> str:
        return str(
            meta.get("feishu_reply_to_message_id")
            or meta.get("feishu_message_id")
            or ""
        )

    def get_reply_target_message_id(self, meta: dict[str, Any]) -> str:
        return str(meta.get("feishu_reply_target_message_id") or "").strip()

    def apply_local_delivery_ref(
        self,
        meta: dict[str, Any],
        delivery_ref: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(meta or {})
        canonical_text = str(delivery_ref.get("canonical_text") or "").strip()
        if canonical_text:
            merged["feishu_reply_target_content"] = canonical_text
        platform_message_type = str(delivery_ref.get("platform_message_type") or "").strip()
        if platform_message_type:
            merged["feishu_reply_target_msg_type"] = platform_message_type
        merged["feishu_reply_target_source"] = "local_delivery_cache"
        return merged

    def build_turn_context_reminder(self, meta: dict[str, Any]) -> str | None:
        target_message_id = str(meta.get("feishu_reply_target_message_id") or "").strip()
        if not target_message_id:
            return None
        relation = str(meta.get("feishu_reply_target_relation") or "reply").strip() or "reply"
        target_type = str(meta.get("feishu_reply_target_msg_type") or "").strip() or "unknown"
        target_content = str(meta.get("feishu_reply_target_content") or "").strip()
        if not target_content:
            target_content = "(unavailable: the replied message content could not be fetched)"
        return (
            "<system-reminder>\n"
            "# feishuReplyContext\n"
            "The current user message was sent in Feishu as a reply to an earlier message.\n"
            f"replyTargetMessageId: {target_message_id}\n"
            f"replyTargetRelation: {relation}\n"
            f"replyTargetType: {target_type}\n"
            f"replyTargetContent:\n{target_content}\n"
            "Use this as channel context for interpreting pronouns like 'this' or 'that'.\n"
            "</system-reminder>"
        )

    def build_user_message_metadata(self, meta: dict[str, Any]) -> dict[str, Any]:
        channel_metadata: dict[str, Any] = {
            "type": self.channel_type,
            "message_id": str(meta.get("feishu_message_id") or ""),
            "chat_id": str(meta.get("feishu_chat_id") or ""),
            "chat_type": str(meta.get("feishu_chat_type") or ""),
        }
        reply_target_message_id = str(meta.get("feishu_reply_target_message_id") or "").strip()
        if reply_target_message_id:
            channel_metadata["reply_context"] = {
                "target_message_id": reply_target_message_id,
                "relation": str(meta.get("feishu_reply_target_relation") or ""),
                "target_msg_type": str(meta.get("feishu_reply_target_msg_type") or ""),
                "target_content": str(meta.get("feishu_reply_target_content") or ""),
            }
        return {"channel": channel_metadata}

    async def send(
        self,
        session_id: str,
        content: ContentPart,
        meta: dict[str, Any],
    ) -> ChannelDeliveryReceipt | None:
        # For p2p: use open_id (sender). For group: use chat_id.
        receive_id, receive_id_type = self._resolve_receive_routing(session_id, meta)
        reply_to_message_id = self._reply_to_message_id_from_meta(meta)

        if isinstance(content, TextContent):
            message_id = await self._send_text(
                receive_id,
                receive_id_type,
                content.text,
                reply_to_message_id=reply_to_message_id,
            )
            return self._delivery_receipt_for_message(message_id, "text")
        elif isinstance(content, ImageContent):
            raise NotImplementedError("Image send not yet implemented")
        elif isinstance(content, FileContent):
            raise NotImplementedError("File send not yet implemented")
        elif isinstance(content, CardContent):
            message_id = await self._send_card(
                receive_id,
                receive_id_type,
                content.card,
                reply_to_message_id=reply_to_message_id,
            )
            return self._delivery_receipt_for_message(message_id, "interactive")
        # AudioContent: ignore for now
        return None

    @staticmethod
    def _delivery_receipt_for_message(
        message_id: str | None,
        platform_message_type: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> ChannelDeliveryReceipt | None:
        normalized_message_id = str(message_id or "").strip()
        if not normalized_message_id:
            return None
        return ChannelDeliveryReceipt(
            handles=[
                ChannelDeliveryHandle(
                    platform_message_id=normalized_message_id,
                    platform_message_type=platform_message_type,
                    extra=dict(extra or {}),
                )
            ]
        )

    def collect_streaming_delivery_receipt(
        self,
        controller: Any,
        meta: dict[str, Any],
    ) -> ChannelDeliveryReceipt | None:
        handles: list[ChannelDeliveryHandle] = []
        seen: set[str] = set()

        def _append(message_id: str | None, *, variant: str) -> None:
            normalized_message_id = str(message_id or "").strip()
            if not normalized_message_id or normalized_message_id in seen:
                return
            seen.add(normalized_message_id)
            handles.append(
                ChannelDeliveryHandle(
                    platform_message_id=normalized_message_id,
                    platform_message_type="interactive",
                    extra={"variant": variant},
                )
            )

        _append(getattr(controller, "message_id", None), variant="final")
        _append(getattr(controller, "_reasoning_message_id", None), variant="reasoning")
        for call_id, message_id in dict(getattr(controller, "_tool_message_ids", {}) or {}).items():
            _append(message_id, variant=f"tool:{call_id}")
        if not handles:
            return None
        return ChannelDeliveryReceipt(handles=handles)

    def _resolve_receive_routing(
        self, session_id: str, meta: dict[str, Any]
    ) -> tuple[str, str]:
        chat_type = meta.get("feishu_chat_type")
        if chat_type == "p2p":
            receive_id = meta.get("sender_open_id")
            receive_id_type = "open_id"
        else:
            receive_id = meta.get("feishu_chat_id")
            receive_id_type = "chat_id"
        if receive_id:
            return receive_id, receive_id_type
        fallback = session_id.removeprefix("feishu:")
        logger.warning(
            "FeishuChannel: outbound routing unresolved chat_type=%s "
            "chat_id=%s sender_open_id=%s; falling back to session-derived "
            "id=%r receive_id_type=%s — Feishu will likely reject this send. "
            "Ensure inbound/card-action meta carries feishu_chat_id and "
            "sender_open_id.",
            chat_type,
            meta.get("feishu_chat_id"),
            meta.get("sender_open_id"),
            fallback,
            receive_id_type,
        )
        return fallback, receive_id_type

    def create_streaming_controller(
        self, session_id: str, meta: dict[str, Any]
    ):
        """Create a StreamingCardController for streaming card output.

        Uses the card/ package to send interactive streaming cards via CardKit API.
        """
        from doyoutrade.assistant.channels.feishu.card import CardKitClient, StreamingCardController

        receive_id, receive_id_type = self._resolve_receive_routing(session_id, meta)

        cardkit = CardKitClient(app_id=self.app_id, app_secret=self.app_secret, domain=self.domain)
        return StreamingCardController(
            cardkit_client=cardkit,
            chat_id=receive_id,
            receive_id=receive_id,
            receive_id_type=receive_id_type,
            reply_to_message_id=self._reply_to_message_id_from_meta(meta),
            show_tool_use=True,
            session_key=session_id,
            precreated_cards=self._precreated_cards,
        )

    def _ensure_lark_client(self) -> Any:
        """Return the lark API client, building one on demand.

        ``start()`` builds it for the receive loop, but read-only API calls (e.g.
        listing chats for the trigger-delivery picker) may run before/without the
        websocket loop, so build a standalone client when absent.
        """
        if self._lark_client is not None:
            return self._lark_client
        import lark_oapi as lark
        from lark_oapi.client import ClientBuilder

        self._lark_client = (
            ClientBuilder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .log_level(lark.LogLevel.INFO)
            .domain(lark.LARK_DOMAIN if self.domain == "lark" else lark.FEISHU_DOMAIN)
            .build()
        )
        return self._lark_client

    async def list_chats(self) -> list[dict[str, str]]:
        """List the Feishu groups this bot belongs to (im/v1/chats, paginated).

        Returns ``[{"chat_id": "oc_…", "name": "…"}]`` — the source for the trigger
        delivery channel picker. Raises ``RuntimeError`` with the Feishu code/msg on
        a non-zero response so the caller can surface why the list is empty (no
        silent swallow — CLAUDE.md §错误可见性).
        """
        from lark_oapi.api.im.v1.model import ListChatRequest

        client = self._ensure_lark_client()
        chats: list[dict[str, str]] = []
        page_token: str | None = None
        while True:
            builder = ListChatRequest.builder().page_size(100)
            if page_token:
                builder = builder.page_token(page_token)
            resp = await client.im.v1.chat.alist(builder.build())
            if resp.code != 0:
                raise RuntimeError(
                    f"feishu im/v1/chats list failed: code={resp.code} msg={resp.msg}"
                )
            data = resp.data
            for item in (getattr(data, "items", None) or []):
                chat_id = getattr(item, "chat_id", "") or ""
                if not chat_id:
                    continue
                chats.append({"chat_id": chat_id, "name": getattr(item, "name", "") or chat_id})
            if not getattr(data, "has_more", False):
                break
            page_token = getattr(data, "page_token", None)
            if not page_token:
                break
        return chats

    async def _send_text(
        self,
        receive_id: str,
        receive_id_type: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        import json as _json
        from lark_oapi.api.im.v1.model import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        content_str = _json.dumps({"text": text}, ensure_ascii=False)
        if reply_to_message_id:
            message_id = await self._reply_message_http(
                reply_to_message_id=reply_to_message_id,
                msg_type="text",
                content=content_str,
            )
            if message_id:
                return message_id
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(content_str)
                .build()
            )
            .build()
        )
        resp = await self._lark_client.im.v1.message.acreate(req)
        if resp.code != 0:
            import logging
            logging.getLogger(__name__).warning(
                "Feishu send text failed: code=%s msg=%s", resp.code, resp.msg
            )
            return None
        return str(getattr(getattr(resp, "data", None), "message_id", None) or "") or None

    async def _upload_image_bytes(self, data: bytes) -> str | None:
        """Upload raw image bytes to Feishu, returning an ``image_key`` or None.

        Uses the sync ``im.v1.image.create`` wrapped in a thread so we don't
        depend on an async variant existing. A non-zero response is logged (not
        swallowed) and yields ``None`` so the caller can fall back to text.
        """
        import asyncio as _asyncio
        import io as _io

        from lark_oapi.api.im.v1 import (
            CreateImageRequest,
            CreateImageRequestBody,
        )

        client = self._ensure_lark_client()
        req = (
            CreateImageRequest.builder()
            .request_body(
                CreateImageRequestBody.builder()
                .image_type("message")
                .image(_io.BytesIO(data))
                .build()
            )
            .build()
        )
        try:
            resp = await _asyncio.to_thread(client.im.v1.image.create, req)
        except Exception as exc:  # noqa: BLE001 - upload is best-effort, log + fall back
            logger.warning(
                "Feishu image upload raised %s: %s (falling back to text)",
                type(exc).__name__,
                exc,
            )
            return None
        if getattr(resp, "code", None) != 0:
            logger.warning(
                "Feishu image upload failed: code=%s msg=%s",
                getattr(resp, "code", None),
                getattr(resp, "msg", None),
            )
            return None
        return str(getattr(getattr(resp, "data", None), "image_key", None) or "") or None

    async def _upload_file_bytes(self, data: bytes, *, file_name: str) -> str | None:
        """Upload raw file bytes to Feishu, returning a ``file_key`` or None."""
        import asyncio as _asyncio
        import io as _io

        from lark_oapi.api.im.v1 import (
            CreateFileRequest,
            CreateFileRequestBody,
        )

        client = self._ensure_lark_client()
        req = (
            CreateFileRequest.builder()
            .request_body(
                CreateFileRequestBody.builder()
                .file_type("stream")
                .file_name(file_name or "attachment")
                .file(_io.BytesIO(data))
                .build()
            )
            .build()
        )
        try:
            resp = await _asyncio.to_thread(client.im.v1.file.create, req)
        except Exception as exc:  # noqa: BLE001 - upload is best-effort, log + fall back
            logger.warning(
                "Feishu file upload raised %s: %s",
                type(exc).__name__,
                exc,
            )
            return None
        if getattr(resp, "code", None) != 0:
            logger.warning(
                "Feishu file upload failed: code=%s msg=%s",
                getattr(resp, "code", None),
                getattr(resp, "msg", None),
            )
            return None
        return str(getattr(getattr(resp, "data", None), "file_key", None) or "") or None

    async def _send_image(
        self,
        receive_id: str,
        receive_id_type: str,
        content: ImageContent,
        *,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        """Send an image. Prefers an existing ``image_id``; else uploads ``data``.

        On any failure to obtain/send an image, falls back to the ``caption``
        text (if any) so the recipient still gets the content — never silent.
        """
        import json as _json
        from lark_oapi.api.im.v1.model import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        image_key = (content.image_id or "").strip()
        if not image_key and content.data:
            image_key = await self._upload_image_bytes(content.data) or ""

        if not image_key:
            logger.warning(
                "Feishu image send: no image_key and no uploadable data "
                "(caption_fallback=%s)",
                bool(content.caption),
            )
            if content.caption:
                return await self._send_text(
                    receive_id,
                    receive_id_type,
                    content.caption,
                    reply_to_message_id=reply_to_message_id,
                )
            return None

        content_str = _json.dumps({"image_key": image_key}, ensure_ascii=False)
        if reply_to_message_id:
            message_id = await self._reply_message_http(
                reply_to_message_id=reply_to_message_id,
                msg_type="image",
                content=content_str,
            )
            if message_id:
                return message_id
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("image")
                .content(content_str)
                .build()
            )
            .build()
        )
        resp = await self._lark_client.im.v1.message.acreate(req)
        if resp.code != 0:
            logger.warning(
                "Feishu send image failed: code=%s msg=%s (caption_fallback=%s)",
                resp.code,
                resp.msg,
                bool(content.caption),
            )
            if content.caption:
                return await self._send_text(
                    receive_id,
                    receive_id_type,
                    content.caption,
                    reply_to_message_id=reply_to_message_id,
                )
            return None
        return str(getattr(getattr(resp, "data", None), "message_id", None) or "") or None

    async def _send_file(
        self,
        receive_id: str,
        receive_id_type: str,
        content: FileContent,
        *,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        """Send a file. Prefers an existing ``file_id``; else uploads ``data``."""
        import json as _json
        from lark_oapi.api.im.v1.model import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        file_key = (content.file_id or "").strip()
        if not file_key and content.data:
            file_key = await self._upload_file_bytes(
                content.data, file_name=content.name or "attachment"
            ) or ""

        if not file_key:
            logger.warning(
                "Feishu file send: no file_key and no uploadable data name=%s",
                content.name,
            )
            return None

        content_str = _json.dumps({"file_key": file_key}, ensure_ascii=False)
        if reply_to_message_id:
            message_id = await self._reply_message_http(
                reply_to_message_id=reply_to_message_id,
                msg_type="file",
                content=content_str,
            )
            if message_id:
                return message_id
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("file")
                .content(content_str)
                .build()
            )
            .build()
        )
        resp = await self._lark_client.im.v1.message.acreate(req)
        if resp.code != 0:
            logger.warning(
                "Feishu send file failed: code=%s msg=%s", resp.code, resp.msg
            )
            return None
        return str(getattr(getattr(resp, "data", None), "message_id", None) or "") or None

    async def _send_card(
        self,
        receive_id: str,
        receive_id_type: str,
        card: dict[str, Any],
        *,
        reply_to_message_id: str | None = None,
    ) -> str | None:
        import json as _json
        from lark_oapi.api.im.v1.model import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        content_str = _json.dumps(card)
        if reply_to_message_id:
            message_id = await self._reply_message_http(
                reply_to_message_id=reply_to_message_id,
                msg_type="interactive",
                content=content_str,
            )
            if message_id:
                return message_id
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("interactive")
                .content(content_str)
                .build()
            )
            .build()
        )
        resp = await self._lark_client.im.v1.message.acreate(req)
        if resp.code != 0:
            logger.warning(
                "Feishu send card failed: code=%s msg=%s", resp.code, resp.msg
            )
            return None
        return str(getattr(getattr(resp, "data", None), "message_id", None) or "") or None

    async def _reply_message_http(
        self,
        *,
        reply_to_message_id: str,
        msg_type: str,
        content: str,
    ) -> str | None:
        if not reply_to_message_id:
            return None
        url = f"{self._base_url}/open-apis/im/v1/messages/{reply_to_message_id}/reply"
        payload = {
            "msg_type": msg_type,
            "content": content,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=self._tenant_headers(), json=payload)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:
            logger.warning(
                "Feishu: reply send HTTP error type=%s msg=%s reply_to_message_id=%s",
                type(exc).__name__,
                exc,
                reply_to_message_id,
            )
            return None
        if body.get("code") != 0:
            logger.warning(
                "Feishu: reply send failed code=%s msg=%s reply_to_message_id=%s",
                body.get("code"),
                body.get("msg"),
                reply_to_message_id,
            )
            return None
        data = body.get("data") or {}
        return str(data.get("message_id") or "") or None

    async def _send_text_webhook(self, webhook: str, message_id: str, text: str) -> bool:
        """通过 sessionWebhook 发送回复消息。

        Args:
            webhook: sessionWebhook URL
            message_id: 被回复的消息 ID（当前未使用，但签名保留以便扩展）
            text: 回复文本内容

        Returns:
            True if successful, False otherwise.
        """
        if not webhook or not text:
            logger.debug("Feishu: skip webhook send - empty webhook or text")
            return False

        try:
            payload = {
                "msg_type": "text",
                "content": {"text": text},
            }
            # 注意：POST 到 sessionWebhook 不需要传 message_id，
            # Feishu 自动关联到触发该 webhook 的事件

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    webhook,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            if resp.status_code == 200:
                logger.info("Feishu: webhook send ok message_id=%s", message_id[:16] if message_id else "?")
                return True
            else:
                logger.warning(
                    "Feishu: webhook send failed status=%s body=%s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except Exception:
            logger.exception("Feishu: _send_text_webhook failed")
            return False

    async def send_reply(
        self,
        session_id: str,
        reply: LifecycleReply,
        meta: dict[str, Any],
    ) -> ChannelDeliveryReceipt | None:
        """将 LifecycleReply 渲染为 Feishu 静态卡片并发送。"""
        card = self._build_lifecycle_card(reply)
        return await self.send(session_id, CardContent(card), meta)

    def _build_lifecycle_card(self, reply: LifecycleReply) -> dict[str, Any]:
        """将 LifecycleReply 渲染为 Feishu 卡片 JSON。"""
        # 构建 fields 列表
        fields = []
        for item in reply.content:
            fields.append({
                "is_short": True,
                "text": {
                    "tag": "lark_md",
                    "content": f"**{item.get('label', '')}**\n{item.get('value', '')}",
                },
            })

        # 构建 elements
        elements = []
        if fields:
            elements.append({
                "tag": "div",
                "fields": fields,
            })
        if reply.footer:
            elements.append({
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": reply.footer},
                ],
            })

        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"✅ {reply.title}"},
                "template": "blue",
            },
            "elements": elements,
        }
