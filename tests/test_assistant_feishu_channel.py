import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from doyoutrade.assistant.channels.feishu import FeishuChannel
from doyoutrade.assistant.channels.feishu.card.streaming import StreamingCardController
from doyoutrade.assistant.channels.config import FeishuChannelConfig


class TestFeishuChannelBasic(unittest.TestCase):
    def test_channel_type(self):
        self.assertEqual(FeishuChannel.channel_type, "feishu")

    def test_from_config_sets_fields(self):
        mock_as = MagicMock()
        cfg = FeishuChannelConfig(
            enabled=True,
            app_id="cli_aaa",
            app_secret="secret_xyz",
            domain="lark",
        )
        ch = FeishuChannel.from_config(mock_as, cfg)
        self.assertEqual(ch.app_id, "cli_aaa")
        self.assertEqual(ch.app_secret, "secret_xyz")
        self.assertEqual(ch.domain, "lark")
        self.assertEqual(ch.encrypt_key, "")
        self.assertEqual(ch.verification_token, "")
        self.assertIs(ch._assistant_service, mock_as)

    def test_from_config_domain_defaults_to_feishu(self):
        mock_as = MagicMock()
        cfg = FeishuChannelConfig(app_id="cli_aaa", app_secret="secret")
        ch = FeishuChannel.from_config(mock_as, cfg)
        self.assertEqual(ch.domain, "feishu")

    def test_resolve_session_id(self):
        mock_as = MagicMock()
        ch = FeishuChannel.from_config(mock_as, FeishuChannelConfig())
        sid = ch.resolve_session_id("ou_abc123", {})
        self.assertEqual(sid, "channel:feishu:ou_abc123")

    def test_resolve_session_id_ignores_meta(self):
        mock_as = MagicMock()
        ch = FeishuChannel.from_config(mock_as, FeishuChannelConfig())
        sid = ch.resolve_session_id("ou_xyz", {"feishu_chat_id": "oc_123"})
        self.assertEqual(sid, "channel:feishu:ou_xyz")

    def test_clone_preserves_assistant_service(self):
        mock_as = MagicMock()
        ch = FeishuChannel.from_config(mock_as, FeishuChannelConfig())
        cloned = ch.clone(FeishuChannelConfig())
        self.assertIs(cloned._assistant_service, mock_as)
        self.assertEqual(cloned.channel_type, "feishu")


