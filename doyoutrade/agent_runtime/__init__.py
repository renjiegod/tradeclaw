"""Provider-neutral agent runtime primitives.

This package intentionally stays below trading strategy code: strategies describe
messages and tools once, while provider codecs translate them to native wire
formats.
"""

from doyoutrade.agent_runtime.types import (
    AgentMessage,
    AgentToolCall,
    AgentTurnResponse,
    ToolSpec,
)
from doyoutrade.agent_runtime.codecs import (
    agent_tool_specs_from_openai_tools,
    agent_turn_response_from_model_response,
    tool_specs_for_provider,
)

__all__ = [
    "AgentMessage",
    "AgentToolCall",
    "AgentTurnResponse",
    "ToolSpec",
    "agent_tool_specs_from_openai_tools",
    "agent_turn_response_from_model_response",
    "tool_specs_for_provider",
]
