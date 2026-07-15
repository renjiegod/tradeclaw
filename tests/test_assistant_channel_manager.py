import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from doyoutrade.assistant.channels.manager import ChannelManager
from doyoutrade.assistant.channels.base import (
    BaseChannel,
    ChannelAgentRequest,
    ChannelDeliveryHandle,
    ChannelDeliveryReceipt,
    LifecycleReply,
    TextContent,
)


class DummyChannel(BaseChannel):
    channel_type = "dummy"

    def __init__(self, channel_id: str | None = None):
        super().__init__(channel_id=channel_id)
        self.started = False
        self.stopped = False
        self._build_calls = []
        self._send_calls = []
        self._reply_calls = []

    @classmethod
    def from_config(cls, assistant_service, config):
        ch = cls()
        ch._assistant_service = assistant_service
        return ch

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def build_agent_request_from_native(self, native):
        self._build_calls.append(native)
        return ChannelAgentRequest(
            session_id=self.resolve_session_id(str(native), {}),
            sender_id=str(native),
            content=str(native),
        )

    async def send(self, session_id, content, meta):
        self._send_calls.append((session_id, content, meta))
        return None

    async def send_reply(self, session_id, reply, meta):
        self._reply_calls.append((session_id, reply, meta))
        return None


class ReplyAwareDummyChannel(DummyChannel):
    def build_agent_request_from_native(self, native):
        if isinstance(native, dict):
            return ChannelAgentRequest(
                session_id=native.get("session_id", ""),
                sender_id=native.get("sender_id", ""),
                content=native.get("content", ""),
                channel_meta=native.get("meta", {}),
            )
        return super().build_agent_request_from_native(native)

    def build_turn_context_reminder(self, meta):
        target = meta.get("reply_target")
        if not target:
            return None
        return f"<system-reminder>\n# replyTarget\n{target}\n</system-reminder>"

    def build_user_message_metadata(self, meta):
        target = meta.get("reply_target")
        if not target:
            return {}
        return {"channel": {"type": "dummy", "reply_target": target}}

    def get_reply_target_message_id(self, meta):
        return str(meta.get("reply_to_message_id") or "")

    def apply_local_delivery_ref(self, meta, delivery_ref):
        merged = dict(meta or {})
        merged["reply_target"] = delivery_ref.get("canonical_text")
        return merged


class DeliveryAwareDummyChannel(ReplyAwareDummyChannel):
    async def send(self, session_id, content, meta):
        await super().send(session_id, content, meta)
        return ChannelDeliveryReceipt(
            handles=[ChannelDeliveryHandle(platform_message_id="dummy-msg-1", platform_message_type="text")]
        )


class DummyStreamingController:
    def __init__(self):
        self.is_terminal_phase = False
        self.on_idle_calls = 0
        self.abort_calls = 0

    async def on_idle(self):
        self.on_idle_calls += 1
        self.is_terminal_phase = True

    async def abort_card(self):
        self.abort_calls += 1
        self.is_terminal_phase = True


class FailingStreamingController(DummyStreamingController):
    async def on_idle(self):
        self.on_idle_calls += 1
        raise RuntimeError("final card delivery failed")


class DummyStreamingChannel(DummyChannel):
    def __init__(self, channel_id: str | None = None):
        super().__init__(channel_id=channel_id)
        self.streaming_controller = DummyStreamingController()

    def create_streaming_controller(self, session_id, meta):
        return self.streaming_controller


class TestDummyChannelSendReply(unittest.TestCase):
    def test_dummy_channel_send_reply_does_not_raise(self):
        """send_reply() on DummyChannel should be callable without error (default pass implementation)."""
        ch = DummyChannel()
        reply = LifecycleReply(
            type="lifecycle_notification",
            title="新会话已创建",
            content=[{"label": "会话ID", "value": "new-session-123"}],
            footer="点击查看详情",
        )
        # Should not raise - default implementation is pass
        asyncio.run(ch.send_reply("session-abc", reply, {}))