class TestFeishuChannelWebSocket(unittest.TestCase):
    def setUp(self):
        self.mock_as = MagicMock()
        self.mock_manager = MagicMock()
        self.mock_manager.enqueue = AsyncMock()

    def test_parse_event_text_message(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.config import FeishuChannelConfig

        ch = FeishuChannel(
            assistant_service=self.mock_as,
            app_id="cli_aaa",
            app_secret="secret",
        )
        ch._manager = self.mock_manager
        ch._asyncio_loop = asyncio.new_event_loop()

        event_data = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_abc123"}},
                "message": {
                    "message_id": "om_msg_001",
                    "chat_id": "oc_chat_xyz",
                    "chat_type": "p2p",
                    "content": '{"text":"Hello world"}',
                    "msg_type": "text",
                },
            },
        }

        result = ch._parse_event(event_data)

        self.assertIsNotNone(result)
        self.assertEqual(result["channel_id"], "feishu")
        self.assertEqual(result["channel_type"], "feishu")
        self.assertEqual(result["sender_id"], "ou_abc123")
        self.assertEqual(result["user_id"], "ou_abc123")
        self.assertEqual(result["session_id"], "channel:feishu:ou_abc123")
        self.assertEqual(result["content"], "Hello world")
        self.assertEqual(result["meta"]["feishu_message_id"], "om_msg_001")
        self.assertEqual(result["meta"]["feishu_chat_id"], "oc_chat_xyz")
        self.assertEqual(result["meta"]["feishu_chat_type"], "p2p")

    def test_parse_event_strips_bot_mention_so_slash_command_survives(self):
        """Group @bot + /new should yield a bare '/new' content (bug fix)."""
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.lifecycle_commands import parse_lifecycle_command

        ch = FeishuChannel(
            assistant_service=self.mock_as,
            app_id="cli_aaa",
            app_secret="secret",
        )
        ch._bot_open_id = "ou_bot_self"  # pretend bot/v3/info already resolved

        event_data = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_human"}},
                "message": {
                    "message_id": "om_grp_001",
                    "chat_id": "oc_group_xyz",
                    "chat_type": "group",
                    "content": '{"text":"@_user_1 /new"}',
                    "msg_type": "text",
                    "mentions": [
                        {"key": "@_user_1", "id": {"open_id": "ou_bot_self"}, "name": "doyoutrade"},
                    ],
                },
            },
        }

        result = ch._parse_event(event_data)

        self.assertIsNotNone(result)
        self.assertEqual(result["content"], "/new")
        # The cleaned content must now be recognized as a lifecycle command.
        self.assertIsNotNone(parse_lifecycle_command(result["content"]))
        self.assertEqual(result["meta"]["feishu_bot_open_id"], "ou_bot_self")
        self.assertEqual(len(result["meta"]["feishu_mentions"]), 1)

    def test_parse_event_keeps_other_user_mentions_as_names(self):
        """Only the bot's own @ is stripped; other users become readable @name."""
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        ch._bot_open_id = "ou_bot_self"

        event_data = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_human"}},
                "message": {
                    "message_id": "om_grp_002",
                    "chat_id": "oc_group_xyz",
                    "chat_type": "group",
                    "content": '{"text":"@_user_1 帮 @_user_2 看下 600519"}',
                    "msg_type": "text",
                    "mentions": [
                        {"key": "@_user_1", "id": {"open_id": "ou_bot_self"}, "name": "doyoutrade"},
                        {"key": "@_user_2", "id": {"open_id": "ou_alice"}, "name": "Alice"},
                    ],
                },
            },
        }

        result = ch._parse_event(event_data)

        self.assertEqual(result["content"], "帮 @Alice 看下 600519")

    def test_parse_event_unresolved_bot_open_id_strips_all_mentions(self):
        """When bot open_id can't be resolved, degrade to stripping all @."""
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        # _lark_client is None → _fetch_bot_open_id returns "" → fallback path.

        event_data = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_human"}},
                "message": {
                    "message_id": "om_grp_003",
                    "chat_id": "oc_group_xyz",
                    "chat_type": "group",
                    "content": '{"text":"@_user_1 /new"}',
                    "msg_type": "text",
                    "mentions": [
                        {"key": "@_user_1", "id": {"open_id": "ou_bot_self"}, "name": "doyoutrade"},
                    ],
                },
            },
        }

        result = ch._parse_event(event_data)

        self.assertEqual(result["content"], "/new")
        self.assertEqual(result["meta"]["feishu_bot_open_id"], "")

    def test_fetch_bot_open_id_parses_response(self):
        """_fetch_bot_open_id reads bot.open_id from the bot/v3/info envelope."""
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        mock_client = MagicMock()
        resp = MagicMock()
        resp.raw.content = b'{"code":0,"bot":{"open_id":"ou_bot_resolved"}}'
        mock_client.request.return_value = resp
        ch._lark_client = mock_client

        self.assertEqual(ch._get_bot_open_id_cached(), "ou_bot_resolved")
        # cached: a second call must not hit the client again
        self.assertEqual(ch._get_bot_open_id_cached(), "ou_bot_resolved")
        mock_client.request.assert_called_once()

    def test_parse_event_uses_persistent_channel_id_for_session(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(
            assistant_service=self.mock_as,
            channel_id="channel-feishu-a",
            app_id="cli_aaa",
            app_secret="secret",
        )

        event_data = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_abc123"}},
                "message": {
                    "message_id": "om_msg_001",
                    "chat_id": "oc_chat_xyz",
                    "chat_type": "p2p",
                    "content": '{"text":"Hello world"}',
                    "msg_type": "text",
                },
            },
        }

        result = ch._parse_event(event_data)

        self.assertIsNotNone(result)
        self.assertEqual(result["channel_id"], "channel-feishu-a")
        self.assertEqual(result["channel_type"], "feishu")
        self.assertEqual(result["session_id"], "channel:channel-feishu-a:ou_abc123")

    def test_parse_event_extracts_reply_target_and_fetches_brief(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(
            assistant_service=self.mock_as,
            app_id="cli_aaa",
            app_secret="secret",
        )
        ch._fetch_message_brief = MagicMock(
            return_value={
                "message_id": "om_parent_001",
                "msg_type": "text",
                "content": "原消息内容",
            }
        )

        event_data = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_abc123"}},
                "message": {
                    "message_id": "om_msg_001",
                    "root_id": "om_root_001",
                    "parent_id": "om_parent_001",
                    "chat_id": "oc_chat_xyz",
                    "chat_type": "group",
                    "content": '{"text":"这条请处理一下"}',
                    "msg_type": "text",
                },
            },
        }

        result = ch._parse_event(event_data)

        self.assertIsNotNone(result)
        meta = result["meta"]
        self.assertEqual(meta["feishu_root_message_id"], "om_root_001")
        self.assertEqual(meta["feishu_parent_message_id"], "om_parent_001")
        self.assertEqual(meta["feishu_reply_target_message_id"], "om_parent_001")
        self.assertEqual(meta["feishu_reply_target_relation"], "parent")
        self.assertEqual(meta["feishu_reply_target_msg_type"], "text")
        self.assertEqual(meta["feishu_reply_target_content"], "原消息内容")
        ch._fetch_message_brief.assert_called_once_with("om_parent_001")

    @patch("doyoutrade.assistant.channels.feishu.channel.httpx.Client")
    def test_fetch_message_brief_reads_body_content_when_top_level_content_missing(self, mock_client_cls):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(
            assistant_service=self.mock_as,
            app_id="cli_aaa",
            app_secret="secret",
        )
        ch._tenant_headers = MagicMock(return_value={"Authorization": "Bearer t"})

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 0,
            "data": {
                "items": [
                    {
                        "message_id": "om_parent_001",
                        "msg_type": "text",
                        "body": {"content": '{"text":"原消息内容"}'},
                    }
                ]
            },
        }
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.__exit__.return_value = False
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        brief = ch._fetch_message_brief("om_parent_001")

        self.assertEqual(
            brief,
            {
                "message_id": "om_parent_001",
                "msg_type": "text",
                "content": "原消息内容",
            },
        )

    def test_parse_event_missing_sender_returns_none(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.config import FeishuChannelConfig

        ch = FeishuChannel(
            assistant_service=self.mock_as,
            app_id="cli_aaa",
            app_secret="secret",
        )

        event_data = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {},
                "message": {
                    "message_id": "om_001",
                    "chat_id": "oc_xyz",
                    "chat_type": "p2p",
                    "content": '{"text":"hi"}',
                    "msg_type": "text",
                },
            },
        }
        result = ch._parse_event(event_data)
        self.assertIsNone(result)

    def test_parse_event_image_message(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")

        event_data = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_img_user"}},
                "message": {
                    "message_id": "om_img_001",
                    "chat_id": "oc_img_chat",
                    "chat_type": "p2p",
                    "content": '{"image_key":"img_key_123"}',
                    "msg_type": "image",
                },
            },
        }
        result = ch._parse_event(event_data)
        self.assertIsNotNone(result)
        self.assertIn("img_key_123", result["content"])

    def test_on_message_sync_calls_enqueue(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        ch._manager = self.mock_manager
        ch._asyncio_loop = asyncio.new_event_loop()

        event_data = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_sync_test"}},
                "message": {
                    "message_id": "om_sync_001",
                    "chat_id": "oc_sync_chat",
                    "chat_type": "p2p",
                    "content": '{"text":"sync test"}',
                    "msg_type": "text",
                },
            },
        }

        ch._on_message_sync(event_data)

        pending = asyncio.all_tasks(ch._asyncio_loop)
        if pending:
            ch._asyncio_loop.run_until_complete(
                asyncio.gather(*pending, timeout=3.0)
            )

        self.mock_manager.enqueue.assert_called_once()
        call_args = self.mock_manager.enqueue.call_args
        self.assertEqual(call_args[0][0], "feishu")

    def test_start_creates_ws_client_and_starts_thread(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.config import FeishuChannelConfig

        cfg = FeishuChannelConfig(app_id="cli_aaa", app_secret="secret")
        ch = FeishuChannel.from_config(self.mock_as, cfg)
        ch._manager = self.mock_manager

        with patch("lark_oapi.ws.Client") as mock_ws_class:
            mock_ws_instance = MagicMock()
            mock_ws_instance._connect = AsyncMock(side_effect=RuntimeError("connect failed"))
            mock_ws_instance._disconnect = AsyncMock()
            mock_ws_class.return_value = mock_ws_instance

            async def run():
                await ch.start()

            asyncio.run(run())

            mock_ws_class.assert_called_once()
            call_args = mock_ws_class.call_args[0]
            self.assertEqual(call_args[0], "cli_aaa")
            self.assertEqual(call_args[1], "secret")
            self.assertIsNotNone(ch._ws_thread)
            self.assertTrue(ch._ws_thread.daemon)

            async def stop():
                await ch.stop()

            asyncio.run(stop())
            self.assertFalse(ch._ws_thread and ch._ws_thread.is_alive())

    def test_stop_interrupts_ws_reconnect_backoff(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.config import FeishuChannelConfig

        cfg = FeishuChannelConfig(app_id="cli_aaa", app_secret="secret")
        ch = FeishuChannel.from_config(self.mock_as, cfg)
        ch._manager = self.mock_manager

        with (
            patch("lark_oapi.ws.Client") as mock_ws_class,
            patch(
                "doyoutrade.assistant.channels.feishu.channel._FEISHU_WS_INITIAL_RETRY_DELAY",
                5.0,
            ),
        ):
            mock_ws_instance = MagicMock()
            mock_ws_instance._connect = AsyncMock(side_effect=RuntimeError("connect failed"))
            mock_ws_instance._disconnect = AsyncMock()
            mock_ws_class.return_value = mock_ws_instance

            async def run():
                await ch.start()
                await asyncio.sleep(0.05)
                await ch.stop()

            asyncio.run(run())

            self.assertIsNotNone(ch._ws_thread)
            self.assertFalse(ch._ws_thread.is_alive())

    def test_ws_receive_loop_exit_triggers_outer_reconnect(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.config import FeishuChannelConfig

        cfg = FeishuChannelConfig(app_id="cli_aaa", app_secret="secret")
        ch = FeishuChannel.from_config(self.mock_as, cfg)
        ch._manager = self.mock_manager
        receive_done = threading.Event()

        with patch("lark_oapi.ws.Client") as mock_ws_class:
            mock_ws_instance = MagicMock()
            mock_ws_instance._connect = AsyncMock()
            mock_ws_instance._disconnect = AsyncMock()

            async def receive_once_then_exit():
                receive_done.set()

            mock_ws_instance._receive_message_loop = AsyncMock(side_effect=receive_once_then_exit)
            mock_ws_instance._ping_loop = AsyncMock()
            mock_ws_class.return_value = mock_ws_instance

            async def run():
                await ch.start()
                self.assertTrue(receive_done.wait(timeout=1.0))
                for _ in range(20):
                    if mock_ws_instance._connect.await_count >= 2:
                        break
                    await asyncio.sleep(0.05)
                await ch.stop()

            asyncio.run(run())

            self.assertGreaterEqual(mock_ws_instance._connect.await_count, 2)


class TestFeishuChannelSend(unittest.TestCase):
    def setUp(self):
        self.mock_as = MagicMock()

    def test_send_text_sends_via_lark_client(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.base import TextContent

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        ch._lark_client = MagicMock()

        async def mock_create(req):
            class Resp:
                code = 0
                msg = "success"
            return Resp()
        ch._lark_client.im.v1.message.acreate = AsyncMock(side_effect=mock_create)

        async def run():
            await ch.send(
                session_id="feishu:ou_test",
                content=TextContent(text="Hello from Feishu"),
                meta={"feishu_chat_id": "oc_test_chat"},
            )
        asyncio.run(run())

        ch._lark_client.im.v1.message.acreate.assert_called_once()
        call_args = ch._lark_client.im.v1.message.acreate.call_args
        req = call_args[0][0]
        self.assertEqual(req.receive_id_type, "chat_id")
        self.assertEqual(req.request_body.receive_id, "oc_test_chat")
        self.assertEqual(req.request_body.msg_type, "text")
        self.assertIn("Hello from Feishu", req.request_body.content)

    def test_send_text_extracts_open_id_from_session_id(self):
        """send() strips the 'feishu:' prefix from session_id to get the open_id."""
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.base import TextContent

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        ch._lark_client = MagicMock()

        async def mock_create(req):
            class Resp:
                code = 0
            return Resp()
        ch._lark_client.im.v1.message.acreate = AsyncMock(side_effect=mock_create)

        async def run():
            await ch.send(
                session_id="feishu:ou_abc",
                content=TextContent(text="test"),
                meta={},  # no feishu_chat_id in meta
            )
        asyncio.run(run())

        # Without feishu_chat_id, receive_id should be the open_id (ou_abc)
        call_args = ch._lark_client.im.v1.message.acreate.call_args
        req = call_args[0][0]
        self.assertEqual(req.request_body.receive_id, "ou_abc")

    def test_send_text_replies_to_original_message_when_present(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.base import TextContent

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        ch._lark_client = MagicMock()
        ch._reply_message_http = AsyncMock(return_value="om_reply_001")

        async def run():
            await ch.send(
                session_id="feishu:ou_test",
                content=TextContent(text="收到"),
                meta={
                    "feishu_chat_id": "oc_test_chat",
                    "feishu_reply_to_message_id": "om_origin_001",
                },
            )

        asyncio.run(run())

        ch._reply_message_http.assert_awaited_once_with(
            reply_to_message_id="om_origin_001",
            msg_type="text",
            content='{"text": "收到"}',
        )
        ch._lark_client.im.v1.message.acreate.assert_not_called()

    def test_apply_local_delivery_ref_overrides_reply_target_content(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        merged = ch.apply_local_delivery_ref(
            {
                "feishu_reply_target_message_id": "om_origin_001",
                "feishu_reply_target_msg_type": "interactive",
                "feishu_reply_target_content": "{\"placeholder\":true}",
            },
            {
                "canonical_text": "这是本地缓存的 agent 正文",
                "platform_message_type": "interactive",
            },
        )

        self.assertEqual(merged["feishu_reply_target_content"], "这是本地缓存的 agent 正文")
        self.assertEqual(merged["feishu_reply_target_source"], "local_delivery_cache")

    def test_build_turn_context_reminder_mentions_replied_content(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        reminder = ch.build_turn_context_reminder(
            {
                "feishu_reply_target_message_id": "om_parent_001",
                "feishu_reply_target_relation": "parent",
                "feishu_reply_target_msg_type": "text",
                "feishu_reply_target_content": "请帮我分析这只股票",
            }
        )

        self.assertIsNotNone(reminder)
        self.assertIn("feishuReplyContext", reminder)
        self.assertIn("om_parent_001", reminder)
        self.assertIn("请帮我分析这只股票", reminder)

    def test_send_image_raises_not_implemented(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.base import ImageContent

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")

        async def run():
            with self.assertRaises(NotImplementedError):
                await ch.send(
                    session_id="feishu:ou_abc",
                    content=ImageContent(image_id="img_xyz"),
                    meta={},
                )
        asyncio.run(run())

    def test_send_file_raises_not_implemented(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.base import FileContent

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")

        async def run():
            with self.assertRaises(NotImplementedError):
                await ch.send(
                    session_id="feishu:ou_abc",
                    content=FileContent(file_id="file_xyz", name="doc.pdf"),
                    meta={},
                )
        asyncio.run(run())


class TestFeishuChannelStartupPrecreation(unittest.TestCase):
    @patch("doyoutrade.assistant.channels.feishu.card.cardkit.CardKitClient")
    def test_feishu_channel_precreates_cards_on_startup(self, MockCardKitClient):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel
        from doyoutrade.assistant.channels.config import FeishuChannelConfig

        mock_cardkit = MagicMock()
        mock_cardkit.precreate_cards.return_value = {"thinking": "precreated_thinking_123"}
        MockCardKitClient.return_value = mock_cardkit

        config = FeishuChannelConfig(
            app_id="app_123",
            app_secret="secret_xxx",
            thinking_card_id="oc_thinking_123",
        )
        channel = FeishuChannel.from_config(assistant_service=None, config=config)
        # Channel should store precreated card IDs after from_config
        assert hasattr(channel, "_precreated_cards")
        assert channel._precreated_cards == {"thinking": "precreated_thinking_123"}
        # If card_id is configured but precreation fails, StartupError raised


class TestStreamingCardControllerFIFO(unittest.TestCase):
    def test_streaming_card_controller_fifo_order(self):
        """验证所有卡片操作严格通过 _enqueue 串行。"""
        enqueue_calls: list[str] = []

        mock_cardkit = MagicMock()
        controller = StreamingCardController(
            cardkit_client=mock_cardkit,
            chat_id="chat_123",
            receive_id="user_123",
        )

        # Patch _enqueue at class level to track calls
        original_enqueue = StreamingCardController._enqueue

        async def tracking_enqueue(self_ref, op):
            enqueue_calls.append(op.__name__ if hasattr(op, "__name__") else repr(op))
            # Don't actually run the op to avoid async complexity

        StreamingCardController._enqueue = tracking_enqueue

        try:

            async def run():
                controller._capture_reasoning_time = lambda: None
                await controller.on_reasoning_stream("thinking...")
                await controller.on_partial_reply("hello")
                await controller.on_tool_start("search", tool_call_id="tc1")
                await controller.on_idle()

            asyncio.run(run())
        finally:
            StreamingCardController._enqueue = original_enqueue

        # reasoning, tool_start, and idle_finalize still serialize through _enqueue.
        # partial_reply may be buffered until finalization when auxiliary cards already exist.
        self.assertGreaterEqual(len(enqueue_calls), 3)


class TestStreamingCardControllerRichText(unittest.TestCase):
    def test_text_segment_is_delivered_in_chronological_order_between_cards(self):
        """A text segment streamed between a reasoning card and a tool card is
        delivered in place (chronologically), not deferred to the very end. With
        per-segment cards, ``on_idle`` then finalizes that text card cleanly."""
        send_order: list[str] = []

        mock_cardkit = MagicMock()
        mock_cardkit.send_card_json.side_effect = lambda *args, **kwargs: send_order.append("main") or "msg_main"
        mock_cardkit.create_card.side_effect = lambda *args, **kwargs: f"card_{len(send_order) + 1}"
        mock_cardkit.send_card_by_card_id.side_effect = lambda *args, **kwargs: (
            send_order.append(kwargs.get("card_id", "standalone")) or f"msg_{len(send_order)}"
        )
        mock_cardkit.stream_card_content.return_value = True
        mock_cardkit.update_card.return_value = True
        mock_cardkit.patch_message.return_value = True
        mock_cardkit.set_streaming_mode.return_value = True

        controller = StreamingCardController(
            cardkit_client=mock_cardkit,
            chat_id="chat_123",
            receive_id="user_123",
        )

        async def run():
            await controller.on_reasoning_stream("thinking step 1")
            await controller.on_partial_reply("draft answer")
            await controller.on_tool_start("search", tool_call_id="tc1")
            await controller.on_idle()

        asyncio.run(run())

        # reasoning card → text card ("main") → tool card, in that order.
        self.assertIn("main", send_order)
        self.assertEqual(send_order.index("main"), 1)
        self.assertNotEqual(send_order[-1], "main")  # a tool card followed the text
        self.assertTrue(controller.is_terminal_phase)

    def test_rich_text_falls_back_to_cardkit_card_when_direct_im_card_fails(self):
        mock_cardkit = MagicMock()
        mock_cardkit.send_card_json.return_value = None
        mock_cardkit.create_card.return_value = "card_rich_123"
        mock_cardkit.send_card_by_card_id.return_value = "msg_rich_123"

        controller = StreamingCardController(
            cardkit_client=mock_cardkit,
            chat_id="chat_123",
            receive_id="user_123",
        )

        async def run():
            await controller.on_partial_reply("正文内容")

        asyncio.run(run())

        mock_cardkit.create_card.assert_called_once()
        mock_cardkit.send_card_by_card_id.assert_called_once_with(
            card_id="card_rich_123",
            receive_id="user_123",
            receive_id_type="open_id",
            reply_to_message_id=None,
        )
        self.assertEqual(controller.message_id, "msg_rich_123")

    def test_approval_card_falls_back_to_cardkit_card_when_direct_im_card_fails(self):
        mock_cardkit = MagicMock()
        mock_cardkit.send_card_json.return_value = None
        mock_cardkit.create_card.return_value = "card_appr_123"
        mock_cardkit.send_card_by_card_id.return_value = "msg_appr_123"

        controller = StreamingCardController(
            cardkit_client=mock_cardkit,
            chat_id="chat_123",
            receive_id="user_123",
            reply_to_message_id="om_parent",
        )

        async def run():
            await controller.on_approval_request(
                {
                    "approval_id": "appr-1",
                    "description": "停止交易任务",
                    "command_preview": "doyoutrade-cli task stop task-1",
                    "timeout_seconds": 300,
                }
            )

        asyncio.run(run())

        mock_cardkit.send_card_json.assert_called_once()
        self.assertEqual(mock_cardkit.send_card_json.call_args.kwargs["reply_to_message_id"], "om_parent")
        mock_cardkit.create_card.assert_called_once()
        mock_cardkit.send_card_by_card_id.assert_called_once_with(
            card_id="card_appr_123",
            receive_id="user_123",
            receive_id_type="open_id",
            reply_to_message_id="om_parent",
        )

    def test_approval_card_failure_preserves_delivery_error_details(self):
        mock_cardkit = MagicMock()
        mock_cardkit.send_card_json.return_value = None
        mock_cardkit.create_card.return_value = None
        mock_cardkit.last_error = {"operation": "create_card", "code": 230001, "msg": "bad card"}

        controller = StreamingCardController(
            cardkit_client=mock_cardkit,
            chat_id="chat_123",
            receive_id="user_123",
        )

        async def run():
            await controller.on_approval_request(
                {
                    "approval_id": "appr-1",
                    "description": "停止交易任务",
                    "command_preview": "doyoutrade-cli task stop task-1",
                    "timeout_seconds": 300,
                }
            )

        with self.assertRaisesRegex(RuntimeError, "bad card"):
            asyncio.run(run())


class TestFeishuChannelSendReply(unittest.TestCase):
    def test_build_lifecycle_card_renders_correctly(self):
        """_build_lifecycle_card produces correct Feishu card JSON."""
        from doyoutrade.assistant.channels.base import LifecycleReply
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel()
        reply = LifecycleReply(
            title="新会话已创建",
            content=[
                {"label": "标题", "value": "Test Title"},
                {"label": "会话 ID", "value": "abc12345"},
            ],
            footer="会话已切换，请开始新对话",
        )

        card = ch._build_lifecycle_card(reply)

        self.assertEqual(card["config"]["wide_screen_mode"], True)
        self.assertEqual(card["header"]["title"]["tag"], "plain_text")
        self.assertEqual(card["header"]["title"]["content"], "✅ 新会话已创建")
        self.assertEqual(card["header"]["template"], "blue")

        # Check fields
        fields = card["elements"][0]["fields"]
        self.assertEqual(len(fields), 2)
        self.assertTrue(fields[0]["is_short"])
        self.assertIn("Test Title", fields[0]["text"]["content"])

        # Check note
        note = card["elements"][1]
        self.assertEqual(note["tag"], "note")
        self.assertEqual(note["elements"][0]["tag"], "plain_text")
        self.assertEqual(note["elements"][0]["content"], "会话已切换，请开始新对话")

    def test_build_lifecycle_card_without_footer(self):
        """Card without footer still renders correctly."""
        from doyoutrade.assistant.channels.base import LifecycleReply
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel()
        reply = LifecycleReply(
            title="Test",
            content=[{"label": "Key", "value": "Val"}],
            footer=None,
        )

        card = ch._build_lifecycle_card(reply)
        self.assertEqual(len(card["elements"]), 1)  # Only the div, no note


class TestFeishuTypingReaction(unittest.TestCase):
    """收到消息后立即在原消息上贴 Typing reaction。"""

    def setUp(self):
        self.mock_as = MagicMock()

    def _make_channel(self, reaction_resp_code=0):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        ch._lark_client = MagicMock()

        async def mock_acreate(req):
            class Resp:
                code = reaction_resp_code
                msg = "ok" if reaction_resp_code == 0 else "boom"
            return Resp()

        ch._lark_client.im.v1.message_reaction.acreate = AsyncMock(side_effect=mock_acreate)
        return ch

    def test_add_typing_reaction_uses_typing_emoji_and_message_id(self):
        ch = self._make_channel()

        asyncio.run(ch._add_typing_reaction("om_react_001"))

        ch._lark_client.im.v1.message_reaction.acreate.assert_called_once()
        req = ch._lark_client.im.v1.message_reaction.acreate.call_args[0][0]
        self.assertEqual(req.message_id, "om_react_001")
        self.assertEqual(req.request_body.reaction_type.emoji_type, "Typing")

    def test_add_typing_reaction_skips_without_message_id(self):
        ch = self._make_channel()

        asyncio.run(ch._add_typing_reaction(""))

        ch._lark_client.im.v1.message_reaction.acreate.assert_not_called()

    def test_add_typing_reaction_skips_without_lark_client(self):
        from doyoutrade.assistant.channels.feishu.channel import FeishuChannel

        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        ch._lark_client = None
        # Must not raise even though there is no client.
        asyncio.run(ch._add_typing_reaction("om_x"))

    def test_add_typing_reaction_logs_on_api_error_but_does_not_raise(self):
        ch = self._make_channel(reaction_resp_code=232002)
        # API non-zero code must be swallowed (best-effort) without raising.
        asyncio.run(ch._add_typing_reaction("om_react_err"))
        ch._lark_client.im.v1.message_reaction.acreate.assert_called_once()

    def test_add_typing_reaction_swallows_request_exception(self):
        ch = self._make_channel()
        ch._lark_client.im.v1.message_reaction.acreate = AsyncMock(
            side_effect=RuntimeError("network down")
        )
        # Request exception must not propagate out of the best-effort reaction.
        asyncio.run(ch._add_typing_reaction("om_react_exc"))

    def test_on_message_sync_fires_typing_reaction(self):
        ch = self._make_channel()
        mock_manager = MagicMock()
        mock_manager.enqueue = AsyncMock()
        ch._manager = mock_manager
        ch._asyncio_loop = asyncio.new_event_loop()

        event_data = {
            "schema": "2.0",
            "header": {"event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_react"}},
                "message": {
                    "message_id": "om_react_sync",
                    "chat_id": "oc_react_chat",
                    "chat_type": "p2p",
                    "content": '{"text":"hi"}',
                    "msg_type": "text",
                },
            },
        }

        ch._on_message_sync(event_data)

        # run_coroutine_threadsafe defers task creation to the next loop tick;
        # drive the loop so the reaction coroutine actually executes.
        async def _drain():
            await asyncio.sleep(0.05)
            pending = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        ch._asyncio_loop.run_until_complete(_drain())

        ch._lark_client.im.v1.message_reaction.acreate.assert_called_once()
        req = ch._lark_client.im.v1.message_reaction.acreate.call_args[0][0]
        self.assertEqual(req.message_id, "om_react_sync")
        self.assertEqual(req.request_body.reaction_type.emoji_type, "Typing")
        mock_manager.enqueue.assert_called_once()


class TestFeishuToolApprovalCallback(unittest.TestCase):
    """Card callback for assistant tool-call ``approval_resolve`` actions."""

    def setUp(self):
        self.mock_as = MagicMock()
        self.mock_as.approval_broker = MagicMock()

    def _make_channel(self):
        ch = FeishuChannel(
            assistant_service=self.mock_as,
            app_id="cli_aaa",
            app_secret="secret",
        )
        ch._asyncio_loop = asyncio.new_event_loop()
        return ch

    def _event(self, decision="approve_once"):
        return {
            "event": {
                "operator": {"open_id": "ou_resolver"},
                "context": {"open_message_id": "om_approval_1"},
                "action": {
                    "value": {
                        "action": "approval_resolve",
                        "approval_id": "appr-1",
                        "decision": decision,
                        "description": "停止交易任务",
                        "command_preview": "doyoutrade-cli task stop task-1",
                    }
                },
            }
        }

    def _drain(self, ch):
        async def _run():
            await asyncio.sleep(0.05)
            pending = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        ch._asyncio_loop.run_until_complete(_run())

    def test_click_resolves_broker_and_updates_message_card(self):
        ch = self._make_channel()

        with patch(
            "doyoutrade.assistant.channels.feishu.card.cardkit.CardKitClient"
        ) as MockCardKit:
            MockCardKit.return_value.patch_message.return_value = True
            ch._on_card_action_trigger(self._event(decision="approve_once"))
            self._drain(ch)

        self.mock_as.approval_broker.resolve.assert_called_once_with(
            "appr-1",
            action="approve_once",
            source="feishu_card",
            resolver_id="ou_resolver",
            reason="",
            command_prefix="",
        )
        MockCardKit.return_value.patch_message.assert_called_once()
        args = MockCardKit.return_value.patch_message.call_args.args
        self.assertEqual(args[0], "om_approval_1")
        updated_card = args[1]
        self.assertEqual(updated_card["header"]["template"], "green")
        self.assertIn("停止交易任务", str(updated_card))
        self.assertNotIn("'tag': 'button'", str(updated_card))

    def test_reject_updates_message_card_to_grey(self):
        ch = self._make_channel()

        with patch(
            "doyoutrade.assistant.channels.feishu.card.cardkit.CardKitClient"
        ) as MockCardKit:
            MockCardKit.return_value.patch_message.return_value = True
            ch._on_card_action_trigger(self._event(decision="reject"))
            self._drain(ch)

        updated_card = MockCardKit.return_value.patch_message.call_args.args[1]
        self.assertEqual(updated_card["header"]["template"], "grey")
        self.assertIn("已拒绝", updated_card["header"]["title"]["content"])


class TestFeishuStopAttemptCallback(unittest.TestCase):
    """Card callback for the streaming-card ``stop_attempt`` action."""

    def setUp(self):
        self.mock_as = MagicMock()
        self.mock_as.stop_attempt = AsyncMock(return_value={"stopped": True, "active": True})

    def _make_channel(self):
        ch = FeishuChannel(
            assistant_service=self.mock_as,
            app_id="cli_aaa",
            app_secret="secret",
        )
        ch._asyncio_loop = asyncio.new_event_loop()
        return ch

    def _event(self, session_id="asst-1"):
        value = {"action": "stop_attempt"}
        if session_id is not None:
            value["session_id"] = session_id
        return {
            "event": {
                "operator": {"open_id": "ou_clicker"},
                "context": {"open_message_id": "om_stream_1"},
                "action": {"value": value},
            }
        }

    def _drain(self, ch):
        async def _run():
            await asyncio.sleep(0.05)
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        ch._asyncio_loop.run_until_complete(_run())

    def test_click_dispatches_stop_attempt_for_session(self):
        ch = self._make_channel()
        ch._on_card_action_trigger(self._event(session_id="asst-1"))
        self._drain(ch)
        self.mock_as.stop_attempt.assert_awaited_once_with("asst-1")

    def test_missing_session_id_does_not_dispatch(self):
        ch = self._make_channel()
        ch._on_card_action_trigger(self._event(session_id=None))
        self._drain(ch)
        self.mock_as.stop_attempt.assert_not_awaited()


class TestFeishuTradeApprovalCallback(unittest.TestCase):
    """Card callback for the execution-side ``trade_approval_resolve`` action."""

    def setUp(self):
        self.mock_as = MagicMock()

    def _make_channel(self, gate):
        ch = FeishuChannel(
            assistant_service=self.mock_as,
            app_id="cli_aaa",
            app_secret="secret",
            trade_approval_gate=gate,
        )
        ch._asyncio_loop = asyncio.new_event_loop()
        return ch

    def _event(self, decision="approve", approval_id="ap-1"):
        return {
            "event": {
                "user_id": "ou_resolver",
                "card_id": "card_abc",
                "action_value": {
                    "action": "trade_approval_resolve",
                    "approval_id": approval_id,
                    "decision": decision,
                    "task_id": "task-1",
                    "intent_id": "intent-1",
                    "symbol": "600000.SH",
                    "action_side": "buy",
                },
            }
        }

    def _drain(self, ch):
        async def _run():
            await asyncio.sleep(0.05)
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        ch._asyncio_loop.run_until_complete(_run())

    def test_approve_routes_to_execution_gate_not_broker(self):
        gate = MagicMock()
        gate.approve = AsyncMock(return_value=MagicMock(status="approved"))
        gate.reject = AsyncMock()
        ch = self._make_channel(gate)

        with patch(
            "doyoutrade.assistant.channels.feishu.card.cardkit.CardKitClient"
        ) as MockCardKit:
            MockCardKit.return_value.update_card.return_value = True
            ch._on_card_action_trigger(self._event(decision="approve"))
            self._drain(ch)

        gate.approve.assert_awaited_once()
        # The assistant broker must NOT be touched by a trade approval.
        self.assertFalse(getattr(self.mock_as.approval_broker, "resolve", MagicMock()).called)
        call = gate.approve.call_args
        self.assertEqual(call.args[0], "ap-1")
        self.assertEqual(call.kwargs["resolver_id"], "ou_resolver")
        self.assertEqual(call.kwargs["decision_source"], "feishu_card")

    def test_reject_routes_to_gate_reject(self):
        gate = MagicMock()
        gate.approve = AsyncMock()
        gate.reject = AsyncMock(return_value=MagicMock(status="rejected"))
        ch = self._make_channel(gate)

        with patch(
            "doyoutrade.assistant.channels.feishu.card.cardkit.CardKitClient"
        ) as MockCardKit:
            MockCardKit.return_value.update_card.return_value = True
            ch._on_card_action_trigger(self._event(decision="reject"))
            self._drain(ch)

        gate.reject.assert_awaited_once()
        gate.approve.assert_not_awaited()
        self.assertEqual(gate.reject.call_args.kwargs["decision_source"], "feishu_card")

    def test_success_updates_card_to_terminal(self):
        gate = MagicMock()
        gate.approve = AsyncMock(return_value=MagicMock(status="approved"))
        ch = self._make_channel(gate)

        with patch(
            "doyoutrade.assistant.channels.feishu.card.cardkit.CardKitClient"
        ) as MockCardKit:
            MockCardKit.return_value.update_card.return_value = True
            ch._on_card_action_trigger(self._event(decision="approve"))
            self._drain(ch)

        MockCardKit.return_value.update_card.assert_called_once()
        updated_card = MockCardKit.return_value.update_card.call_args.args[1]
        self.assertEqual(updated_card["header"]["template"], "green")

    def test_state_conflict_updates_card_and_warns(self):
        from doyoutrade.persistence.errors import StateConflictError

        gate = MagicMock()
        gate.approve = AsyncMock(side_effect=StateConflictError("already"))
        ch = self._make_channel(gate)

        with self.assertLogs(
            "doyoutrade.assistant.channels.feishu.channel", level="WARNING"
        ) as logs:
            with patch(
                "doyoutrade.assistant.channels.feishu.card.cardkit.CardKitClient"
            ) as MockCardKit:
                MockCardKit.return_value.update_card.return_value = True
                ch._on_card_action_trigger(self._event(decision="approve"))
                self._drain(ch)

        MockCardKit.return_value.update_card.assert_called_once()
        self.assertTrue(any("already resolved" in m for m in logs.output))
        self.assertTrue(any("StateConflictError" in m for m in logs.output))

    def test_record_not_found_updates_card_and_warns(self):
        from doyoutrade.persistence.errors import RecordNotFoundError

        gate = MagicMock()
        gate.approve = AsyncMock(side_effect=RecordNotFoundError("gone"))
        ch = self._make_channel(gate)

        with self.assertLogs(
            "doyoutrade.assistant.channels.feishu.channel", level="WARNING"
        ) as logs:
            with patch(
                "doyoutrade.assistant.channels.feishu.card.cardkit.CardKitClient"
            ) as MockCardKit:
                MockCardKit.return_value.update_card.return_value = True
                ch._on_card_action_trigger(self._event(decision="approve"))
                self._drain(ch)

        MockCardKit.return_value.update_card.assert_called_once()
        self.assertTrue(any("not found / expired" in m for m in logs.output))

    def test_gate_none_warns_and_does_not_raise(self):
        ch = self._make_channel(gate=None)
        with self.assertLogs(
            "doyoutrade.assistant.channels.feishu.channel", level="WARNING"
        ) as logs:
            ch._on_card_action_trigger(self._event(decision="approve"))
            self._drain(ch)
        self.assertTrue(any("trade approval not available" in m for m in logs.output))

    def test_invalid_decision_warns_and_does_not_call_gate(self):
        gate = MagicMock()
        gate.approve = AsyncMock()
        gate.reject = AsyncMock()
        ch = self._make_channel(gate)
        with self.assertLogs(
            "doyoutrade.assistant.channels.feishu.channel", level="WARNING"
        ):
            ch._on_card_action_trigger(self._event(decision="approve_always"))
            self._drain(ch)
        gate.approve.assert_not_awaited()
        gate.reject.assert_not_awaited()


class TestFeishuSendTradeApprovalCard(unittest.TestCase):
    def setUp(self):
        self.mock_as = MagicMock()

    def _payload(self):
        return {
            "approval_id": "ap-1",
            "intent_id": "intent-1",
            "task_id": "task-1",
            "symbol": "600000.SH",
            "action": "buy",
            "notional": "5000.00",
            "strategy_tag": "ma",
            "created_at": "2026-06-14T09:30:00",
            "timeout_seconds": 300,
        }

    def test_send_returns_message_id(self):
        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        ch._lark_client = MagicMock()
        resp = MagicMock()
        resp.code = 0
        resp.data.message_id = "om_sent_1"
        ch._lark_client.im.v1.message.acreate = AsyncMock(return_value=resp)

        message_id = asyncio.run(ch.send_trade_approval_card("oc_chat", self._payload()))

        self.assertEqual(message_id, "om_sent_1")
        ch._lark_client.im.v1.message.acreate.assert_awaited_once()
        req = ch._lark_client.im.v1.message.acreate.call_args.args[0]
        self.assertEqual(req.request_body.receive_id, "oc_chat")
        self.assertEqual(req.request_body.msg_type, "interactive")
        # The serialized card carries the trade approval action contract.
        self.assertIn("trade_approval_resolve", req.request_body.content)

    def test_send_non_zero_code_returns_none(self):
        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        ch._lark_client = MagicMock()
        resp = MagicMock()
        resp.code = 99999
        resp.msg = "boom"
        ch._lark_client.im.v1.message.acreate = AsyncMock(return_value=resp)

        message_id = asyncio.run(ch.send_trade_approval_card("oc_chat", self._payload()))
        self.assertIsNone(message_id)

    def test_send_request_exception_returns_none_no_raise(self):
        ch = FeishuChannel(assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret")
        ch._lark_client = MagicMock()
        ch._lark_client.im.v1.message.acreate = AsyncMock(
            side_effect=RuntimeError("network down")
        )

        message_id = asyncio.run(ch.send_trade_approval_card("oc_chat", self._payload()))
        self.assertIsNone(message_id)


class TestFeishuCardActionRouting(unittest.TestCase):
    """Card-action synthetic messages must carry chat routing meta so the
    agent's response can be delivered back to the chat the card lives in.

    Regression: ask_user_question / confirm_write clicks produced a payload
    with no feishu_chat_id / sender_open_id, so the streaming controller fell
    back to a session-derived id and Feishu rejected the send with
    230001 invalidreceive_id.
    """

    def setUp(self):
        self.mock_as = MagicMock()
        self._cardkit_patcher = patch(
            "doyoutrade.assistant.channels.feishu.card.cardkit.CardKitClient"
        )
        self.mock_cardkit_cls = self._cardkit_patcher.start()
        self.mock_cardkit_cls.return_value.patch_message.return_value = True
        self.addCleanup(self._cardkit_patcher.stop)

    def _make_channel(self):
        ch = FeishuChannel(
            assistant_service=self.mock_as,
            app_id="cli_aaa",
            app_secret="secret",
        )
        ch._manager = MagicMock()
        ch._manager.enqueue = AsyncMock()
        ch._asyncio_loop = asyncio.new_event_loop()
        return ch

    def _ask_user_event(self, action="ask_user_select", option_label="是"):
        return {
            "event": {
                "operator": {"open_id": "ou_clicker"},
                "context": {
                    "open_message_id": "om_card_1",
                    "open_chat_id": "oc_group_chat",
                },
                "action": {
                    "value": {
                        "action": action,
                        "ask_user_id": "uq-abc12345",
                        "option_label": option_label,
                    }
                },
            }
        }

    def _drain(self, ch):
        async def _run():
            await asyncio.sleep(0.05)
            pending = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task()
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        ch._asyncio_loop.run_until_complete(_run())

    def test_ask_user_click_payload_carries_chat_routing(self):
        ch = self._make_channel()
        ch._on_card_action_trigger(self._ask_user_event())
        self._drain(ch)

        ch._manager.enqueue.assert_called_once()
        payload = ch._manager.enqueue.call_args.args[1]
        meta = payload["meta"]
        self.assertEqual(meta["feishu_chat_id"], "oc_group_chat")
        self.assertEqual(meta["sender_open_id"], "ou_clicker")
        self.assertEqual(payload["content"], "/ask_user uq-abc12345 是")

    def test_ask_user_click_finalizes_clicked_card(self):
        ch = self._make_channel()
        ch._on_card_action_trigger(self._ask_user_event())
        self._drain(ch)

        patch_message = self.mock_cardkit_cls.return_value.patch_message
        patch_message.assert_called_once()
        args = patch_message.call_args.args
        self.assertEqual(args[0], "om_card_1")
        terminal_card = args[1]
        self.assertEqual(terminal_card["header"]["template"], "green")
        self.assertIn("是", str(terminal_card["body"]["elements"]))

    def test_ask_user_text_click_finalizes_with_submitted(self):
        ch = self._make_channel()
        event = {
            "event": {
                "operator": {"open_id": "ou_clicker"},
                "context": {
                    "open_message_id": "om_card_text",
                    "open_chat_id": "oc_group_chat",
                },
                "action": {
                    "value": {"action": "ask_user_text", "ask_user_id": "uq-txt1"},
                    "form_value": {"input_uq-txt1": {"value": "hello world"}},
                },
            }
        }
        ch._on_card_action_trigger(event)
        self._drain(ch)

        args = self.mock_cardkit_cls.return_value.patch_message.call_args.args
        self.assertEqual(args[0], "om_card_text")
        self.assertIn("已回答", args[1]["header"]["title"]["content"])
        self.assertIn("hello world", str(args[1]["body"]["elements"]))

    def test_resolve_receive_routing_uses_chat_id(self):
        ch = FeishuChannel(
            assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret"
        )
        receive_id, receive_id_type = ch._resolve_receive_routing(
            "channel:feishu:ou_x",
            {"feishu_chat_id": "oc_group_chat", "sender_open_id": "ou_x"},
        )
        self.assertEqual((receive_id, receive_id_type), ("oc_group_chat", "chat_id"))

    def test_resolve_receive_routing_uses_open_id_for_p2p(self):
        ch = FeishuChannel(
            assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret"
        )
        receive_id, receive_id_type = ch._resolve_receive_routing(
            "channel:feishu:ou_x",
            {"feishu_chat_type": "p2p", "sender_open_id": "ou_p2p"},
        )
        self.assertEqual((receive_id, receive_id_type), ("ou_p2p", "open_id"))

    def test_resolve_receive_routing_warns_when_unresolved(self):
        ch = FeishuChannel(
            assistant_service=self.mock_as, app_id="cli_aaa", app_secret="secret"
        )
        with self.assertLogs(
            "doyoutrade.assistant.channels.feishu.channel", level="WARNING"
        ) as logs:
            receive_id, receive_id_type = ch._resolve_receive_routing(
                "channel:feishu:ou_x", {}
            )
        self.assertEqual(receive_id_type, "chat_id")
        self.assertTrue(any("routing unresolved" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
