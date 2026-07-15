"""Tests for model invocation recording and persistence."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from doyoutrade.agent_runtime import AgentTurnResponse
from doyoutrade.models.base import ModelAdapter, ModelRequest, ModelResponse
from doyoutrade.models.invocation_context import model_invocation_scope
from doyoutrade.test_messages import AIMessage, HumanMessage, PseudoToolCall, SystemMessage, ToolMessage

from doyoutrade.models.providers import (
    AnthropicAdapter,
    OpenAICompatibleAdapter,
    serialized_chat_invocation_request,
    serialized_model_invocation_request,
)
from doyoutrade.models.providers._common import (
    PseudoAIMessage,
    PseudoToolCall as SdkPseudoToolCall,
    _first_chat_completion_message,
    build_anthropic_messages_from_lc_turns,
    recordable_anthropic_sdk_response,
)
from doyoutrade.models.recording import RecordingModelAdapter
from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.models import Base, ModelInvocationRecord
from doyoutrade.persistence.repositories import SqlAlchemyModelInvocationRepository


class SerializedModelInvocationRequestTests(unittest.TestCase):
    def test_openai_compatible_shape(self) -> None:
        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.2,
            max_tokens=128,
            timeout_seconds=30,
        )
        body = serialized_model_invocation_request(
            adapter,
            ModelRequest(system_prompt="S", user_prompt="U"),
        )
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(body["max_tokens"], 128)
        self.assertEqual(
            body["messages"],
            [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "U"},
            ],
        )
        self.assertIsNone(body.get("tools"))

    def test_openai_tools_from_request(self) -> None:
        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.2,
            max_tokens=128,
            timeout_seconds=30,
        )
        tool_def = [{"type": "function", "function": {"name": "foo", "parameters": {}}}]
        body = serialized_model_invocation_request(
            adapter,
            ModelRequest(system_prompt="S", user_prompt="U", tools=tool_def),
        )
        self.assertEqual(body["tools"], tool_def)

    def test_anthropic_shape(self) -> None:
        adapter = AnthropicAdapter(
            model="claude-test",
            api_key="k",
            temperature=0.4,
            max_tokens=256,
            timeout_seconds=30,
        )
        body = serialized_model_invocation_request(
            adapter,
            ModelRequest(system_prompt="S", user_prompt="U"),
        )
        self.assertEqual(body["model"], "claude-test")
        self.assertEqual(body["temperature"], 0.4)
        self.assertEqual(body["max_tokens"], 256)
        self.assertEqual(body["system"], "S")
        self.assertEqual(body["messages"], [{"role": "user", "content": "U"}])
        self.assertIsNone(body.get("tools"))

    def test_anthropic_shape_includes_thinking_when_configured(self) -> None:
        adapter = AnthropicAdapter(
            model="claude-test",
            api_key="k",
            temperature=0.4,
            max_tokens=256,
            timeout_seconds=30,
            thinking={"type": "enabled", "budget_tokens": 1024},
        )
        body = serialized_model_invocation_request(
            adapter,
            ModelRequest(system_prompt="S", user_prompt="U"),
        )
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 1024})

    def test_anthropic_shape_includes_cache_control_when_configured(self) -> None:
        adapter = AnthropicAdapter(
            model="claude-test",
            api_key="k",
            temperature=0.4,
            max_tokens=256,
            timeout_seconds=30,
            cache_control={"type": "ephemeral"},
        )
        body = serialized_model_invocation_request(
            adapter,
            ModelRequest(system_prompt="S", user_prompt="U"),
        )
        self.assertEqual(body["cache_control"], {"type": "ephemeral"})

    def test_recordable_anthropic_sdk_response_prefers_wire_usage(self) -> None:
        from anthropic.types.message import Message
        from anthropic.types.text_block import TextBlock
        from anthropic.types.usage import Usage

        text_block = TextBlock(type="text", text="hi")
        usage = Usage(input_tokens=12, output_tokens=34)
        msg = Message(
            id="msg-1",
            content=[text_block],
            model="claude-3-5-sonnet-20241022",
            role="assistant",
            type="message",
            usage=usage,
        )
        wire = {
            "usage": {
                "input_tokens": 12,
                "output_tokens": 34,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 14331,
            }
        }
        out = recordable_anthropic_sdk_response(msg, wire)
        self.assertEqual(out["usage"]["cache_read_input_tokens"], 14331)
        self.assertEqual(out["usage"]["cache_creation_input_tokens"], 0)
        self.assertIsNone(recordable_anthropic_sdk_response(msg, None)["usage"]["cache_read_input_tokens"])

    def test_openai_tools_from_request_body(self) -> None:
        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.2,
            max_tokens=128,
            timeout_seconds=30,
        )
        tool_def = [{"type": "function", "function": {"name": "from_request", "parameters": {}}}]
        body = serialized_model_invocation_request(
            adapter,
            ModelRequest(system_prompt="S", user_prompt="U", tools=tool_def),
        )
        self.assertEqual(body["tools"], tool_def)


class SerializedChatInvocationRequestTests(unittest.TestCase):
    def test_openai_serializes_message_roles(self) -> None:
        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.2,
            max_tokens=128,
            timeout_seconds=30,
        )
        msgs = [
            SystemMessage(content="S"),
            HumanMessage(content="U"),
            AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1", "type": "tool_call"}]),
        ]
        tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
        body = serialized_chat_invocation_request(adapter, msgs, tools)
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(len(body["messages"]), 3)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("tool_calls", body["messages"][2])

    def test_openai_serializes_dataclass_tool_calls(self) -> None:
        """PseudoToolCall dataclass (production SDK path) must serialize same as dict-style."""
        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.2,
            max_tokens=128,
            timeout_seconds=30,
        )
        msgs = [
            SystemMessage(content="S"),
            HumanMessage(content="U"),
            AIMessage(
                content="",
                tool_calls=[
                    PseudoToolCall(name="data_bars_relative", args='{"symbol": "600000.SH"}', id="call_1"),
                ],
            ),
        ]
        tools = [{"type": "function", "function": {"name": "data_bars_relative", "parameters": {}}}]
        body = serialized_chat_invocation_request(adapter, msgs, tools)
        self.assertEqual(body["model"], "gpt-test")
        tc = body["messages"][2]["tool_calls"][0]
        self.assertEqual(tc["type"], "function")
        self.assertEqual(tc["function"]["name"], "data_bars_relative")
        self.assertEqual(tc["function"]["arguments"], '{"symbol": "600000.SH"}')
        self.assertEqual(tc["id"], "call_1")


class _OpenAIMessagesConversionTests(unittest.TestCase):
    """Verify _messages_to_dicts preserves tool_call_id on tool result messages."""

    def test_tool_message_preserves_tool_call_id(self) -> None:
        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.2,
            max_tokens=128,
            timeout_seconds=30,
        )
        msgs = [
            SystemMessage(content="system"),
            HumanMessage(content="user"),
            # Simulate assistant tool call (role=assistant)
            AIMessage(
                content="",
                tool_calls=[
                    PseudoToolCall(name="data_bars_relative", args='{"symbol": "600000.SH"}', id="call_abc123"),
                ],
            ),
            # Tool result message
            ToolMessage(content='{"ok": true}', tool_call_id="call_abc123"),
        ]
        # Only pass tools to exercise the multi-turn path
        tools = [{"type": "function", "function": {"name": "data_bars_relative", "parameters": {}}}]
        body = serialized_chat_invocation_request(adapter, msgs, tools)
        converted_msgs = body["messages"]
        # Assistant message (OpenAI wire role)
        self.assertEqual(converted_msgs[2]["role"], "assistant")
        # Tool result must include tool_call_id
        self.assertEqual(converted_msgs[3]["role"], "tool")
        self.assertEqual(converted_msgs[3]["tool_call_id"], "call_abc123")
        self.assertIn('"ok"', converted_msgs[3]["content"])

    def test_messages_to_dicts_includes_assistant_tool_calls_for_api(self) -> None:
        """Regression: API request must include assistant tool_calls before tool role messages."""
        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.2,
            max_tokens=128,
            timeout_seconds=30,
        )
        msgs = [
            SystemMessage(content="system"),
            HumanMessage(content="user"),
            PseudoAIMessage(
                content="",
                tool_calls=[
                    PseudoToolCall(
                        name="data_bars_relative",
                        args='{"symbol": "600000.SH"}',
                        id="call_function_3lazu65sdbzp_1",
                    ),
                ],
            ),
            ToolMessage(
                content='{"bars": []}',
                tool_call_id="call_function_3lazu65sdbzp_1",
            ),
        ]
        converted = adapter._messages_to_dicts(msgs)
        self.assertEqual(converted[2]["role"], "assistant")
        self.assertIn("tool_calls", converted[2])
        tc0 = converted[2]["tool_calls"][0]
        self.assertEqual(tc0["id"], "call_function_3lazu65sdbzp_1")
        self.assertEqual(tc0["type"], "function")
        self.assertEqual(tc0["function"]["name"], "data_bars_relative")
        self.assertEqual(tc0["function"]["arguments"], '{"symbol": "600000.SH"}')
        self.assertEqual(converted[3]["role"], "tool")
        self.assertEqual(converted[3]["tool_call_id"], "call_function_3lazu65sdbzp_1")


class AnthropicLcMessagesConversionTests(unittest.TestCase):
    def test_multi_turn_preserves_tool_use_and_merges_tool_results(self) -> None:
        """Anthropic API needs assistant tool_use blocks plus user tool_result blocks (not a flat string)."""
        rest = [
            HumanMessage(content="ping"),
            PseudoAIMessage(
                content="",
                tool_calls=[
                    SdkPseudoToolCall(name="data_bars_relative", args='{"symbol": "X"}', id="toolu_01AAA"),
                    SdkPseudoToolCall(name="data_bars_relative", args='{"symbol": "Y"}', id="toolu_01BBB"),
                ],
            ),
            ToolMessage(content='{"ok": 1}', tool_call_id="toolu_01AAA"),
            ToolMessage(content='{"ok": 2}', tool_call_id="toolu_01BBB"),
        ]
        api_msgs = build_anthropic_messages_from_lc_turns(rest)
        self.assertEqual(len(api_msgs), 3)
        self.assertEqual(api_msgs[0]["role"], "user")
        self.assertEqual(api_msgs[0]["content"][0]["type"], "text")
        self.assertEqual(api_msgs[1]["role"], "assistant")
        tu = [b for b in api_msgs[1]["content"] if b["type"] == "tool_use"]
        self.assertEqual(len(tu), 2)
        self.assertEqual(tu[0]["id"], "toolu_01AAA")
        self.assertEqual(tu[0]["input"], {"symbol": "X"})
        self.assertEqual(api_msgs[2]["role"], "user")
        tr = [b for b in api_msgs[2]["content"] if b["type"] == "tool_result"]
        self.assertEqual(len(tr), 2)
        self.assertEqual({x["tool_use_id"] for x in tr}, {"toolu_01AAA", "toolu_01BBB"})

    def test_tool_results_merge_companion_skill_text_in_same_user_turn(self) -> None:
        """invoke_skill: short tool_result + SKILL body in same Anthropic user message."""
        rest = [
            HumanMessage(content="ping"),
            PseudoAIMessage(
                content="",
                tool_calls=[
                    SdkPseudoToolCall(name="invoke_skill", args='{"skill": "x"}', id="toolu_skill"),
                ],
            ),
            ToolMessage(
                content='{"success": true, "skill_invoked": "x"}',
                tool_call_id="toolu_skill",
                companion_user_text="<invoke_skill_loaded>x body</invoke_skill_loaded>",
            ),
        ]
        api_msgs = build_anthropic_messages_from_lc_turns(rest)
        self.assertEqual(len(api_msgs), 3)
        last = api_msgs[2]["content"]
        tr = [b for b in last if b["type"] == "tool_result"]
        tx = [b for b in last if b["type"] == "text"]
        self.assertEqual(len(tr), 1)
        self.assertEqual(len(tx), 1)
        self.assertIn("x body", tx[0]["text"])


class OpenAIMessagesCompanionTests(unittest.TestCase):
    def test_consecutive_tool_companions_flush_one_user_message(self) -> None:
        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.2,
            max_tokens=128,
            timeout_seconds=30,
        )
        msgs = [
            HumanMessage(content="u"),
            PseudoAIMessage(
                content="",
                tool_calls=[
                    SdkPseudoToolCall(name="a", args="{}", id="c1"),
                    SdkPseudoToolCall(name="b", args="{}", id="c2"),
                ],
            ),
            ToolMessage(content="{}", tool_call_id="c1", companion_user_text="skill-a"),
            ToolMessage(content="{}", tool_call_id="c2", companion_user_text="skill-b"),
            HumanMessage(content="next"),
        ]
        converted = adapter._messages_to_dicts(msgs)
        self.assertEqual(converted[2]["role"], "tool")
        self.assertEqual(converted[3]["role"], "tool")
        self.assertEqual(converted[4]["role"], "user")
        self.assertIn("skill-a", converted[4]["content"])
        self.assertIn("skill-b", converted[4]["content"])
        self.assertEqual(converted[5]["role"], "user")
        self.assertEqual(converted[5]["content"], "next")


class _StubInner(ModelAdapter):
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def generate(self, request: ModelRequest) -> ModelResponse:
        if self.fail:
            raise RuntimeError("boom")
        return ModelResponse(
            text="{}",
            raw=SimpleNamespace(
                content="{}",
                model="anthropic-model",
                response_metadata={"time_to_first_token_ms": 42},
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            ),
        )


class _StubChatFail(ModelAdapter):
    """Inner adapter with ``chat_ainvoke`` that raises (for recording error-path tests)."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    def generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def chat_ainvoke(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        raise self._exc