class TestChannelManager(unittest.TestCase):
    def setUp(self):
        self.mock_as = MagicMock()
        self.mock_as.get_or_create_session = AsyncMock()
        self.mock_as.send_message = AsyncMock()
        self.mock_as.get_active_channel_peer_session = AsyncMock(return_value=None)
        self.mock_as.set_active_channel_peer_session = AsyncMock()
        self.mock_as.resolve_channel_delivery_ref = AsyncMock(return_value=None)
        self.mock_as.register_channel_delivery_refs = AsyncMock()
        self.mgr = ChannelManager(self.mock_as)

    def test_register_and_get(self):
        ch = DummyChannel()
        self.mgr.register(ch)
        self.assertIs(self.mgr.get("dummy"), ch)
        self.assertEqual(self.mgr.channel_types, ["dummy"])

    def test_register_duplicate_raises(self):
        self.mgr.register(DummyChannel())
        with self.assertRaises(ValueError) as ctx:
            self.mgr.register(DummyChannel())
        self.assertIn("already registered", str(ctx.exception))

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.mgr.get("nonexistent"))

    def test_start_all(self):
        ch = DummyChannel()
        self.mgr.register(ch)
        asyncio.run(self.mgr.start_all())
        self.assertTrue(ch.started)

    def test_stop_all(self):
        ch = DummyChannel()
        self.mgr.register(ch)
        asyncio.run(self.mgr.start_all())
        asyncio.run(self.mgr.stop_all())
        self.assertTrue(ch.stopped)

    def test_enqueue_unknown_channel_noops(self):
        """enqueue with unknown channel_type does nothing (no crash)."""
        asyncio.run(self.mgr.enqueue("unknown_channel", {"foo": "bar"}))
        # No exception = pass

    def test_enqueue_builds_request_and_calls_send_message(self):
        ch = DummyChannel()
        self.mgr.register(ch)

        async def run_and_wait():
            task = asyncio.create_task(self.mgr.enqueue("dummy", "hello"))
            await asyncio.sleep(0.05)  # let the task run
            return task

        asyncio.run(run_and_wait())

        self.mock_as.get_or_create_session.assert_called_once()
        self.mock_as.send_message.assert_called_once_with(
            session_id="channel:dummy:hello",
            content="hello",
        )

    def test_new_lifecycle_command_rebinds_channel_peer_to_new_session(self):
        ch = DummyChannel()
        self.mgr.register(ch)

        self.mock_as.send_message.side_effect = [
            {
                "session": {"session_id": "channel:dummy:hello:new-1"},
                "messages": [],
                "lifecycle_command": {
                    "command": "new",
                    "previous_session_id": "channel:dummy:hello",
                    "new_session_id": "channel:dummy:hello:new-1",
                },
            },
            {
                "messages": [
                    {"role": "assistant", "content": "ok"},
                ],
            },
        ]

        async def run_and_wait():
            await self.mgr.enqueue("dummy", "hello")
            await asyncio.sleep(0.05)
            await self.mgr.enqueue("dummy", "hello")
            await asyncio.sleep(0.05)

        asyncio.run(run_and_wait())

        first_call, second_call = self.mock_as.send_message.await_args_list
        self.assertEqual(first_call.kwargs["session_id"], "channel:dummy:hello")
        self.assertEqual(second_call.kwargs["session_id"], "channel:dummy:hello:new-1")

    def test_enqueue_passes_turn_context_and_user_message_metadata(self):
        ch = ReplyAwareDummyChannel()
        self.mgr.register(ch)
        self.mock_as.send_message.return_value = {
            "messages": [{"role": "assistant", "content": "ok"}],
        }

        async def run_and_wait():
            await self.mgr.enqueue(
                "dummy",
                {
                    "session_id": "channel:dummy:user-1",
                    "sender_id": "user-1",
                    "content": "请继续",
                    "meta": {"reply_target": "上一条策略建议"},
                },
            )
            await asyncio.sleep(0.05)

        asyncio.run(run_and_wait())

        kwargs = self.mock_as.send_message.await_args.kwargs
        self.assertEqual(kwargs["session_id"], "channel:dummy:user-1")
        self.assertEqual(kwargs["content"], "请继续")
        self.assertIn("replyTarget", kwargs["turn_context_reminder"])
        self.assertEqual(
            kwargs["user_message_metadata"],
            {"channel": {"type": "dummy", "reply_target": "上一条策略建议"}},
        )

    def test_enqueue_hydrates_reply_context_from_local_delivery_ref(self):
        ch = ReplyAwareDummyChannel()
        self.mgr.register(ch)
        self.mock_as.resolve_channel_delivery_ref.return_value = {
            "canonical_text": "这是 agent 上一条标准正文",
        }
        self.mock_as.send_message.return_value = {
            "messages": [{"role": "assistant", "content": "ok"}],
        }

        async def run_and_wait():
            await self.mgr.enqueue(
                "dummy",
                {
                    "session_id": "channel:dummy:user-2",
                    "sender_id": "user-2",
                    "content": "请继续",
                    "meta": {"reply_to_message_id": "dummy-msg-1"},
                },
            )
            await asyncio.sleep(0.05)

        asyncio.run(run_and_wait())

        self.mock_as.resolve_channel_delivery_ref.assert_awaited_once_with(
            "channel:dummy:user-2",
            channel_type="dummy",
            platform_message_id="dummy-msg-1",
        )
        kwargs = self.mock_as.send_message.await_args.kwargs
        self.assertIn("这是 agent 上一条标准正文", kwargs["turn_context_reminder"])
        self.assertEqual(
            kwargs["user_message_metadata"],
            {"channel": {"type": "dummy", "reply_target": "这是 agent 上一条标准正文"}},
        )

    def test_enqueue_registers_outbound_delivery_refs(self):
        ch = DeliveryAwareDummyChannel()
        self.mgr.register(ch)
        self.mock_as.send_message.return_value = {
            "messages": [{"role": "assistant", "message_id": "msg-a1", "content": "assistant answer"}],
        }

        async def run_and_wait():
            await self.mgr.enqueue(
                "dummy",
                {
                    "session_id": "channel:dummy:user-3",
                    "sender_id": "user-3",
                    "content": "hello",
                    "meta": {},
                },
            )
            await asyncio.sleep(0.05)

        asyncio.run(run_and_wait())

        self.mock_as.register_channel_delivery_refs.assert_awaited_once()
        kwargs = self.mock_as.register_channel_delivery_refs.await_args.kwargs
        self.assertEqual(kwargs["channel_type"], "dummy")
        self.assertEqual(kwargs["canonical_text"], "assistant answer")
        self.assertEqual(kwargs["source"], "assistant_message")
        self.assertEqual(kwargs["assistant_message_id"], "msg-a1")
        self.assertEqual(kwargs["handles"][0].platform_message_id, "dummy-msg-1")

    def test_streaming_channel_does_not_send_duplicate_text_reply(self):
        ch = DummyStreamingChannel()
        self.mgr.register(ch)
        self.mock_as.send_message.return_value = {
            "messages": [
                {"role": "assistant", "content": "card already sent this text"},
            ]
        }

        async def run_and_wait():
            await self.mgr.enqueue("dummy", "hello")
            await asyncio.sleep(0.05)

        asyncio.run(run_and_wait())

        self.mock_as.send_message.assert_called_once()
        kwargs = self.mock_as.send_message.await_args.kwargs
        self.assertIs(kwargs["streaming_controller"], ch.streaming_controller)
        self.assertEqual(ch.streaming_controller.on_idle_calls, 1)
        self.assertEqual(ch._send_calls, [])

    def test_streaming_channel_falls_back_to_plain_text_when_finalize_fails(self):
        ch = DummyStreamingChannel()
        ch.streaming_controller = FailingStreamingController()
        self.mgr.register(ch)
        self.mock_as.send_message.return_value = {
            "messages": [
                {"role": "assistant", "content": "final fallback text"},
            ]
        }

        async def run_and_wait():
            await self.mgr.enqueue("dummy", "hello")
            await asyncio.sleep(0.05)

        asyncio.run(run_and_wait())

        self.mock_as.send_message.assert_called_once()
        self.assertEqual(ch.streaming_controller.on_idle_calls, 1)
        self.assertEqual(len(ch._send_calls), 1)
        session_id, content, meta = ch._send_calls[0]
        self.assertEqual(session_id, "channel:dummy:hello")
        self.assertIsInstance(content, TextContent)
        self.assertEqual(content.text, "final fallback text")

    def test_same_type_channels_route_to_bound_agents_by_channel_id(self):
        ch1 = DummyChannel(channel_id="channel-a")
        ch2 = DummyChannel(channel_id="channel-b")
        self.mgr.register(ch1, agent_id="agent-a")
        self.mgr.register(ch2, agent_id="agent-b")

        async def run_and_wait():
            await self.mgr.enqueue("channel-a", "same-user")
            await self.mgr.enqueue("channel-b", "same-user")
            await asyncio.sleep(0.05)

        asyncio.run(run_and_wait())

        calls = self.mock_as.get_or_create_session.await_args_list
        self.assertEqual(calls[0].kwargs["agent_id"], "agent-a")
        self.assertEqual(calls[0].kwargs["session_id"], "channel:channel-a:same-user")
        self.assertEqual(calls[1].kwargs["agent_id"], "agent-b")
        self.assertEqual(calls[1].kwargs["session_id"], "channel:channel-b:same-user")

    def test_start_all_with_multiple_channels(self):
        ch1 = DummyChannel()
        ch2 = DummyChannel()
        ch2.channel_id = "dummy2"
        self.mgr.register(ch1)
        self.mgr.register(ch2)

        asyncio.run(self.mgr.start_all())

        self.assertTrue(ch1.started)
        self.assertTrue(ch2.started)


