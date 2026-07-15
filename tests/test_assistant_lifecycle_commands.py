import asyncio
import unittest

from doyoutrade.assistant.lifecycle_commands import parse_lifecycle_command


class AssistantLifecycleCommandTests(unittest.TestCase):
    def test_parse_new_command_with_whitespace_and_case(self):
        command = parse_lifecycle_command("  /NEW  ")

        self.assertIsNotNone(command)
        self.assertEqual(command.name, "new")
        self.assertEqual(command.arguments, "")

    def test_unknown_slash_command_is_not_lifecycle_command(self):
        self.assertIsNone(parse_lifecycle_command("/technical-basic"))

    def test_new_command_with_arguments_is_not_bare_lifecycle_command(self):
        self.assertIsNone(parse_lifecycle_command("/new please"))


class AssistantLifecycleReplyTests(unittest.TestCase):
    """Tests for LifecycleReply support in _handle_lifecycle_command."""

    def test_lifecycle_command_new_returns_reply_field(self):
        """_handle_lifecycle_command('new') returns a reply dict for channel to render."""
        from doyoutrade.assistant.service import AssistantService
        from doyoutrade.assistant.repository import InMemoryAssistantRepository

        repo = InMemoryAssistantRepository()
        svc = AssistantService(repository=repo)

        # Create a session first
        session = asyncio.run(svc.create_session(agent_id="test-agent", title="Test Session"))

        # Handle /new command
        result = asyncio.run(svc._handle_lifecycle_command(session, "new"))

        self.assertIsInstance(result, dict)
        self.assertEqual(result["lifecycle_command"]["command"], "new")
        self.assertIn("reply", result)

        reply = result["reply"]
        self.assertEqual(reply["type"], "lifecycle_notification")
        self.assertEqual(reply["title"], "新会话已创建")
        self.assertIsInstance(reply["content"], list)
        self.assertTrue(len(reply["content"]) >= 2)
        # content should have 标题 and 会话 ID
        labels = [item["label"] for item in reply["content"]]
        self.assertIn("标题", labels)
        self.assertIn("会话 ID", labels)
        self.assertEqual(reply["footer"], "会话已切换，请开始新对话")

    def test_lifecycle_reply_content_includes_new_session_info(self):
        """reply content contains new session title and short ID."""
        from doyoutrade.assistant.service import AssistantService
        from doyoutrade.assistant.repository import InMemoryAssistantRepository

        repo = InMemoryAssistantRepository()
        svc = AssistantService(repository=repo)

        session = asyncio.run(svc.create_session(agent_id="test-agent", title="My Custom Title"))
        result = asyncio.run(svc._handle_lifecycle_command(session, "new"))

        reply = result["reply"]
        content_dict = {item["label"]: item["value"] for item in reply["content"]}

        self.assertEqual(content_dict["标题"], "(无标题)")
        self.assertEqual(content_dict["会话 ID"], result["lifecycle_command"]["new_session_id"][:8])