class _StubChatSuccess(ModelAdapter):
    def generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def chat_ainvoke(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        raw = AIMessage(
            content="",
            tool_calls=[
                PseudoToolCall(
                    name="data_bars_relative",
                    args='{"symbol": "600000.SH"}',
                    id="call_1",
                ),
            ],
        )
        raw.usage_metadata = {
            "input_tokens": 7,
            "output_tokens": 3,
            "total_tokens": 10,
        }
        return ModelResponse(text="", raw=raw)


class _StubChatText(ModelAdapter):
    def generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def chat_ainvoke(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        raw = AIMessage(content="hello", tool_calls=[])
        return ModelResponse(text="hello", raw=raw)


class _StubAgentTurnStreaming(ModelAdapter):
    def generate(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def agent_turn(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_text_delta=None,
        on_thinking_delta=None,
    ) -> AgentTurnResponse:
        if on_thinking_delta is not None:
            maybe = on_thinking_delta("thinking...")
            if hasattr(maybe, "__await__"):
                await maybe
        if on_text_delta is not None:
            maybe = on_text_delta("he")
            if hasattr(maybe, "__await__"):
                await maybe
            maybe = on_text_delta("llo")
            if hasattr(maybe, "__await__"):
                await maybe
        return AgentTurnResponse(
            content="hello",
            tool_calls=[],
            raw=AIMessage(content="hello", tool_calls=[]),
            request_payload={"messages": []},
            response_payload={"streamed": True},
            usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )


class _StubWithPseudoToolCall(ModelAdapter):
    """Returns PseudoAIMessage with PseudoToolCall dataclass instances (production SDK path)."""

    def generate(self, request: ModelRequest) -> ModelResponse:
        raw = AIMessage(
            content="{}",
            tool_calls=[
                PseudoToolCall(name="data_bars_relative", args='{"symbol": "600000.SH"}', id="call_1"),
            ],
        )
        return ModelResponse(text="{}", raw=raw)


class RecordingOpenaiSdkPayloadTests(unittest.TestCase):
    def test_generate_records_full_chat_completion_request_and_response(self) -> None:
        captured: list[dict] = []

        class FakeCompletion:
            def __init__(self) -> None:
                self.msg = SimpleNamespace(
                    content="hi",
                    tool_calls=None,
                    model="gpt-test",
                    finish_reason="stop",
                )
                self.choices = [SimpleNamespace(message=self.msg)]

            def model_dump(self, *, mode: str = "json"):  # noqa: ARG002
                return {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "gpt-test",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                }

        fake_completion = FakeCompletion()

        class FakeCompletions:
            def __init__(self) -> None:
                self.last: dict | None = None

            def create(self, **kwargs):
                self.last = kwargs
                return fake_completion

        completions_api = FakeCompletions()
        fake_sync = SimpleNamespace(chat=SimpleNamespace(completions=completions_api))

        inner = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.1,
            max_tokens=64,
            timeout_seconds=30,
        )
        inner.sync_client = fake_sync

        adapter = RecordingModelAdapter(
            inner,
            provider="openai_compatible",
            provider_kind="openai_compatible",
            model="gpt-test",
            recorder=captured.append,
        )
        out = adapter.generate(ModelRequest(system_prompt="S", user_prompt="U"))
        self.assertEqual(out.text, "hi")
        self.assertEqual(len(captured), 1)
        row = captured[0]
        req = row["request_payload"]
        self.assertEqual(completions_api.last, req)
        self.assertEqual(req["model"], "gpt-test")
        self.assertEqual(req["temperature"], 0.1)
        self.assertEqual(req["max_tokens"], 64)
        self.assertEqual(
            req["messages"],
            [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
        )
        resp = row["response_payload"]
        self.assertEqual(resp["id"], "chatcmpl-fake")
        self.assertEqual(resp["object"], "chat.completion")
        self.assertIn("choices", resp)
        self.assertIn("usage", resp)
        self.assertNotIn("message", resp)


class RecordingModelAdapterTests(unittest.TestCase):
    def test_records_success_payload(self) -> None:
        captured: list[dict] = []
        inner = _StubInner()
        adapter = RecordingModelAdapter(
            inner,
            provider="anthropic",
            provider_kind="anthropic",
            model="claude-test",
            recorder=captured.append,
        )
        out = adapter.generate(ModelRequest(system_prompt="s", user_prompt="u"))
        self.assertEqual(out.text, "{}")
        self.assertEqual(len(captured), 1)
        row = captured[0]
        self.assertTrue(row["ok"])
        self.assertEqual(row["model_id"], "anthropic-model")
        self.assertEqual(row["model"], "claude-test")
        self.assertEqual(row["first_token_latency_ms"], 42)
        self.assertEqual(row["input_tokens"], 10)
        self.assertEqual(row["output_tokens"], 5)
        self.assertEqual(row["total_tokens"], 15)
        msgs = row["request_payload"]["messages"]
        self.assertEqual(msgs[0], {"role": "system", "content": "s"})
        self.assertEqual(msgs[1], {"role": "user", "content": "u"})
        self.assertIsNone(row["request_payload"]["tools"])
        rp = row["response_payload"]
        self.assertIn("message", rp)
        self.assertNotIn("text", rp)
        self.assertEqual(rp["message"]["content"], "{}")
        self.assertIsNone(row.get("trace_id"))
        self.assertIsNotNone(row.get("span_id"))  # span_id comes from OTel span

    def test_observability_disabled_suppresses_recording(self) -> None:
        # Fast (non-debug) backtest mode: recording is short-circuited but the
        # underlying model call still runs and returns normally.
        from doyoutrade.debug.context import observability_disabled

        captured: list[dict] = []
        inner = _StubInner()
        adapter = RecordingModelAdapter(
            inner,
            provider="anthropic",
            provider_kind="anthropic",
            model="claude-test",
            recorder=captured.append,
        )
        with observability_disabled():
            out = adapter.generate(ModelRequest(system_prompt="s", user_prompt="u"))
        self.assertEqual(out.text, "{}")
        self.assertEqual(captured, [])
        # Outside the scope recording resumes.
        out2 = adapter.generate(ModelRequest(system_prompt="s", user_prompt="u"))
        self.assertEqual(out2.text, "{}")
        self.assertEqual(len(captured), 1)

    def test_records_provider_key_from_adapter_when_scope_omits_it(self) -> None:
        captured: list[dict] = []
        inner = _StubInner()
        adapter = RecordingModelAdapter(
            inner,
            provider="anthropic-main",
            provider_kind="anthropic",
            model="claude-test",
            recorder=captured.append,
        )

        adapter.generate(ModelRequest(system_prompt="s", user_prompt="u"))

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["provider_key"], "anthropic-main")

    def test_signal_scope_does_not_merge_strategy_extras_into_request_payload(self) -> None:
        captured: list[dict] = []
        inner = _StubInner()
        adapter = RecordingModelAdapter(
            inner,
            provider="anthropic",
            provider_kind="anthropic",
            model="claude-test",
            recorder=captured.append,
        )
        cycle = SimpleNamespace(task_id="inst-x", run_id="run-y", trace_id=None, span_id=None)
        with model_invocation_scope(
            cycle,
            "signal",
            extras={"signal_tool_names": ["data_bars_relative"], "react_max_turns": 3},
        ):
            adapter.generate(ModelRequest(system_prompt="s", user_prompt="u"))
        self.assertEqual(len(captured), 1)
        req = captured[0]["request_payload"]
        self.assertIsNone(req.get("tools"))
        self.assertNotIn("signal_tool_names", req)
        self.assertNotIn("react_max_turns", req)
        self.assertIsNotNone(captured[0]["span_id"])  # span_id from OTel span

    def test_records_trace_id_from_cycle_state(self) -> None:
        captured: list[dict] = []
        inner = _StubInner()
        adapter = RecordingModelAdapter(
            inner,
            provider="anthropic",
            provider_kind="anthropic",
            model="claude-test",
            recorder=captured.append,
        )
        cycle = SimpleNamespace(task_id="inst-x", run_id="run-y", trace_id="a" * 32, span_id=None)
        with model_invocation_scope(cycle, "signal"):
            adapter.generate(ModelRequest(system_prompt="s", user_prompt="u"))
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["trace_id"], "a" * 32)
        self.assertIsNotNone(captured[0]["span_id"])  # span_id from OTel span

    def test_compaction_model_invocation_uses_assistant_compaction_call_kind(self) -> None:
        captured: list[dict] = []
        inner = _StubChatSuccess()
        adapter = RecordingModelAdapter(
            inner,
            provider="openai_compatible",
            provider_kind="openai_compatible",
            model="gpt-test",
            recorder=captured.append,
        )
        cycle = SimpleNamespace(task_id=None, run_id="asst-run-1", trace_id="b" * 32, span_id=None)

        async def call() -> None:
            with model_invocation_scope(
                cycle,
                "assistant_compaction",
                extras={
                    "assistant_session_id": "asst-session-1",
                    "model_route_name": "summary-route",
                },
            ):
                await adapter.agent_turn([HumanMessage(content="summarize this")], tools=None)

        asyncio.run(call())

        self.assertEqual(len(captured), 1)
        row = captured[0]
        self.assertEqual(row["call_kind"], "assistant_compaction")
        self.assertEqual(row["run_id"], "asst-run-1")
        self.assertEqual(row["trace_id"], "b" * 32)
        self.assertEqual(row["model_route_name"], "summary-route")

    def test_records_error_before_rethrow(self) -> None:
        captured: list[dict] = []
        inner = _StubInner(fail=True)
        adapter = RecordingModelAdapter(
            inner,
            provider="openai_compatible",
            provider_kind="openai_compatible",
            model="gpt-test",
            recorder=captured.append,
        )
        with self.assertRaises(RuntimeError):
            adapter.generate(ModelRequest(system_prompt="s", user_prompt="u"))
        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0]["ok"])
        self.assertIn("boom", captured[0]["error_message"])
        self.assertIsNotNone(captured[0]["span_id"])  # span_id from OTel span even on error
        rp = captured[0].get("response_payload")
        self.assertIsInstance(rp, dict)
        err = rp.get("error") if isinstance(rp, dict) else None
        self.assertIsInstance(err, dict)
        assert err is not None
        self.assertEqual(err.get("code"), "model_invocation_failed")
        self.assertEqual(err.get("type"), "RuntimeError")

    def test_records_error_includes_http_body_from_wrapped_exception(self) -> None:
        import httpx

        captured: list[dict] = []
        req = httpx.Request("POST", "http://127.0.0.1:1234/v1/x")
        resp = httpx.Response(422, content=b'{"reason":"schema"}', request=req)
        http_exc = httpx.HTTPStatusError("bad", request=req, response=resp)

        class _RaisesWrapped(ModelAdapter):
            def generate(self, request: ModelRequest) -> ModelResponse:
                raise RuntimeError("outer") from http_exc

        adapter = RecordingModelAdapter(
            _RaisesWrapped(),
            provider="lmstudio",
            provider_kind="lmstudio",
            model="m",
            recorder=captured.append,
        )
        with self.assertRaises(RuntimeError):
            adapter.generate(ModelRequest(system_prompt="s", user_prompt="u"))
        self.assertEqual(len(captured), 1)
        rp = captured[0]["response_payload"]
        self.assertIsInstance(rp, dict)
        err = rp["error"]
        self.assertEqual(err.get("http_status"), 422)
        self.assertIn("schema", str(err.get("body_preview") or ""))

    def test_records_chat_ainvoke_error_payload(self) -> None:
        import httpx

        captured: list[dict] = []
        req = httpx.Request("POST", "http://127.0.0.1:1234/v1/x")
        resp = httpx.Response(503, content=b"upstream", request=req)
        http_exc = httpx.HTTPStatusError("x", request=req, response=resp)
        try:
            raise RuntimeError("tick failed") from http_exc
        except RuntimeError as wrapped_exc:
            inner = _StubChatFail(wrapped_exc)
        adapter = RecordingModelAdapter(
            inner,
            provider="lmstudio",
            provider_kind="lmstudio",
            model="m",
            recorder=captured.append,
        )

        async def call() -> None:
            await adapter.chat_ainvoke([], tools=None)

        with self.assertRaises(RuntimeError):
            asyncio.run(call())
        self.assertEqual(len(captured), 1)
        err = captured[0]["response_payload"]["error"]
        self.assertEqual(err.get("http_status"), 503)
        self.assertIn("upstream", str(err.get("body_preview") or ""))

    def test_agent_turn_records_and_returns_normalized_response(self) -> None:
        captured: list[dict] = []
        adapter = RecordingModelAdapter(
            _StubChatSuccess(),
            provider="openai_compatible",
            provider_kind="openai_compatible",
            model="gpt-test",
            recorder=captured.append,
        )

        async def call() -> AgentTurnResponse:
            return await adapter.agent_turn([], tools=None)

        turn = asyncio.run(call())

        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0]["ok"])
        self.assertEqual(captured[0]["input_tokens"], 7)
        self.assertEqual(len(turn.tool_calls), 1)
        self.assertEqual(turn.tool_calls[0].name, "data_bars_relative")
        self.assertEqual(turn.tool_calls[0].arguments, {"symbol": "600000.SH"})

    def test_agent_turn_emits_default_text_delta(self) -> None:
        captured: list[dict] = []
        deltas: list[str] = []
        adapter = RecordingModelAdapter(
            _StubChatText(),
            provider="openai_compatible",
            provider_kind="openai_compatible",
            model="gpt-test",
            recorder=captured.append,
        )

        async def call() -> AgentTurnResponse:
            async def on_delta(delta: str) -> None:
                deltas.append(delta)

            return await adapter.agent_turn([], tools=None, on_text_delta=on_delta)

        turn = asyncio.run(call())

        self.assertEqual(turn.content, "hello")
        self.assertEqual(deltas, ["hello"])
        self.assertEqual(len(captured), 1)

    def test_agent_turn_delegates_native_streaming_deltas_to_inner(self) -> None:
        captured: list[dict] = []
        deltas: list[str] = []
        adapter = RecordingModelAdapter(
            _StubAgentTurnStreaming(),
            provider="openai_compatible",
            provider_kind="openai_compatible",
            model="gpt-test",
            recorder=captured.append,
        )

        async def call() -> AgentTurnResponse:
            async def on_delta(delta: str) -> None:
                deltas.append(delta)

            return await adapter.agent_turn([], tools=None, on_text_delta=on_delta)

        turn = asyncio.run(call())

        self.assertEqual(turn.content, "hello")
        self.assertEqual(deltas, ["he", "llo"])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["response_payload"], {"streamed": True})
        self.assertIsInstance(captured[0]["first_token_latency_ms"], int)
        self.assertEqual(captured[0]["output_tokens"], 2)

    def test_records_dataclass_tool_calls(self) -> None:
        """PseudoToolCall dataclass (production SDK path) must serialize correctly in recording."""
        captured: list[dict] = []
        inner = _StubWithPseudoToolCall()
        adapter = RecordingModelAdapter(
            inner,
            provider="anthropic",
            provider_kind="anthropic",
            model="claude-test",
            recorder=captured.append,
        )
        out = adapter.generate(ModelRequest(system_prompt="s", user_prompt="u"))
        self.assertEqual(out.text, "{}")
        self.assertEqual(len(captured), 1)
        row = captured[0]
        self.assertTrue(row["ok"])
        # Verify PseudoToolCall dataclass was serialized correctly
        rp = row["response_payload"]
        tc = rp["message"]["tool_calls"][0]
        self.assertEqual(tc["name"], "data_bars_relative")
        self.assertEqual(tc["args"], '{"symbol": "600000.SH"}')
        self.assertEqual(tc["id"], "call_1")


