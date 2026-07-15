import unittest
from unittest.mock import AsyncMock, MagicMock

from doyoutrade.agent_runtime import AgentToolCall, AgentTurnResponse
from doyoutrade.assistant.service import AssistantService
from doyoutrade.assistant.repository import InMemoryAssistantRepository, InMemoryAgentRepository
from doyoutrade.tools import OperationHandler, OperationRegistry


class AssistantServiceAgentBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_session_requires_agent_id(self):
        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent({
            "name": "Test Agent",
            "system_prompt": "You are a test agent.",
        })
        service = AssistantService(
            InMemoryAssistantRepository(),
            agent_repository=agent_repo,
        )
        session = await service.create_session(agent_id=agent["id"], title="Test")
        self.assertEqual(session["agent_id"], agent["id"])
        self.assertEqual(session["title"], "Test")

    async def test_create_session_fails_for_missing_agent(self):
        service = AssistantService(
            InMemoryAssistantRepository(),
            agent_repository=InMemoryAgentRepository(),
        )
        with self.assertRaises(ValueError) as ctx:
            await service.create_session(agent_id="nonexistent", title="Test")
        self.assertIn("Agent not found", str(ctx.exception))

    async def test_create_session_fails_for_inactive_agent(self):
        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent({
            "name": "Inactive Agent",
            "system_prompt": "You are inactive.",
            "status": "inactive",
        })
        service = AssistantService(
            InMemoryAssistantRepository(),
            agent_repository=agent_repo,
        )
        with self.assertRaises(ValueError) as ctx:
            await service.create_session(agent_id=agent["id"], title="Test")
        self.assertIn("inactive", str(ctx.exception))

    async def test_get_or_create_session_uses_correct_agent(self):
        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent({
            "name": "Test Agent",
            "system_prompt": "You are a test agent.",
        })
        service = AssistantService(
            InMemoryAssistantRepository(),
            agent_repository=agent_repo,
        )
        session = await service.get_or_create_session(
            "existing-session-id",
            agent_id=agent["id"],
            title="Existing",
        )
        self.assertEqual(session["agent_id"], agent["id"])

    async def test_get_or_create_returns_existing_session(self):
        """Even with a different agent_id, existing session should be returned as-is."""
        agent_repo = InMemoryAgentRepository()
        agent1 = await agent_repo.create_agent({"name": "Agent1", "system_prompt": "Hi"})
        agent2 = await agent_repo.create_agent({"name": "Agent2", "system_prompt": "Hi"})
        service = AssistantService(
            InMemoryAssistantRepository(),
            agent_repository=agent_repo,
        )
        # Create with agent1
        session = await service.get_or_create_session(
            "shared-session-id",
            agent_id=agent1["id"],
            title="Session 1",
        )
        # Get with agent2 - should return existing session (agent1)
        existing = await service.get_or_create_session(
            "shared-session-id",
            agent_id=agent2["id"],
            title="Session 2",
        )
        self.assertEqual(existing["session_id"], session["session_id"])
        self.assertEqual(existing["agent_id"], agent1["id"])

    async def test_agent_config_used_in_session_created_event(self):
        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent({
            "name": "Custom Agent",
            "system_prompt": "Custom prompt here",
            "model_route_name": "custom-route",
            "max_turns": 3,
        })
        service = AssistantService(
            InMemoryAssistantRepository(),
            agent_repository=agent_repo,
        )
        session = await service.create_session(agent_id=agent["id"], title="Test")
        events = await service.list_events(session["session_id"], limit=10)
        created_events = [e for e in events if e["event_type"] == "session.created"]
        self.assertEqual(len(created_events), 1)
        self.assertEqual(created_events[0]["payload"]["agent_id"], agent["id"])


class _LongResultTool(OperationHandler):
    name = "long_result"
    description = "Return a long tool result for compaction tests."

    async def execute(self, **kwargs):
        return "tool-result:" + ("x" * 400)


