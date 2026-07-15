import unittest

from doyoutrade.assistant.repository import InMemoryAssistantRepository
from doyoutrade.assistant.service import AssistantService
from doyoutrade.assistant.context_compaction.full import (
    build_full_compaction_plan,
    build_summary_boundary_metadata,
)

from doyoutrade.test_messages import AIMessage, HumanMessage, ToolMessage

from doyoutrade.assistant.context_compaction.estimation import estimate_messages_tokens
from doyoutrade.assistant.context_compaction.micro import micro_compact_messages
from doyoutrade.assistant.context_compaction.types import (
    DEFAULT_CONTEXT_COMPACTION,
    ContextCompactionConfig,
    normalize_context_compaction_config,
)

try:
    from langchain_core.messages import ToolMessage as LangChainToolMessage
except Exception:  # pragma: no cover - optional dependency
    LangChainToolMessage = None


class _NonSerializableValue:
    def __str__(self) -> str:
        return "non-serializable-value"


class ContextCompactionTypesTests(unittest.TestCase):
    def test_normalize_context_compaction_config_uses_defaults(self):
        normalized = normalize_context_compaction_config(None)

        self.assertEqual(normalized, DEFAULT_CONTEXT_COMPACTION)
        self.assertIsNot(normalized, DEFAULT_CONTEXT_COMPACTION)

    def test_normalize_context_compaction_config_merges_partial_values(self):
        normalized = normalize_context_compaction_config({
            "mode": "manual",
            "auto_threshold_tokens": 12345,
            "allow_slash_compact": False,
        })

        self.assertEqual(normalized["mode"], "manual")
        self.assertEqual(normalized["auto_threshold_tokens"], 12345)
        self.assertFalse(normalized["allow_slash_compact"])
        self.assertTrue(normalized["micro_compaction_enabled"])

    def test_context_compaction_config_as_dict_matches_defaults(self):
        config = ContextCompactionConfig()

        self.assertEqual(config.as_dict(), DEFAULT_CONTEXT_COMPACTION)


class ContextCompactionEstimationTests(unittest.TestCase):
    def test_estimate_messages_tokens_counts_text_and_tool_payloads(self):
        short_total = estimate_messages_tokens(
            [
                HumanMessage(content="hello world"),
                AIMessage(content="reply"),
            ]
        )
        long_total = estimate_messages_tokens(
            [
                HumanMessage(content="hello world"),
                AIMessage(content="reply"),
                ToolMessage(content='{"preview":"' + ("x" * 300) + '"}', tool_call_id="call-1"),
            ]
        )

        self.assertGreater(short_total, 0)
        self.assertGreater(long_total, short_total)

    def test_estimate_messages_tokens_handles_non_serializable_top_level_dict(self):
        total = estimate_messages_tokens(
            [
                ToolMessage(
                    content={"payload": _NonSerializableValue()},
                    tool_call_id="call-1",
                )
            ]
        )

        self.assertGreater(total, 0)


