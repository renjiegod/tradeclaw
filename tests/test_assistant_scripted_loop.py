"""Assistant-loop regression tests driven by the scripted model adapter.

These tests pin loop *mechanics* — tool dispatch order, error-repair
convergence, reminder injection, the max-turns cap — independent of any
real model, so behaviour regressions in ``AssistantService._run_loop``
fail here before they reach e2e or live validation.
"""

import unittest
from typing import Any

from doyoutrade.assistant import AssistantService, InMemoryAssistantRepository
from doyoutrade.tools import OperationHandler, OperationRegistry, ToolResult
from tests.scripted_model import (
    ScriptExhaustedError,
    ScriptNotConsumedError,
    ScriptedModelAdapter,
    call_tool,
    say,
)


class _EchoTool(OperationHandler):
    name = "echo_tool"
    description = "Echo test tool"
    category = "agent"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def __init__(self) -> None:
        self.executed_with: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            return ToolResult(
                text=f"[error:unknown_arguments] {contract.error}",
                is_error=True,
            )
        kwargs = contract.kwargs
        self.executed_with.append(dict(kwargs))
        return f'{{"status":"ok","echo":"{kwargs["text"]}"}}'


class ScriptedLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._services: list[AssistantService] = []

    async def asyncTearDown(self) -> None:
        for service in self._services:
            await service.aclose()

    def _build_service(
        self,
        adapter: ScriptedModelAdapter,
        *,
        tools: list[OperationHandler] | None = None,
        max_turns: int = 6,
    ) -> AssistantService:
        service = AssistantService(
            InMemoryAssistantRepository(),
            model_adapter_factory=adapter.factory,
            tool_registry=OperationRegistry(list(tools or [])),
            max_turns=max_turns,
        )
        self._services.append(service)
        return service

    async def test_tool_dispatch_then_final_answer(self):
        tool = _EchoTool()
        adapter = ScriptedModelAdapter(
            [
                call_tool("echo_tool", {"text": "ping"}),
                say("pong delivered"),
            ]
        )
        service = self._build_service(adapter, tools=[tool])
        session = await service.create_session(agent_id="test-agent", title="scripted")

        result = await service.send_message(
            session_id=session["session_id"], content="echo ping"
        )

        adapter.assert_exhausted()
        self.assertEqual(tool.executed_with, [{"text": "ping"}])
        self.assertEqual(result["messages"][-1]["content"], "pong delivered")
        # The second model call must have seen the tool result in context.
        second_call_text = "\n".join(adapter.calls[1].message_texts())
        self.assertIn('"echo":"ping"', second_call_text)

    async def test_unknown_arguments_error_reaches_model_and_repair_converges(self):
        """A typo'd tool arg must surface as a structured error in the next
        model call, and a corrected retry must succeed — pinning the
        unknown_arguments repair loop end to end."""
        tool = _EchoTool()

        def _expect_error_visible(messages: list[Any], _tools: list[Any] | None) -> None:
            joined = "\n".join(
                str(getattr(message, "content", "")) for message in messages
            )
            assert "unknown_arguments" in joined, (
                "repair turn did not see the unknown_arguments tool error"
            )

        adapter = ScriptedModelAdapter(
            [
                call_tool("echo_tool", {"txt": "ping"}),  # typo: txt
                call_tool(
                    "echo_tool", {"text": "ping"}, expect=_expect_error_visible
                ),
                say("repaired"),
            ]
        )
        service = self._build_service(adapter, tools=[tool])
        session = await service.create_session(agent_id="test-agent", title="repair")

        result = await service.send_message(
            session_id=session["session_id"], content="echo ping"
        )

        adapter.assert_exhausted()
        self.assertEqual(tool.executed_with, [{"text": "ping"}])
        self.assertEqual(result["messages"][-1]["content"], "repaired")

    async def test_runtime_context_reminder_injected_before_model_call(self):
        seen: dict[str, bool] = {"reminder": False}

        def _expect_reminder(messages: list[Any], _tools: list[Any] | None) -> None:
            joined = "\n".join(
                str(getattr(message, "content", "")) for message in messages
            )
            seen["reminder"] = "currentDate" in joined and "system-reminder" in joined

        adapter = ScriptedModelAdapter([say("ok", expect=_expect_reminder)])
        service = self._build_service(adapter)
        session = await service.create_session(agent_id="test-agent", title="reminder")

        await service.send_message(session_id=session["session_id"], content="hi")

        adapter.assert_exhausted()
        self.assertTrue(
            seen["reminder"],
            "runtime context <system-reminder> with currentDate was not injected",
        )

    async def test_max_turns_cap_returns_visible_notice(self):
        tool = _EchoTool()
        adapter = ScriptedModelAdapter(
            [
                call_tool("echo_tool", {"text": "one"}),
                call_tool("echo_tool", {"text": "two"}),
                call_tool("echo_tool", {"text": "three"}),
            ]
        )
        service = self._build_service(adapter, tools=[tool], max_turns=2)
        session = await service.create_session(agent_id="test-agent", title="cap")

        result = await service.send_message(
            session_id=session["session_id"], content="loop forever"
        )

        # Only max_turns model calls happen; the cap is reported, not silent.
        self.assertEqual(len(adapter.calls), 2)
        self.assertIn("上限", result["messages"][-1]["content"])

    async def test_script_exhaustion_fails_loudly(self):
        """If the loop asks for more model turns than scripted, the harness
        must raise instead of silently feeding empty turns."""
        tool = _EchoTool()
        adapter = ScriptedModelAdapter([call_tool("echo_tool", {"text": "ping"})])
        service = self._build_service(adapter, tools=[tool])
        session = await service.create_session(agent_id="test-agent", title="exhaust")

        with self.assertRaises(Exception) as ctx:
            await service.send_message(
                session_id=session["session_id"], content="echo ping"
            )
        self.assertIn("script has only", str(ctx.exception))

    async def test_assert_exhausted_flags_unconsumed_steps(self):
        adapter = ScriptedModelAdapter([say("first"), say("never reached")])
        service = self._build_service(adapter)
        session = await service.create_session(agent_id="test-agent", title="left")

        await service.send_message(session_id=session["session_id"], content="hi")

        with self.assertRaises(ScriptNotConsumedError):
            adapter.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
