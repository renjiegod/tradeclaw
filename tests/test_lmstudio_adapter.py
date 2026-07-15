"""Tests for :class:`doyoutrade.models.providers.lmstudio.LmStudioAdapter`."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from doyoutrade.models.providers._common import PseudoAIMessage
from doyoutrade.models.providers.lmstudio import (
    LmStudioAdapter,
    _build_prediction_config_dict,
    _langchain_messages_to_openai_dicts,
    _lmstudio_sdk_api_host,
    _normalize_lmstudio_base_url,
    _reraise_lmstudio_jinja_safe_hint,
    openai_function_tools_to_lmstudio_raw_tools_dict,
)


class NormalizeLmStudioBaseUrlTests(unittest.TestCase):
    def test_fixes_missing_colon_after_scheme(self) -> None:
        self.assertEqual(
            _normalize_lmstudio_base_url("http//localhost:1234"),
            "http://localhost:1234",
        )
        self.assertEqual(
            _normalize_lmstudio_base_url("HTTP//127.0.0.1:9999/v1"),
            "http://127.0.0.1:9999/v1",
        )
        self.assertEqual(
            _normalize_lmstudio_base_url("https//example.com"),
            "https://example.com",
        )

    def test_leaves_well_formed_urls(self) -> None:
        self.assertEqual(
            _normalize_lmstudio_base_url("http://localhost:1234"),
            "http://localhost:1234",
        )
        self.assertIsNone(_normalize_lmstudio_base_url(None))
        self.assertIsNone(_normalize_lmstudio_base_url("   "))

    def test_adapter_applies_normalization(self) -> None:
        a = LmStudioAdapter("m", base_url="http//127.0.0.1:1", temperature=0.0, max_tokens=10, timeout_seconds=30.0)
        self.assertEqual(a.api_host, "127.0.0.1:1")

    def test_full_http_url_stripped_to_host_port_for_sdk(self) -> None:
        self.assertEqual(_lmstudio_sdk_api_host("http://localhost:1234"), "localhost:1234")
        self.assertEqual(_lmstudio_sdk_api_host("https://127.0.0.1:9999"), "127.0.0.1:9999")
        self.assertEqual(_lmstudio_sdk_api_host("localhost:1234"), "localhost:1234")

    def test_adapter_strips_scheme_from_config_base_url(self) -> None:
        a = LmStudioAdapter(
            "m",
            base_url="http://localhost:1234",
            temperature=0.0,
            max_tokens=10,
            timeout_seconds=30.0,
        )
        self.assertEqual(a.api_host, "localhost:1234")


class OpenAiToolsToLmStudioDictTests(unittest.TestCase):
    def test_tool_array_shape_matches_llm_tool_function_layout(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Weather lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                },
            }
        ]
        out = openai_function_tools_to_lmstudio_raw_tools_dict(tools)
        self.assertEqual(out["type"], "toolArray")
        self.assertEqual(len(out["tools"]), 1)
        t0 = out["tools"][0]
        self.assertEqual(t0["type"], "function")
        self.assertEqual(t0["function"]["name"], "get_weather")
        self.assertEqual(t0["function"]["description"], "Weather lookup")
        self.assertEqual(t0["function"]["parameters"]["type"], "object")

    def test_none_parameters_becomes_empty_object_schema(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {"name": "noop", "description": None, "parameters": None},
            }
        ]
        out = openai_function_tools_to_lmstudio_raw_tools_dict(tools)
        params = out["tools"][0]["function"]["parameters"]
        self.assertIsInstance(params, dict)
        self.assertEqual(params.get("type"), "object")


class PseudoAIMessageLmStudioTests(unittest.TestCase):
    def test_from_lmstudio_assistant_parses_tool_requests(self) -> None:
        from lmstudio._sdk_models import (
            AssistantResponse,
            TextData,
            ToolCallRequest,
            ToolCallRequestData,
        )

        tr = ToolCallRequest(
            type="function",
            name="submit_orders",
            id="call_abc",
            arguments={"orders": [{"symbol": "AAA"}]},
        )
        block = ToolCallRequestData(tool_call_request=tr)
        ar = AssistantResponse(content=[TextData(text="Ok"), block])
        raw = PseudoAIMessage.from_lmstudio_assistant(ar)
        self.assertIn("Ok", str(raw.content))
        self.assertIsNotNone(raw.tool_calls)
        assert raw.tool_calls is not None
        self.assertEqual(len(raw.tool_calls), 1)
        self.assertEqual(raw.tool_calls[0].name, "submit_orders")
        self.assertEqual(raw.tool_calls[0].id, "call_abc")
        self.assertIn("AAA", raw.tool_calls[0].args)


class LangchainToLmStudioChatMappingTests(unittest.TestCase):
    def test_skill_companion_flushed_as_extra_user_turn(self) -> None:
        class _Tool:
            type = "tool"
            content = "tool-out"
            tool_call_id = "c1"
            companion_user_text = "skill-body"

        class _Human:
            type = "human"
            content = "next"

        dicts = _langchain_messages_to_openai_dicts([_Tool(), _Human()])
        self.assertEqual(dicts[-2]["role"], "user")
        self.assertEqual(dicts[-2]["content"], "skill-body")
        self.assertEqual(dicts[-1]["role"], "user")
        self.assertEqual(dicts[-1]["content"], "next")


class BuildPredictionConfigDictTests(unittest.TestCase):
    def test_merges_prediction_config_extra(self) -> None:
        cfg = _build_prediction_config_dict(
            tools=None,
            temperature=0.2,
            max_tokens=128,
            prediction_config_extra={"promptTemplate": {"type": "manual", "stopStrings": []}},
        )
        self.assertEqual(cfg["temperature"], 0.2)
        self.assertEqual(cfg["maxTokens"], 128)
        self.assertEqual(cfg["promptTemplate"]["type"], "manual")

    def test_extra_deep_merges_with_raw_tools(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "t1",
                    "description": "d",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        cfg = _build_prediction_config_dict(
            tools=tools,
            temperature=None,
            max_tokens=None,
            prediction_config_extra={"raw": {"foo": 1}},
        )
        self.assertEqual(cfg["rawTools"]["type"], "toolArray")
        self.assertEqual(cfg["raw"]["foo"], 1)


class ReraiseLmstudioJinjaSafeHintTests(unittest.TestCase):
    def test_augment_lmstudio_server_error_with_safe_filter_message(self) -> None:
        from lmstudio.json_api import LMStudioServerError

        err = LMStudioServerError(
            'Chat response error: Error rendering prompt with jinja template: '
            '"Unknown StringValue filter: safe".'
        )
        with self.assertRaises(LMStudioServerError) as ctx:
            _reraise_lmstudio_jinja_safe_hint(err)
        self.assertIn("[doyoutrade]", str(ctx.exception))
        self.assertIn("Prompt Template", str(ctx.exception))

    def test_passes_through_unrelated_errors(self) -> None:
        err = ValueError("something else")
        with self.assertRaises(ValueError) as ctx:
            _reraise_lmstudio_jinja_safe_hint(err)
        self.assertEqual(str(ctx.exception), "something else")


class LmStudioAdapterChatAinvokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_ainvoke_returns_tool_calls_from_stream(self) -> None:
        from lmstudio._sdk_models import ToolCallRequest
        from lmstudio.json_api import PredictionToolCallEvent

        class _Pred:
            content = "done"
            stats = None

            def _to_history_content(self) -> str:
                return "done"

        class _Stream:
            def __init__(self) -> None:
                self._tr = ToolCallRequest(
                    type="function",
                    name="echo",
                    id="id-1",
                    arguments={"q": "x"},
                )

            async def _iter_events(self):
                yield PredictionToolCallEvent(self._tr)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            def result(self):
                return _Pred()

        fake_llm = MagicMock()
        fake_llm.respond_stream = AsyncMock(return_value=_Stream())

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.llm.model = AsyncMock(return_value=fake_llm)

        adapter = LmStudioAdapter("my-model")

        class _Sys:
            type = "system"
            content = "s"

        class _Human:
            type = "human"
            content = "h"

        with patch("lmstudio.AsyncClient", return_value=fake_client):
            out = await adapter.chat_ainvoke([_Sys(), _Human()], tools=[])

        self.assertEqual(out.text, "done")
        assert out.raw is not None
        assert out.raw.tool_calls is not None
        self.assertEqual(out.raw.tool_calls[0].name, "echo")
        self.assertEqual(out.raw.tool_calls[0].id, "id-1")
        fake_llm.respond_stream.assert_awaited()

    async def test_agent_turn_emits_text_deltas_from_stream(self) -> None:
        class _Pred:
            content = "hello"
            stats = None

            def _to_history_content(self) -> str:
                return "hello"

        class _Stream:
            async def _iter_events(self):
                yield type("TextEvent", (), {"content": "he"})()
                yield type("TextEvent", (), {"content": "llo"})()

            def result(self):
                return _Pred()

        fake_llm = MagicMock()
        fake_llm.respond_stream = AsyncMock(return_value=_Stream())

        fake_client = MagicMock()
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=None)
        fake_client.llm.model = AsyncMock(return_value=fake_llm)

        adapter = LmStudioAdapter("my-model")

        class _Human:
            type = "human"
            content = "h"

        deltas: list[str] = []

        async def on_delta(delta: str) -> None:
            deltas.append(delta)

        with patch("lmstudio.AsyncClient", return_value=fake_client):
            turn = await adapter.agent_turn([_Human()], tools=[], on_text_delta=on_delta)

        self.assertEqual(deltas, ["he", "llo"])
        self.assertEqual(turn.content, "hello")
        fake_llm.respond_stream.assert_awaited()

    def test_init_raises_when_lmstudio_missing(self) -> None:
        with patch(
            "doyoutrade.models.providers.lmstudio._require_lmstudio",
            side_effect=RuntimeError("lmstudio is not installed"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                LmStudioAdapter("x")
            self.assertIn("lmstudio is not installed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