class PseudoAIMessageFromOpenAITests(unittest.TestCase):
    def test_list_content_is_flattened_for_json_fallback(self) -> None:
        msg = SimpleNamespace(
            content=[{"type": "text", "text": '{"proposals": []}'}],
            tool_calls=None,
            model="x",
            finish_reason="stop",
        )
        raw = PseudoAIMessage.from_openai(msg)
        self.assertEqual(raw.content, '{"proposals": []}')

    def test_openai_serialized_request_includes_tool_choice(self) -> None:
        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.2,
            max_tokens=128,
            timeout_seconds=30,
            tool_choice="required",
        )
        tool_def = [{"type": "function", "function": {"name": "foo", "parameters": {}}}]
        body = serialized_model_invocation_request(
            adapter,
            ModelRequest(system_prompt="S", user_prompt="U", tools=tool_def),
        )
        self.assertEqual(body["tool_choice"], "required")


class OpenAICompatibleStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_turn_streams_openai_chat_completion_chunks(self) -> None:
        calls: list[dict[str, Any]] = []

        class _Stream:
            async def __aiter__(self):
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="he", tool_calls=None))],
                    usage=None,
                )
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="llo", tool_calls=None))],
                    usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5),
                )

        class _Completions:
            async def create(self, **kwargs):
                calls.append(kwargs)
                return _Stream()

        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.1,
            max_tokens=64,
            timeout_seconds=30,
        )
        adapter.async_client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        deltas: list[str] = []

        async def on_delta(delta: str) -> None:
            deltas.append(delta)

        turn = await adapter.agent_turn(
            [HumanMessage(content="hi")],
            tools=None,
            on_text_delta=on_delta,
        )

        self.assertEqual(deltas, ["he", "llo"])
        self.assertEqual(turn.content, "hello")
        self.assertEqual(turn.raw.content, "hello")
        self.assertEqual(turn.usage, {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})
        self.assertTrue(calls[0]["stream"])

    async def test_agent_turn_splits_inline_think_tags_from_content(self) -> None:
        """MiniMax-style providers inline <think>...</think> into ``content``
        instead of a dedicated reasoning_content delta. It must be routed to
        on_thinking_delta and kept out of the visible text / final content."""

        class _Stream:
            async def __aiter__(self):
                # Tag boundary deliberately split across chunks.
                for piece in ("Hello <th", "ink>steps here</think> world"):
                    yield SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=piece, tool_calls=None))],
                        usage=None,
                    )

        class _Completions:
            async def create(self, **kwargs):
                return _Stream()

        adapter = OpenAICompatibleAdapter(
            model="minimax-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.1,
            max_tokens=64,
            timeout_seconds=30,
        )
        adapter.async_client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        text_deltas: list[str] = []
        thinking_deltas: list[str] = []

        async def on_text(delta: str) -> None:
            text_deltas.append(delta)

        async def on_thinking(delta: str) -> None:
            thinking_deltas.append(delta)

        turn = await adapter.agent_turn(
            [HumanMessage(content="hi")],
            tools=None,
            on_text_delta=on_text,
            on_thinking_delta=on_thinking,
        )

        self.assertEqual("".join(text_deltas), "Hello  world")
        self.assertEqual("".join(thinking_deltas), "steps here")
        self.assertEqual(turn.content, "Hello  world")
        self.assertNotIn("<think>", turn.content)


class AnthropicStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_turn_streams_anthropic_message_events(self) -> None:
        calls: list[dict[str, Any]] = []

        class _Stream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def __aiter__(self):
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="你"),
                )
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="好"),
                )

            async def get_final_message(self):
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="你好")],
                    usage=SimpleNamespace(
                        input_tokens=4,
                        output_tokens=2,
                        model_dump=lambda mode="json": {  # noqa: ARG005
                            "input_tokens": 4,
                            "output_tokens": 2,
                        },
                    ),
                    stop_reason="end_turn",
                    stats=None,
                )

        class _Messages:
            def stream(self, **kwargs):
                calls.append(kwargs)
                return _Stream()

        adapter = AnthropicAdapter(
            model="claude-test",
            api_key="k",
            temperature=0.1,
            max_tokens=64,
            timeout_seconds=30,
        )
        adapter.async_client = SimpleNamespace(messages=_Messages())
        deltas: list[str] = []

        async def on_delta(delta: str) -> None:
            deltas.append(delta)

        turn = await adapter.agent_turn(
            [HumanMessage(content="hi")],
            tools=None,
            on_text_delta=on_delta,
        )

        self.assertEqual(deltas, ["你", "好"])
        self.assertEqual(turn.content, "你好")
        self.assertEqual(turn.usage["input_tokens"], 4)
        self.assertEqual(turn.usage["output_tokens"], 2)
        self.assertEqual(calls[0]["model"], "claude-test")


class ModelInvocationRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "mi.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}",
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_add_and_list_ordering(self) -> None:
        repo = SqlAlchemyModelInvocationRepository(self.session_factory)
        base = {
            "model_id": "anthropic-model",
            "provider_kind": "anthropic",
            "model": "m1",
            "task_id": "i1",
            "run_id": "r1",
            "trace_id": "t" * 32,
            "span_id": "span1234567890ab",
            "call_kind": "signal",
            "first_token_latency_ms": None,
            "total_latency_ms": 100,
            "input_tokens": 1,
            "output_tokens": 2,
            "total_tokens": 3,
            "ok": True,
            "error_message": "",
            "request_payload": {
                "model": "m1",
                "messages": [
                    {"role": "system", "content": "a"},
                    {"role": "user", "content": "b"},
                ],
            },
            "response_payload": {"message": {}},
        }
        await repo.add_invocation(base)
        second = dict(base)
        second["request_payload"] = {
            "model": "m1",
            "messages": [
                {"role": "system", "content": "c"},
                {"role": "user", "content": "d"},
            ],
        }
        await repo.add_invocation(second)

        items, total = await repo.list_invocations(limit=10, offset=0)
        self.assertEqual(total, 2)
        self.assertGreater(items[0]["id"], items[1]["id"])
        self.assertEqual(items[0]["request"]["messages"][0]["content"], "c")
        self.assertEqual(items[1]["request"]["messages"][0]["content"], "a")
        self.assertEqual(items[0]["trace_id"], "t" * 32)
        self.assertEqual(items[1]["trace_id"], "t" * 32)

    async def test_list_invocations_filters_by_trace_id_and_span_id(self) -> None:
        repo = SqlAlchemyModelInvocationRepository(self.session_factory)
        a = {
            "model_id": "anthropic-model",
            "provider_kind": "anthropic",
            "model": "m1",
            "task_id": "i1",
            "run_id": "r1",
            "trace_id": "aa" + "b" * 30,
            "span_id": "span-one-unique",
            "call_kind": "signal",
            "first_token_latency_ms": None,
            "total_latency_ms": 1,
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
            "ok": True,
            "error_message": "",
            "request_payload": {"model": "m1", "messages": []},
            "response_payload": None,
        }
        b = dict(a)
        b["trace_id"] = "cc" + "d" * 30
        b["span_id"] = "span-two-other"
        await repo.add_invocation(a)
        await repo.add_invocation(b)

        trace_a = "aa" + "b" * 30
        trace_b = "cc" + "d" * 30
        by_trace, n1 = await repo.list_invocations(limit=10, offset=0, trace_id=trace_a)
        self.assertEqual(n1, 1)
        self.assertEqual(by_trace[0]["span_id"], "span-one-unique")

        by_span, n2 = await repo.list_invocations(limit=10, offset=0, span_id="span-two-other")
        self.assertEqual(n2, 1)
        self.assertEqual(by_span[0]["trace_id"], trace_b)

        both, n3 = await repo.list_invocations(
            limit=10, offset=0, trace_id=trace_b, span_id="span-two-other"
        )
        self.assertEqual(n3, 1)

        no_partial, n4 = await repo.list_invocations(limit=10, offset=0, trace_id="aab")
        self.assertEqual(n4, 0)

    async def test_get_invocation_by_span_id(self) -> None:
        repo = SqlAlchemyModelInvocationRepository(self.session_factory)
        payload = {
            "model_id": "anthropic-model",
            "provider_kind": "anthropic",
            "model": "m1",
            "task_id": "i1",
            "run_id": "r1",
            "trace_id": "t" * 32,
            "span_id": "span1234567890ab",
            "call_kind": "signal",
            "first_token_latency_ms": None,
            "total_latency_ms": 100,
            "input_tokens": 1,
            "output_tokens": 2,
            "total_tokens": 3,
            "ok": True,
            "error_message": "",
            "request_payload": {"model": "m1", "messages": []},
            "response_payload": None,
        }
        await repo.add_invocation(payload)
        result = await repo.get_invocation_by_span_id("span1234567890ab")
        assert result is not None
        assert result["span_id"] == "span1234567890ab"
        assert result["model"] == "m1"

        missing = await repo.get_invocation_by_span_id("nonexistent")
        assert missing is None

    async def test_get_invocation_by_span_id_hex_matches_legacy_decimal_storage(self) -> None:
        """Debug UI uses 16-char hex; older rows may store str(int(span_id)) from OTel."""
        repo = SqlAlchemyModelInvocationRepository(self.session_factory)
        hex_id = "fb03571413b1c2c3"
        as_int = int(hex_id, 16)
        async with self.session_factory() as session:
            session.add(
                ModelInvocationRecord(
                    model_id="anthropic-model",
                    provider_kind="anthropic",
                    model="m1",
                    task_id="i1",
                    run_id="r1",
                    trace_id="t" * 32,
                    span_id=str(as_int),
                    call_kind="signal",
                    first_token_latency_ms=None,
                    total_latency_ms=1,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    ok=True,
                    error_message="",
                    request_payload={"model": "m1", "messages": []},
                    response_payload=None,
                ),
            )
            await session.commit()

        result = await repo.get_invocation_by_span_id(hex_id)
        assert result is not None
        assert result["model"] == "m1"
        assert result["span_id"] == str(as_int)

    async def test_add_invocation_normalizes_int_span_id_to_hex(self) -> None:
        repo = SqlAlchemyModelInvocationRepository(self.session_factory)
        sid = int("fb03571413b1c2c3", 16)
        payload = {
            "model_id": "anthropic-model",
            "provider_kind": "anthropic",
            "model": "m1",
            "task_id": "i1",
            "run_id": "r1",
            "trace_id": "t" * 32,
            "span_id": sid,
            "call_kind": "signal",
            "first_token_latency_ms": None,
            "total_latency_ms": 100,
            "input_tokens": 1,
            "output_tokens": 2,
            "total_tokens": 3,
            "ok": True,
            "error_message": "",
            "request_payload": {"model": "m1", "messages": []},
            "response_payload": None,
        }
        await repo.add_invocation(payload)
        found = await repo.get_invocation_by_span_id("fb03571413b1c2c3")
        assert found is not None
        assert found["span_id"] == format(sid, "016x")