class _BypassTruncationStubTool(OperationHandler):
    """Returns an oversized payload that must survive the agent-loop preview cap."""

    name = "load_skill"  # Reuse the production name so the compaction whitelist applies.
    description = "Stand-in for load_skill that returns an oversized payload."
    bypass_result_truncation = True

    async def execute(self, **kwargs):
        return "skill-body:" + ("y" * 2000)


class AssistantServiceContextCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_applies_micro_compaction_before_followup_model_turn(self):
        captured = {}

        class _Adapter:
            def __init__(self):
                self.calls = 0

            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                self.calls += 1
                if self.calls == 1:
                    return AgentTurnResponse(
                        content="calling tool",
                        tool_calls=[AgentToolCall(id="call-1", name="long_result", arguments={})],
                    )
                captured["messages"] = messages
                return AgentTurnResponse(content="done", tool_calls=[])

        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent(
            {
                "name": "Compaction Agent",
                "system_prompt": "You are a compaction test agent.",
                "max_turns": 2,
                "tool_names": ["long_result"],
                "context_compaction": {
                    "tool_result_max_chars": 80,
                },
            }
        )
        repository = InMemoryAssistantRepository()
        service = AssistantService(
            repository,
            agent_repository=agent_repo,
            model_adapter_factory=AsyncMock(return_value=_Adapter()),
            tool_registry=OperationRegistry([_LongResultTool()], tool_result_max_chars=10_000),
        )
        session = await service.create_session(agent_id=agent["id"], title="Compaction")

        await service.send_message(session_id=session["session_id"], content="Run the tool")

        tool_messages = [message for message in captured["messages"] if getattr(message, "type", None) == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("truncated", str(tool_messages[0].content).lower())
        self.assertLessEqual(len(str(tool_messages[0].content)), 80)

    async def test_send_message_keeps_full_payload_for_bypass_truncation_tool(self):
        """``bypass_result_truncation`` tools (e.g. ``load_skill``) must surface
        the full tool result in both the persisted message's
        ``content_blocks[*].result_preview`` (what the ``/messages`` endpoint
        returns) and the in-turn ``ToolMessage`` forwarded to the model."""

        captured: dict[str, list[object]] = {}

        class _Adapter:
            def __init__(self):
                self.calls = 0

            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                self.calls += 1
                if self.calls == 1:
                    return AgentTurnResponse(
                        content="calling tool",
                        tool_calls=[AgentToolCall(id="call-1", name="load_skill", arguments={})],
                    )
                captured["messages"] = list(messages)
                return AgentTurnResponse(content="done", tool_calls=[])

        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent(
            {
                "name": "Bypass Truncation Agent",
                "system_prompt": "You are a bypass-truncation test agent.",
                "max_turns": 2,
                "tool_names": ["load_skill"],
                # Aggressive compaction limit — still must not touch load_skill.
                "context_compaction": {"tool_result_max_chars": 80},
            }
        )
        repository = InMemoryAssistantRepository()
        service = AssistantService(
            repository,
            agent_repository=agent_repo,
            model_adapter_factory=AsyncMock(return_value=_Adapter()),
            tool_registry=OperationRegistry(
                [_BypassTruncationStubTool()],
                tool_result_max_chars=10_000,
            ),
        )
        session = await service.create_session(agent_id=agent["id"], title="Bypass")

        expected_payload = "skill-body:" + ("y" * 2000)

        await service.send_message(session_id=session["session_id"], content="Load it")

        tool_messages = [
            message for message in captured["messages"] if getattr(message, "type", None) == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        # The in-turn ToolMessage carries the full payload — neither the
        # registry's disk-spill nor micro-compaction trimmed it.
        self.assertEqual(str(tool_messages[0].content), expected_payload)

        stored = await repository.list_messages(session["session_id"], limit=20, offset=0)
        assistant_rows = [row for row in stored if row["role"] == "assistant"]
        self.assertEqual(len(assistant_rows), 1)
        blocks = assistant_rows[0]["metadata"].get("content_blocks") or []
        tool_blocks = [b for b in blocks if b.get("type") == "tool_call"]
        self.assertEqual(len(tool_blocks), 1)
        # /messages returns ``result_preview`` verbatim — must be the full payload.
        self.assertEqual(tool_blocks[0]["result_preview"], expected_payload)

    async def test_send_message_runs_full_compaction_when_threshold_exceeded(self):
        factory_calls: list[str] = []
        main_calls: list[list[object]] = []
        summary_calls: list[list[object]] = []

        class _MainAdapter:
            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                main_calls.append(list(messages))
                return AgentTurnResponse(content="done", tool_calls=[])

        class _SummaryAdapter:
            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                summary_calls.append(list(messages))
                return AgentTurnResponse(content="compacted summary", tool_calls=[])

        async def _factory(route_name):
            factory_calls.append(route_name or "")
            if route_name == "summary-route":
                return _SummaryAdapter()
            return _MainAdapter()

        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent(
            {
                "name": "Compaction Agent",
                "system_prompt": "You are a compaction test agent.",
                "model_route_name": "main-route",
                "context_compaction": {
                    "auto_threshold_tokens": 60,
                    "preserve_recent_messages": 2,
                    "preserve_recent_tool_pairs": 0,
                    "summary_model_route_name": "summary-route",
                },
            }
        )
        repository = InMemoryAssistantRepository()
        session = await repository.create_session(agent_id=agent["id"], title="Compaction")
        await repository.append_message(
            session_id=session["session_id"],
            role="user",
            content="earlier question " + ("x" * 120),
            linked_attempt_id="attempt-old-1",
            metadata={},
        )
        await repository.append_message(
            session_id=session["session_id"],
            role="assistant",
            content="earlier answer " + ("y" * 120),
            linked_attempt_id="attempt-old-1",
            metadata={},
        )
        service = AssistantService(
            repository,
            agent_repository=agent_repo,
            model_adapter_factory=_factory,
        )

        result = await service.send_message(session_id=session["session_id"], content="latest question")

        self.assertEqual(factory_calls, ["main-route", "summary-route"])
        self.assertEqual(len(summary_calls), 1)
        self.assertEqual(len(main_calls), 1)
        self.assertEqual(result["messages"][1]["metadata"]["context_compaction"]["full_applied"], True)
        self.assertEqual(
            result["messages"][1]["metadata"]["context_compaction"]["summary_model_route_name"],
            "summary-route",
        )

        stored_messages = await repository.list_messages(session["session_id"], limit=20, offset=0)
        summary_rows = [
            row for row in stored_messages if row["metadata"].get("context_compaction", {}).get("kind") == "summary_boundary"
        ]
        self.assertEqual(len(summary_rows), 1)
        self.assertEqual(summary_rows[0]["content"], "compacted summary")
        self.assertTrue(any(getattr(message, "content", "") == "compacted summary" for message in main_calls[0]))

        updated_session = await repository.get_session(session["session_id"])
        state = updated_session["config"]["context_compaction_state"]
        self.assertEqual(state["summary_message_id"], summary_rows[0]["message_id"])
        self.assertEqual(
            state["compacted_until_message_id"],
            summary_rows[0]["metadata"]["context_compaction"]["compacted_until_message_id"],
        )

    async def test_slash_compact_runs_manual_full_compaction_and_records_events(self):
        factory_calls: list[str] = []

        class _SummaryAdapter:
            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                return AgentTurnResponse(content="manual compacted summary", tool_calls=[])

        async def _factory(route_name):
            factory_calls.append(route_name or "")
            return _SummaryAdapter()

        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent(
            {
                "name": "Compaction Agent",
                "system_prompt": "You are a compaction test agent.",
                "model_route_name": "main-route",
                "context_compaction": {
                    "mode": "manual",
                    "allow_slash_compact": True,
                    "preserve_recent_messages": 1,
                    "preserve_recent_tool_pairs": 0,
                    "summary_model_route_name": "summary-route",
                },
            }
        )
        repository = InMemoryAssistantRepository()
        session = await repository.create_session(agent_id=agent["id"], title="Compaction")
        await repository.append_message(
            session_id=session["session_id"],
            role="user",
            content="earlier question " + ("x" * 120),
            linked_attempt_id="attempt-old-1",
            metadata={},
        )
        await repository.append_message(
            session_id=session["session_id"],
            role="assistant",
            content="earlier answer " + ("y" * 120),
            linked_attempt_id="attempt-old-1",
            metadata={},
        )
        service = AssistantService(
            repository,
            agent_repository=agent_repo,
            model_adapter_factory=_factory,
        )

        result = await service.send_message(session_id=session["session_id"], content="/compact")

        self.assertEqual(factory_calls, ["summary-route"])
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(result["messages"][0]["content"], "manual compacted summary")
        self.assertEqual(
            result["messages"][0]["metadata"]["context_compaction"]["kind"],
            "summary_boundary",
        )
        events = await repository.list_events(session["session_id"], after_id=None, limit=20)
        event_types = [row["event_type"] for row in events]
        self.assertIn("context_compaction.started", event_types)
        self.assertIn("context_compaction.completed", event_types)
        updated_session = await repository.get_session(session["session_id"])
        state = updated_session["config"]["context_compaction_state"]
        self.assertEqual(state["summary_message_id"], result["messages"][0]["message_id"])

    async def test_slash_compact_rejects_when_disabled_for_agent(self):
        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent(
            {
                "name": "Compaction Agent",
                "system_prompt": "You are a compaction test agent.",
                "context_compaction": {
                    "mode": "manual",
                    "allow_slash_compact": False,
                },
            }
        )
        repository = InMemoryAssistantRepository()
        session = await repository.create_session(agent_id=agent["id"], title="Compaction")
        await repository.append_message(
            session_id=session["session_id"],
            role="user",
            content="earlier question",
            linked_attempt_id="attempt-old-1",
            metadata={},
        )
        service = AssistantService(
            repository,
            agent_repository=agent_repo,
            model_adapter_factory=AsyncMock(),
        )

        with self.assertRaises(ValueError) as ctx:
            await service.send_message(session_id=session["session_id"], content="/compact")

        self.assertIn("disabled", str(ctx.exception).lower())


class BuiltinMainAgentLoadingTests(unittest.IsolatedAsyncioTestCase):
    """The fixed main agent loads code-controlled tools/skills/prompt at runtime,
    not from its (intentionally empty) DB row."""

    async def test_builtin_loads_full_tool_registry_and_template_prompt(self):
        from doyoutrade.assistant.main_agent import MAIN_AGENT_ID, builtin_tool_names
        from doyoutrade.assistant.service import _compose_effective_system_prompt

        agent_repo = InMemoryAgentRepository()
        await agent_repo.ensure_main_agent()
        # No tool_registry passed → service builds the full default registry.
        service = AssistantService(
            InMemoryAssistantRepository(),
            agent_repository=agent_repo,
        )
        session = await service.create_session(agent_id=MAIN_AGENT_ID, title="t")
        agent = await agent_repo.get_agent(MAIN_AGENT_ID)

        # Tools: the builtin gets the FULL registry (all base), code-controlled.
        resolved = service._resolve_tool_inventory(session, agent)
        self.assertEqual(sorted(resolved.effective_tool_names), sorted(builtin_tool_names()))
        self.assertEqual(resolved.deferred_tool_names, [])

        # Prompt: the authoritative main_agent.j2 template (large) + a skills catalog.
        prompt = _compose_effective_system_prompt(service.tool_registry, agent)
        self.assertGreater(len(prompt), 5000)

    async def test_custom_agent_with_empty_tools_gets_no_tools(self):
        """Contrast: a plain agent with no configured tools resolves to ZERO tools,
        proving the builtin's full-registry behavior is a deliberate override."""
        agent_repo = InMemoryAgentRepository()
        custom = await agent_repo.create_agent({"name": "Plain", "system_prompt": "hi"})
        service = AssistantService(
            InMemoryAssistantRepository(),
            agent_repository=agent_repo,
        )
        session = await service.create_session(agent_id=custom["id"], title="t")
        agent = await agent_repo.get_agent(custom["id"])
        resolved = service._resolve_tool_inventory(session, agent)
        self.assertEqual(resolved.effective_tool_names, [])


if __name__ == "__main__":
    unittest.main()
