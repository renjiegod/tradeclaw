from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


AgentRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class AgentToolCall:
    """Provider-neutral function/tool call requested by a model turn."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class AgentMessage:
    """Provider-neutral chat message used by agent loops."""

    role: AgentRole
    content: str
    tool_call_id: str | None = None
    tool_calls: list[AgentToolCall] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    """Provider-neutral tool schema in JSON Schema form."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class AgentTurnResponse:
    """Provider-neutral model turn result."""

    content: str
    tool_calls: list[AgentToolCall]
    raw: Any = None
    request_payload: dict[str, Any] | None = None
    response_payload: dict[str, Any] | None = None
    usage: dict[str, int | None] = field(default_factory=dict)
