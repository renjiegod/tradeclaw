from __future__ import annotations

from decimal import Decimal
import unittest

from doyoutrade.assistant.session_export import build_assistant_session_export


class AssistantSessionExportTests(unittest.TestCase):
    def test_markdown_export_includes_session_agent_messages_events_and_traces(self) -> None:
        payload = build_assistant_session_export(
            session={
                "session_id": "asst-1",
                "agent_id": "agent-1",
                "title": "Validation",
                "status": "idle",
                "config": {"system_prompt_snapshot": "You are DoYouTrade."},
                "created_at": "2026-05-25T00:00:00",
                "updated_at": "2026-05-25T00:00:03",
                "last_attempt_id": "attempt-1",
            },
            agent={
                "id": "agent-1",
                "name": "Validator",
                "status": "active",
                "model_route_name": "stub-route",
                "max_turns": 6,
                "tool_names": ["execute_bash"],
                "tool_configs": [{"name": "execute_bash", "load_mode": "base"}],
                "skill_names": ["doyoutrade-backtest"],
                "resolved_system_prompt": "Resolved prompt",
                "system_prompt": "Stored prompt",
            },
            messages=[
                {
                    "message_id": "msg-u",
                    "session_id": "asst-1",
                    "role": "user",
                    "content": "run validation",
                    "created_at": "2026-05-25T00:00:01",
                    "linked_attempt_id": "attempt-1",
                    "metadata": {},
                },
                {
                    "message_id": "msg-a",
                    "session_id": "asst-1",
                    "role": "assistant",
                    "content": "done",
                    "created_at": "2026-05-25T00:00:02",
                    "linked_attempt_id": "attempt-1",
                    "metadata": {
                        "content_blocks": [
                            {"type": "thinking", "turn": 0, "content": "checking"},
                            {
                                "type": "tool_call",
                                "tool_call_id": "call-1",
                                "name": "execute_bash",
                                "arguments": {"command": "doyoutrade-cli task list"},
                                "status": "completed",
                                "result_preview": "{\"status\":\"ok\"}",
                                "is_error": False,
                            },
                            {"type": "text", "content": "done"},
                        ]
                    },
                },
            ],
            events=[
                {
                    "event_id": "evt-1",
                    "session_id": "asst-1",
                    "event_type": "attempt.completed",
                    "payload": {
                        "attempt_id": "attempt-1",
                        "run_id": "asst-run-1",
                        "trace_id": "trace-1",
                    },
                    "created_at": "2026-05-25T00:00:03",
                }
            ],
            traces={
                "items": [
                    {
                        "trace_id": "trace-1",
                        "session_id": "asst-1",
                        "span_name": "assistant.loop",
                        "created_at": "2026-05-25T00:00:01",
                        "duration_ms": 12.3,
                        "status": "ok",
                        "span_count": 2,
                        "model": "stub-model",
                        "input_tokens": 10,
                        "output_tokens": 8,
                    }
                ],
                "total": 1,
            },
            trace_details=[
                {
                    "trace_id": "trace-1",
                    "session_id": "asst-1",
                    "spans": [
                        {
                            "span_id": "span-1",
                            "trace_id": "trace-1",
                            "parent_span_id": None,
                            "session_id": "asst-1",
                            "name": "assistant.loop",
                            "span_type": "internal",
                            "start_time": "2026-05-25T00:00:01",
                            "end_time": "2026-05-25T00:00:02",
                            "duration_ms": 12.3,
                            "attributes": {"doyoutrade.run_id": "asst-run-1"},
                            "status": "ok",
                            "span_source": "assistant",
                        }
                    ],
                    "model_invocations": [
                        {
                            "id": 1,
                            "model_route_name": "stub-route",
                            "model": "stub-model",
                            "task_id": None,
                            "run_id": "asst-run-1",
                            "trace_id": "trace-1",
                            "span_id": "span-1",
                            "call_kind": "assistant_loop",
                            "input_tokens": 10,
                            "output_tokens": 8,
                            "total_tokens": 18,
                            "ok": True,
                            "error_message": None,
                            "created_at": "2026-05-25T00:00:02",
                            "request": {"messages": [{"role": "user", "content": "run validation"}]},
                            "response": {"text": "done"},
                        }
                    ],
                }
            ],
            fmt="markdown",
            include_traces=True,
        )

        self.assertEqual(payload["ids"]["session_id"], "asst-1")
        self.assertEqual(payload["ids"]["latest_attempt_id"], "attempt-1")
        self.assertEqual(payload["ids"]["run_ids"], ["asst-run-1"])
        self.assertEqual(payload["ids"]["trace_ids"], ["trace-1"])
        self.assertEqual(payload["counts"]["messages"], 2)
        self.assertEqual(payload["counts"]["events"], 1)
        self.assertEqual(payload["counts"]["traces"], 1)
        self.assertEqual(payload["counts"]["spans"], 1)
        self.assertEqual(payload["counts"]["model_invocations"], 1)
        self.assertEqual(payload["trace_details"], [
            {
                "trace_id": "trace-1",
                "session_id": "asst-1",
                "span_count": 1,
                "model_invocation_count": 1,
            }
        ])
        text = payload["export_text"]
        self.assertIn("# Assistant Session Export", text)
        self.assertIn("session_id: `asst-1`", text)
        self.assertIn("## Agent", text)
        self.assertIn("execute_bash", text)
        self.assertIn("#### Thinking", text)
        self.assertIn("Tool Call: `execute_bash`", text)
        self.assertIn("asst-run-1", text)
        self.assertIn("trace-1", text)
        self.assertIn("assistant_loop", text)

    def test_markdown_export_splits_inline_think_tags_out_of_persisted_content(self) -> None:
        """Defensive path for rows persisted before providers (e.g. MiniMax)
        had their inline <think> markup split at the source (see
        doyoutrade/models/reasoning_tags.py) — the export must not print the
        raw tag literally."""
        payload = build_assistant_session_export(
            session={"session_id": "asst-1", "agent_id": "agent-1", "config": {}},
            agent=None,
            messages=[
                {
                    "message_id": "msg-a",
                    "session_id": "asst-1",
                    "role": "assistant",
                    "content": "<think>internal reasoning</think>the visible answer",
                    "created_at": "2026-05-25T00:00:00",
                    "linked_attempt_id": "attempt-1",
                    "metadata": {
                        "content_blocks": [
                            {"type": "text", "content": "<think>internal reasoning</think>the visible answer"},
                        ]
                    },
                }
            ],
            events=[],
            traces={"items": [], "total": 0},
            trace_details=[],
            fmt="markdown",
            include_traces=False,
        )

        text = payload["export_text"]
        self.assertNotIn("<think>", text)
        self.assertIn("the visible answer", text)
        self.assertIn("internal reasoning", text)
        self.assertIn("#### Thinking (inline)", text)

    def test_json_export_keeps_structured_payload_and_omits_markdown_when_not_needed(self) -> None:
        payload = build_assistant_session_export(
            session={"session_id": "asst-1", "agent_id": "agent-1", "config": {}},
            agent=None,
            messages=[],
            events=[],
            traces={"items": [], "total": 0},
            trace_details=[],
            fmt="json",
            include_traces=False,
        )

        self.assertEqual(payload["session"]["session_id"], "asst-1")
        self.assertEqual(payload["agent"], None)
        self.assertEqual(payload["counts"]["messages"], 0)
        self.assertEqual(payload["counts"]["model_invocations"], 0)
        self.assertNotIn("export_text", payload)

    def test_export_sanitizes_decimals_to_strings(self) -> None:
        payload = build_assistant_session_export(
            session={
                "session_id": "asst-1",
                "agent_id": "agent-1",
                "config": {},
                "cash": Decimal("100000.0000000000000000"),
            },
            agent=None,
            messages=[
                {
                    "message_id": "msg-1",
                    "session_id": "asst-1",
                    "role": "assistant",
                    "content": "amount",
                    "created_at": "2026-05-25T00:00:00",
                    "linked_attempt_id": "attempt-1",
                    "metadata": {"amount": Decimal("0.10")},
                }
            ],
            events=[
                {
                    "event_id": "evt-1",
                    "session_id": "asst-1",
                    "event_type": "amount",
                    "payload": {"amount": Decimal("12345678901234567890.123400")},
                    "created_at": "2026-05-25T00:00:01",
                }
            ],
            traces={"items": [], "total": 0},
            trace_details=[
                {
                    "trace_id": "trace-1",
                    "spans": [{"attributes": {"amount": Decimal("0.10")}}],
                    "model_invocations": [
                        {
                            "run_id": "asst-run-1",
                            "trace_id": "trace-1",
                            "request": {"price": Decimal("24.7600")},
                        }
                    ],
                }
            ],
            fmt="json",
            include_traces=True,
        )

        self.assertEqual(payload["session"]["cash"], "100000")
        self.assertEqual(payload["messages"][0]["metadata"]["amount"], "0.1")
        self.assertEqual(payload["events"][0]["payload"]["amount"], "12345678901234567890.1234")
        self.assertEqual(payload["spans"][0]["attributes"]["amount"], "0.1")
        self.assertEqual(payload["model_invocations"][0]["request"]["price"], "24.76")


if __name__ == "__main__":
    unittest.main()
