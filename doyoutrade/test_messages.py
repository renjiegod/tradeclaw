"""Minimal message types for tests — replaces langchain_core.messages when langchain is not installed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SystemMessage:
    content: str | list
    type: str = "system"


@dataclass
class HumanMessage:
    content: str | list
    type: str = "human"


@dataclass
class AIMessage:
    content: str | list = ""
    tool_calls: list[Any] | None = None
    type: str = "ai"


@dataclass
class ToolMessage:
    content: str | list
    tool_call_id: str
    type: str = "tool"
    companion_user_text: str | None = None
    name: str | None = None


# ---------------------------------------------------------------------------
# PseudoToolCall — mimics the dataclass-style tool_calls from official SDKs
# Used in tests to exercise the same code paths as production PseudoToolCall
# ---------------------------------------------------------------------------


@dataclass
class PseudoToolCall:
    """Dataclass-style tool call — mimics doyoutrade.models.providers._common.PseudoToolCall."""
    name: str
    args: str  # JSON string
    id: str | None = None
    type: str = "tool_call"
