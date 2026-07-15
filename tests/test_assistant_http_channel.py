import asyncio
import unittest
from unittest.mock import MagicMock
from doyoutrade.assistant.channels.http import HttpChannel
from doyoutrade.assistant.channels.config import HttpChannelConfig
from doyoutrade.assistant.channels.base import ChannelAgentRequest, TextContent


class TestHttpChannel(unittest.TestCase):
    def test_channel_type(self):
        self.assertEqual(HttpChannel.channel_type, "http")

    def test_from_config_sets_assistant_service(self):
        mock_as = MagicMock()
        ch = HttpChannel.from_config(mock_as, HttpChannelConfig())
        self.assertIs(ch._assistant_service, mock_as)

    def test_build_agent_request_from_native_full_payload(self):
        mock_as = MagicMock()
        ch = HttpChannel.from_config(mock_as, HttpChannelConfig())
        payload = {
            "session_id": "http:user1",
            "content": "hello",
            "sender_id": "user1",
            "meta": {"key": "value"},
        }
        req = ch.build_agent_request_from_native(payload)
        self.assertEqual(req.session_id, "http:user1")
        self.assertEqual(req.content, "hello")
        self.assertEqual(req.sender_id, "user1")
        self.assertEqual(req.channel_meta["key"], "value")

    def test_build_agent_request_from_native_minimal_payload(self):
        mock_as = MagicMock()
        ch = HttpChannel.from_config(mock_as, HttpChannelConfig())
        req = ch.build_agent_request_from_native({"content": "hi"})
        self.assertEqual(req.session_id, "")
        self.assertEqual(req.content, "hi")
        self.assertEqual(req.sender_id, "")

    def test_build_agent_request_from_native_non_dict(self):
        mock_as = MagicMock()
        ch = HttpChannel.from_config(mock_as, HttpChannelConfig())
        req = ch.build_agent_request_from_native("just a string")
        self.assertEqual(req.content, "just a string")

    def test_send_raises_not_implemented(self):
        mock_as = MagicMock()
        ch = HttpChannel.from_config(mock_as, HttpChannelConfig())
        with self.assertRaises(NotImplementedError) as ctx:
            asyncio.run(ch.send("s1", TextContent(text="hi"), {}))
        self.assertIn("should not be called", str(ctx.exception))

    def test_start_is_noop(self):
        mock_as = MagicMock()
        ch = HttpChannel.from_config(mock_as, HttpChannelConfig())
        asyncio.run(ch.start())
        # No errors = pass

    def test_stop_is_noop(self):
        mock_as = MagicMock()
        ch = HttpChannel.from_config(mock_as, HttpChannelConfig())
        asyncio.run(ch.stop())
        # No errors = pass

    def test_clone(self):
        mock_as = MagicMock()
        ch = HttpChannel.from_config(mock_as, HttpChannelConfig())
        cloned = ch.clone(HttpChannelConfig())
        self.assertIs(cloned._assistant_service, mock_as)
        self.assertEqual(cloned.channel_type, "http")
