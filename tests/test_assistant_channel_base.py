import unittest
from unittest.mock import MagicMock

from doyoutrade.assistant.channels.base import (
    TextContent,
    ImageContent,
    FileContent,
    AudioContent,
    ChannelAgentRequest,
    BaseChannel,
)


class TestContentPart(unittest.TestCase):
    def test_text_content_fields(self):
        c = TextContent(text="hello", markdown=True)
        self.assertEqual(c.text, "hello")
        self.assertTrue(c.markdown)

    def test_text_content_defaults(self):
        c = TextContent()
        self.assertEqual(c.text, "")
        self.assertFalse(c.markdown)

    def test_image_content_fields(self):
        c = ImageContent(image_id="img_123", url="https://example.com/img.png")
        self.assertEqual(c.image_id, "img_123")
        self.assertEqual(c.url, "https://example.com/img.png")

    def test_image_content_defaults(self):
        c = ImageContent()
        self.assertIsNone(c.image_id)
        self.assertIsNone(c.url)

    def test_file_content_fields(self):
        c = FileContent(file_id="file_abc", name="report.pdf")
        self.assertEqual(c.file_id, "file_abc")
        self.assertEqual(c.name, "report.pdf")

    def test_file_content_defaults(self):
        c = FileContent()
        self.assertIsNone(c.file_id)
        self.assertIsNone(c.name)

    def test_audio_content_fields(self):
        c = AudioContent(audio_id="audio_xyz", duration_sec=12.5)
        self.assertEqual(c.audio_id, "audio_xyz")
        self.assertEqual(c.duration_sec, 12.5)

    def test_audio_content_defaults(self):
        c = AudioContent()
        self.assertIsNone(c.audio_id)
        self.assertIsNone(c.duration_sec)


class TestChannelAgentRequest(unittest.TestCase):
    def test_fields(self):
        req = ChannelAgentRequest(
            session_id="feishu:ou_abc",
            content="buy BTC",
            sender_id="ou_abc",
            channel_meta={"feishu_chat_id": "oc_xyz"},
        )
        self.assertEqual(req.session_id, "feishu:ou_abc")
        self.assertEqual(req.content, "buy BTC")
        self.assertEqual(req.sender_id, "ou_abc")
        self.assertEqual(req.channel_meta["feishu_chat_id"], "oc_xyz")

    def test_defaults(self):
        req = ChannelAgentRequest(session_id="http:user1", content="hello")
        self.assertEqual(req.sender_id, "")
        self.assertEqual(req.channel_meta, {})

    def test_session_id_format_per_channel(self):
        req1 = ChannelAgentRequest(session_id="feishu:ou_1", content="")
        req2 = ChannelAgentRequest(session_id="http:user_a", content="")
        self.assertTrue(req1.session_id.startswith("feishu:"))
        self.assertTrue(req2.session_id.startswith("http:"))


class DummyChannel(BaseChannel):
    """Minimal concrete BaseChannel for testing."""
    channel_type = "dummy"

    def __init__(self):
        super().__init__()
        self._send_calls = []

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
        return ChannelAgentRequest(
            session_id=f"dummy:{native}",
            content=str(native),
        )

    async def send(self, session_id, content, meta):
        self._send_calls.append((session_id, content, meta))


class TestBaseChannel(unittest.TestCase):
    def setUp(self):
        self.mock_as = MagicMock()
        self.ch = DummyChannel()
        self.ch._assistant_service = self.mock_as

    def test_resolve_session_id_default(self):
        """Default resolve_session_id includes the persistent channel id."""
        self.assertEqual(
            self.ch.resolve_session_id("user123", {}),
            "channel:dummy:user123",
        )

    def test_resolve_session_id_with_meta(self):
        """resolve_session_id is unaffected by meta dict."""
        result = self.ch.resolve_session_id("alice", {"key": "value"})
        self.assertEqual(result, "channel:dummy:alice")

    def test_clone_preserves_assistant_service(self):
        """clone() uses from_config with the original _assistant_service."""
        cloned = self.ch.clone(MagicMock())
        self.assertIs(cloned._assistant_service, self.mock_as)

    def test_clone_returns_correct_channel_type(self):
        """clone() returns a channel with the same channel_type."""
        cloned = self.ch.clone(MagicMock())
        self.assertEqual(cloned.channel_type, "dummy")

    def test_from_config_receives_assistant_service(self):
        """from_config must receive and store assistant_service."""
        mock_as = MagicMock()
        ch = DummyChannel.from_config(mock_as, MagicMock())
        self.assertIs(ch._assistant_service, mock_as)