class TestChannelManagerStartStopErrors(unittest.TestCase):
    def setUp(self):
        self.mock_as = MagicMock()

    def test_start_all_continues_when_channel_start_raises(self):
        """return_exceptions=True means one channel's failure doesn't stop others."""
        good_ch = DummyChannel(channel_id="good")

        bad_ch = DummyChannel(channel_id="bad")

        async def raising_start():
            raise RuntimeError("connection failed")

        bad_ch.start = raising_start

        mgr = ChannelManager(self.mock_as)
        mgr.register(good_ch)
        mgr.register(bad_ch)

        # start_all should NOT raise despite bad_ch failing
        try:
            asyncio.run(mgr.start_all())
        except Exception as e:
            raise AssertionError(f"start_all() raised {e} — return_exceptions=True not working") from e

        # good_ch should have been started despite bad_ch failing
        self.assertTrue(good_ch.started)

    def test_stop_all_continues_when_channel_stop_raises(self):
        """return_exceptions=True means one channel's failure doesn't stop others."""
        good_ch = DummyChannel(channel_id="good")

        bad_ch = DummyChannel(channel_id="bad")

        async def raising_stop():
            raise RuntimeError("cleanup failed")

        bad_ch.stop = raising_stop

        mgr = ChannelManager(self.mock_as)
        mgr.register(good_ch)
        mgr.register(bad_ch)

        try:
            asyncio.run(mgr.stop_all())
        except Exception as e:
            raise AssertionError(f"stop_all() raised {e} — return_exceptions=True not working") from e

        self.assertTrue(good_ch.stopped)