class ContextCompactionMicroTests(unittest.TestCase):
    def test_micro_compact_truncates_long_tool_result(self):
        tool_msg = ToolMessage(
            content='{"data":"' + ("x" * 9000) + '"}',
            tool_call_id="call-1",
        )

        compacted = micro_compact_messages(
            [tool_msg],
            tool_result_max_chars=200,
        )

        self.assertEqual(len(compacted), 1)
        self.assertIn("truncated", compacted[0].content.lower())
        self.assertLess(len(compacted[0].content), len(tool_msg.content))
        self.assertEqual(compacted[0].tool_call_id, tool_msg.tool_call_id)
        self.assertEqual(len(tool_msg.content), 9011)

    def test_micro_compact_leaves_short_tool_result_unchanged(self):
        tool_msg = ToolMessage(content={"ok": True}, tool_call_id="call-1")

        compacted = micro_compact_messages([tool_msg], tool_result_max_chars=200)

        self.assertEqual(compacted[0].content, tool_msg.content)
        self.assertIsNot(compacted[0], tool_msg)

    def test_micro_compact_skips_load_skill_tool_message(self):
        oversized_body = "skill_body " * 1000
        tool_msg = ToolMessage(
            content=oversized_body,
            tool_call_id="call-skill",
            name="load_skill",
        )

        compacted = micro_compact_messages([tool_msg], tool_result_max_chars=200)

        self.assertEqual(len(compacted), 1)
        self.assertEqual(compacted[0].content, oversized_body)
        self.assertEqual(compacted[0].name, "load_skill")
        self.assertNotIn("truncated", compacted[0].content.lower())

    def test_micro_compact_small_limit_keeps_non_empty_prefix(self):
        tool_msg = ToolMessage(
            content='{"data":"' + ("abcdef" * 20) + '"}',
            tool_call_id="call-1",
        )

        tool_result_max_chars = 8
        compacted = micro_compact_messages([tool_msg], tool_result_max_chars=tool_result_max_chars)
        compacted_content = compacted[0].content
        prefix = compacted_content.rstrip(".")

        self.assertTrue(prefix)
        self.assertTrue(tool_msg.content.startswith(prefix))
        self.assertLessEqual(len(compacted[0].content), tool_result_max_chars)
        self.assertNotEqual(compacted_content, tool_msg.content)

    @unittest.skipIf(LangChainToolMessage is None, "langchain_core not installed")
    def test_micro_compact_preserves_langchain_tool_message_fields(self):
        tool_msg = LangChainToolMessage(
            content='{"data":"' + ("x" * 500) + '"}',
            tool_call_id="call-1",
            artifact={"kind": "blob"},
            status="error",
            additional_kwargs={"source": "test"},
            response_metadata={"latency_ms": 3},
            name="tool-name",
            id="message-id",
        )

        compacted = micro_compact_messages([tool_msg], tool_result_max_chars=60)

        self.assertIsInstance(compacted[0], LangChainToolMessage)
        self.assertEqual(compacted[0].tool_call_id, "call-1")
        self.assertEqual(compacted[0].artifact, {"kind": "blob"})
        self.assertEqual(compacted[0].status, "error")
        self.assertEqual(compacted[0].additional_kwargs, {"source": "test"})
        self.assertEqual(compacted[0].response_metadata, {"latency_ms": 3})
        self.assertEqual(compacted[0].name, "tool-name")
        self.assertEqual(compacted[0].id, "message-id")
        self.assertLessEqual(len(compacted[0].content), 60)
        self.assertNotEqual(compacted[0].content, tool_msg.content)


class ContextCompactionFullTests(unittest.TestCase):
    def test_full_compaction_keeps_recent_tail_and_marks_boundary(self):
        rows = [
            {"message_id": "msg-1", "role": "user", "content": "start", "metadata": {}},
            {"message_id": "msg-2", "role": "assistant", "content": "ack", "metadata": {}},
            {"message_id": "msg-3", "role": "assistant", "content": "tool call", "metadata": {}},
            {"message_id": "msg-4", "role": "tool", "content": "tool result", "metadata": {}},
            {"message_id": "msg-5", "role": "user", "content": "more", "metadata": {}},
            {"message_id": "msg-6", "role": "assistant", "content": "more ack", "metadata": {}},
            {"message_id": "msg-7", "role": "user", "content": "latest", "metadata": {}},
        ]

        result = build_full_compaction_plan(
            history_rows=rows,
            preserve_recent_messages=3,
            preserve_recent_tool_pairs=1,
        )

        self.assertTrue(result.compacted_rows)
        self.assertGreaterEqual(len(result.tail_rows), 3)
        self.assertEqual(result.tail_rows[0]["message_id"], "msg-3")
        self.assertEqual(result.tail_rows[1]["message_id"], "msg-4")
        self.assertEqual(
            result.boundary_metadata,
            build_summary_boundary_metadata(
                compacted_until_message_id="msg-2",
                source_message_count=2,
            ),
        )


class AssistantSessionCompactionStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_session_records_compaction_state_after_full_compaction(self):
        repository = InMemoryAssistantRepository()
        service = AssistantService(repository)
        session = await repository.create_session(agent_id="agent-1", title="Compaction")

        await service.record_compaction_state(
            session["session_id"],
            summary_message_id="msg-summary",
            compacted_until_message_id="msg-2",
            raw_message_count_at_compaction=2,
        )

        updated = await repository.get_session(session["session_id"])
        self.assertIsNotNone(updated)
        state = updated["config"]["context_compaction_state"]
        self.assertEqual(state["compaction_count"], 1)
        self.assertEqual(state["summary_message_id"], "msg-summary")
        self.assertEqual(state["compacted_until_message_id"], "msg-2")
        self.assertEqual(state["raw_message_count_at_compaction"], 2)
        self.assertTrue(state["last_compacted_at"])
