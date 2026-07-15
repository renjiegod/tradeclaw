"""Scripted model adapter for assistant-loop regression tests.

Drives ``AssistantService`` turns with a predetermined sequence of model
behaviours so loop mechanics (tool dispatch, error-repair convergence,
reminder injection, turn caps) can be asserted without a real model.

Usage::

    steps = [
        call_tool("dummy_tool", {"symbol": "600000.SH"}),
        say("done"),
    ]
    adapter = ScriptedModelAdapter(steps)
    service = AssistantService(
        InMemoryAssistantRepository(),
        model_adapter_factory=adapter.factory,
        tool_registry=OperationRegistry([...]),
    )
    ...
    adapter.assert_exhausted()

Each ``ScriptStep`` may carry an ``expect`` callback that receives the
exact ``(messages, tools)`` the service handed to the model for that
call — use it to assert on injected reminders or repaired tool args at
the precise turn they must appear.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from doyoutrade.agent_runtime import AgentToolCall, AgentTurnResponse


class ScriptExhaustedError(AssertionError):
    """The service asked the model for more turns than the script provides."""


class ScriptNotConsumedError(AssertionError):
    """The run ended while scripted steps were still pending."""


@dataclass(frozen=True)
class ScriptStep:
    """One model turn: optional thinking/text streaming plus the response."""

    content: str = ""
    tool_calls: tuple[AgentToolCall, ...] = ()
    thinking: str | None = None
    stream: bool = True
    expect: Callable[[list[Any], list[Any] | None], None] | None = None


def say(
    content: str,
    *,
    thinking: str | None = None,
    stream: bool = True,
    expect: Callable[[list[Any], list[Any] | None], None] | None = None,
) -> ScriptStep:
    """A turn where the model answers with plain text and no tool calls."""
    return ScriptStep(content=content, thinking=thinking, stream=stream, expect=expect)


def call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str | None = None,
    preface: str = "",
    thinking: str | None = None,
    expect: Callable[[list[Any], list[Any] | None], None] | None = None,
) -> ScriptStep:
    """A turn where the model requests one tool call (optionally with preface text)."""
    return ScriptStep(
        content=preface,
        tool_calls=(
            AgentToolCall(id=call_id or f"scripted_{name}", name=name, arguments=dict(arguments)),
        ),
        thinking=thinking,
        expect=expect,
    )


def call_tools(
    calls: Sequence[tuple[str, dict[str, Any]]],
    *,
    preface: str = "",
    expect: Callable[[list[Any], list[Any] | None], None] | None = None,
) -> ScriptStep:
    """A turn where the model requests several tool calls at once."""
    return ScriptStep(
        content=preface,
        tool_calls=tuple(
            AgentToolCall(id=f"scripted_{name}_{index}", name=name, arguments=dict(arguments))
            for index, (name, arguments) in enumerate(calls)
        ),
        expect=expect,
    )


@dataclass(frozen=True)
class RecordedModelCall:
    """Snapshot of what the service sent to the model on one call."""

    index: int
    messages: list[Any]
    tools: list[Any] | None

    def message_texts(self) -> list[str]:
        """Plain-text content of every message, tolerant of message shape."""
        texts: list[str] = []
        for message in self.messages:
            content = getattr(message, "content", None)
            if content is None and isinstance(message, dict):
                content = message.get("content")
            texts.append(content if isinstance(content, str) else str(content))
        return texts


class ScriptedModelAdapter:
    """``agent_turn`` adapter that replays a fixed script of model turns.

    Strict by default: a model call past the end of the script raises
    ``ScriptExhaustedError`` (which surfaces as an attempt failure), so a
    loop that takes more turns than the test author planned fails loudly
    instead of silently looping.
    """

    def __init__(self, steps: Sequence[ScriptStep], *, strict: bool = True) -> None:
        self._steps = list(steps)
        self._strict = strict
        self.calls: list[RecordedModelCall] = []

    async def factory(self, _route_name: str | None = None) -> "ScriptedModelAdapter":
        """Drop-in ``model_adapter_factory`` for ``AssistantService``."""
        return self

    async def agent_turn(
        self,
        messages: list[Any],
        *,
        tools: list[Any] | None = None,
        on_text_delta: Callable[[str], Any] | None = None,
        on_thinking_delta: Callable[[str], Any] | None = None,
    ) -> AgentTurnResponse:
        index = len(self.calls)
        self.calls.append(RecordedModelCall(index=index, messages=list(messages), tools=tools))
        if index >= len(self._steps):
            if self._strict:
                raise ScriptExhaustedError(
                    f"model called {index + 1} times but script has only "
                    f"{len(self._steps)} steps; last scripted step was "
                    f"{self._describe(self._steps[-1]) if self._steps else 'none'}"
                )
            return AgentTurnResponse(content="", tool_calls=[], raw=None)

        step = self._steps[index]
        if step.expect is not None:
            step.expect(list(messages), tools)
        if step.thinking is not None and on_thinking_delta is not None:
            await on_thinking_delta(step.thinking)
        if step.stream and step.content and on_text_delta is not None:
            await on_text_delta(step.content)
        return AgentTurnResponse(
            content=step.content,
            tool_calls=list(step.tool_calls),
            raw=SimpleNamespace(tool_calls=None, content=step.content),
        )

    def assert_exhausted(self) -> None:
        """Fail unless every scripted step was consumed."""
        if len(self.calls) < len(self._steps):
            pending = ", ".join(self._describe(step) for step in self._steps[len(self.calls):])
            raise ScriptNotConsumedError(
                f"script has {len(self._steps) - len(self.calls)} unconsumed steps: {pending}"
            )

    def tool_call_sequence(self) -> list[str]:
        """Names of every tool call the script issued, in order."""
        return [
            tool_call.name
            for step in self._steps[: len(self.calls)]
            for tool_call in step.tool_calls
        ]

    @staticmethod
    def _describe(step: ScriptStep) -> str:
        if step.tool_calls:
            return "call(" + ", ".join(tc.name for tc in step.tool_calls) + ")"
        return f"say({step.content[:30]!r})"
