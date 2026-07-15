import unittest

from doyoutrade.agent_runtime import (
    AgentMessage,
    AgentToolCall,
    AgentTurnResponse,
    ToolSpec,
    agent_tool_specs_from_openai_tools,
    agent_turn_response_from_model_response,
    tool_specs_for_provider,
)
from doyoutrade.models.base import ModelResponse
from doyoutrade.models.providers._common import PseudoAIMessage, PseudoToolCall


class AgentRuntimeTypesTests(unittest.TestCase):
    def test_tool_spec_converts_to_openai_and_anthropic(self) -> None:
        spec = ToolSpec(
            name="data_bars_relative",
            description="Read bars.",
            parameters={
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        )

        openai_defs = tool_specs_for_provider("openai_compatible", [spec])
        anthropic_defs = tool_specs_for_provider("anthropic", [spec])

        self.assertEqual(
            openai_defs,
            [
                {
                    "type": "function",
                    "function": {
                        "name": "data_bars_relative",
                        "description": "Read bars.",
                        "parameters": {
                            "type": "object",
                            "properties": {"symbol": {"type": "string"}},
                            "required": ["symbol"],
                        },
                    },
                }
            ],
        )
        self.assertEqual(
            anthropic_defs,
            [
                {
                    "name": "data_bars_relative",
                    "description": "Read bars.",
                    "input_schema": {
                        "type": "object",
                        "properties": {"symbol": {"type": "string"}},
                        "required": ["symbol"],
                    },
                }
            ],
        )

    def test_agent_tool_specs_from_openai_tools_round_trip_names(self) -> None:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "submit_signal_proposals",
                    "description": "Submit.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        specs = agent_tool_specs_from_openai_tools(tools)

        self.assertEqual(
            specs,
            [
                ToolSpec(
                    name="submit_signal_proposals",
                    description="Submit.",
                    parameters={"type": "object", "properties": {}},
                )
            ],
        )

    def test_model_response_normalizes_tool_calls(self) -> None:
        raw = PseudoAIMessage(
            content="thinking",
            tool_calls=[
                PseudoToolCall(
                    name="data_bars_relative",
                    args='{"symbol": "600000.SH", "count": -30}',
                    id="call_1",
                )
            ],
            usage_metadata={
                "input_tokens": 10,
                "output_tokens": 5,
                "total_tokens": 15,
            },
        )
        model_response = ModelResponse(
            text="thinking",
            raw=raw,
            invocation_request_payload={"messages": []},
            invocation_response_payload={"id": "resp_1"},
        )

        turn = agent_turn_response_from_model_response(model_response)

        self.assertEqual(
            turn,
            AgentTurnResponse(
                content="thinking",
                tool_calls=[
                    AgentToolCall(
                        id="call_1",
                        name="data_bars_relative",
                        arguments={"symbol": "600000.SH", "count": -30},
                    )
                ],
                raw=raw,
                request_payload={"messages": []},
                response_payload={"id": "resp_1"},
                usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            ),
        )

    def test_agent_message_keeps_tool_metadata(self) -> None:
        message = AgentMessage(
            role="tool",
            content='{"ok": true}',
            tool_call_id="call_1",
            metadata={"tool": "data_bars_relative"},
        )

        self.assertEqual(message.role, "tool")
        self.assertEqual(message.tool_call_id, "call_1")
        self.assertEqual(message.metadata["tool"], "data_bars_relative")


if __name__ == "__main__":
    unittest.main()