class TestChannelManagerLifecycleReply(unittest.TestCase):
    def setUp(self):
        self.mock_as = MagicMock()
        self.mock_as.get_or_create_session = AsyncMock()
        self.mock_as.send_message = AsyncMock()
        self.mock_as.get_active_channel_peer_session = AsyncMock(return_value=None)
        self.mock_as.set_active_channel_peer_session = AsyncMock()
        self.mgr = ChannelManager(self.mock_as)

    def test_new_lifecycle_command_triggers_send_reply(self):
        """When /new command returns a reply, ChannelManager calls channel.send_reply()."""
        ch = DummyChannel()
        self.mgr.register(ch)

        self.mock_as.send_message.return_value = {
            "session": {"session_id": "new-session-id"},
            "messages": [],
            "lifecycle_command": {
                "command": "new",
                "previous_session_id": "old-session",
                "new_session_id": "new-session-id",
            },
            "reply": {
                "type": "lifecycle_notification",
                "title": "新会话已创建",
                "content": [
                    {"label": "标题", "value": "Test"},
                    {"label": "会话 ID", "value": "new-sess"},
                ],
                "footer": "会话已切换，请开始新对话",
            },
        }

        async def run_and_wait():
            await self.mgr.enqueue("dummy", "hello")
            await asyncio.sleep(0.05)

        asyncio.run(run_and_wait())

        # Verify send_reply was called
        self.assertEqual(len(ch._reply_calls), 1)
        session_id, reply, meta = ch._reply_calls[0]
        self.assertEqual(session_id, "channel:dummy:hello")
        self.assertEqual(reply.title, "新会话已创建")
        self.assertEqual(len(reply.content), 2)

    def test_send_reply_failure_does_not_crash_manager(self):
        """If channel.send_reply() raises, manager continues normally."""
        ch = DummyChannel()
        self.mgr.register(ch)

        async def failing_send_reply(session_id, reply, meta):
            raise RuntimeError("send_reply failed")

        ch.send_reply = failing_send_reply

        self.mock_as.send_message.return_value = {
            "session": {"session_id": "new-session"},
            "messages": [],
            "lifecycle_command": {"command": "new", "previous_session_id": "old", "new_session_id": "new-session"},
            "reply": {
                "type": "lifecycle_notification",
                "title": "新会话已创建",
                "content": [{"label": "标题", "value": "Test"}],
            },
        }

        async def run_and_wait():
            await self.mgr.enqueue("dummy", "hello")
            await asyncio.sleep(0.05)

        # Should not raise
        asyncio.run(run_and_wait())
        # Session should still be rebound
        self.assertEqual(
            self.mgr._active_peer_sessions.get(("dummy", "channel:dummy:hello")),
            "new-session",
        )