class OpenAICompatibleMalformedChoicesTests(unittest.TestCase):
    """Some OpenAI-compatible gateways return ``choices=None``; avoid opaque TypeErrors."""

    def test_first_chat_completion_message_rejects_none_choices(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _first_chat_completion_message(SimpleNamespace(choices=None))
        self.assertIn("choices=None", str(ctx.exception))

    def test_first_chat_completion_message_rejects_empty_choices(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _first_chat_completion_message(SimpleNamespace(choices=[]))
        self.assertIn("empty choices", str(ctx.exception))

    def test_first_chat_completion_message_rejects_missing_message(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            _first_chat_completion_message(SimpleNamespace(choices=[SimpleNamespace(message=None)]))
        self.assertIn("no message field", str(ctx.exception))

    def test_generate_raises_clear_error_when_choices_is_none(self) -> None:
        class FakeCompletion:
            choices = None

        class FakeCompletions:
            def create(self, **kwargs):
                return FakeCompletion()

        adapter = OpenAICompatibleAdapter(
            model="gpt-test",
            api_key="k",
            base_url="http://localhost/v1",
            temperature=0.1,
            max_tokens=64,
            timeout_seconds=30,
        )
        adapter.sync_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        with self.assertRaises(RuntimeError) as ctx:
            adapter.generate(ModelRequest(system_prompt="S", user_prompt="U"))
        self.assertIn("choices=None", str(ctx.exception))

    def test_chat_ainvoke_raises_clear_error_when_choices_is_none(self) -> None:
        class FakeCompletion:
            choices = None

        class FakeAsyncCompletions:
            async def create(self, **kwargs):
                return FakeCompletion()

        async def run() -> None:
            adapter = OpenAICompatibleAdapter(
                model="gpt-test",
                api_key="k",
                base_url="http://localhost/v1",
                temperature=0.1,
                max_tokens=64,
                timeout_seconds=30,
            )
            adapter.async_client = SimpleNamespace(
                chat=SimpleNamespace(completions=FakeAsyncCompletions()),
            )
            await adapter.chat_ainvoke([HumanMessage(content="hi")])

        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(run())
        self.assertIn("choices=None", str(ctx.exception))