class TestChannelManagerPeerSessionPersistence(unittest.TestCase):
    """A channel /new rebinding must survive a manager restart (durable store)."""

    def test_new_rebinding_survives_manager_restart(self):
        store: dict[tuple[str, str], str] = {}

        async def fake_get(channel_id, peer):
            return store.get((channel_id, peer))

        async def fake_set(channel_id, peer, active):
            store[(channel_id, peer)] = active

        svc = MagicMock()
        svc.get_or_create_session = AsyncMock()
        svc.get_active_channel_peer_session = AsyncMock(side_effect=fake_get)
        svc.set_active_channel_peer_session = AsyncMock(side_effect=fake_set)
        svc.send_message = AsyncMock(
            side_effect=[
                {  # first message behaves like /new and rebinds the peer
                    "session": {"session_id": "asst-new"},
                    "messages": [],
                    "lifecycle_command": {
                        "command": "new",
                        "previous_session_id": "channel:dummy:hello",
                        "new_session_id": "asst-new",
                    },
                },
                {"messages": [{"role": "assistant", "content": "ok"}]},  # after restart
            ]
        )

        # First manager: receives /new, persists the rebinding to the store.
        mgr1 = ChannelManager(svc)
        mgr1.register(DummyChannel())

        async def first():
            await mgr1.enqueue("dummy", "hello")
            await asyncio.sleep(0.05)

        asyncio.run(first())
        self.assertEqual(store.get(("dummy", "channel:dummy:hello")), "asst-new")

        # Simulate a server restart: a brand-new manager has an empty in-memory
        # cache but reads the same durable store.
        mgr2 = ChannelManager(svc)
        mgr2.register(DummyChannel())

        async def second():
            await mgr2.enqueue("dummy", "hello")
            await asyncio.sleep(0.05)

        asyncio.run(second())

        # The post-restart message routes to the rebound session, not back to the peer.
        second_call = svc.send_message.await_args_list[1]
        self.assertEqual(second_call.kwargs["session_id"], "asst-new")

    def test_peer_session_lookup_failure_falls_back_to_peer(self):
        """A durable-store read error degrades to the peer session (no crash, no silent misroute)."""
        svc = MagicMock()
        svc.get_or_create_session = AsyncMock()
        svc.send_message = AsyncMock(return_value={"messages": []})
        svc.set_active_channel_peer_session = AsyncMock()
        svc.get_active_channel_peer_session = AsyncMock(side_effect=RuntimeError("db down"))

        mgr = ChannelManager(svc)
        mgr.register(DummyChannel())

        async def run_and_wait():
            await mgr.enqueue("dummy", "hello")
            await asyncio.sleep(0.05)

        asyncio.run(run_and_wait())

        svc.send_message.assert_called_once()
        self.assertEqual(svc.send_message.await_args.kwargs["session_id"], "channel:dummy:hello")
