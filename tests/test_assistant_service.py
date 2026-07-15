import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from doyoutrade.agent_runtime import AgentToolCall, AgentTurnResponse
from doyoutrade.assistant import AssistantService, InMemoryAssistantRepository
from doyoutrade.assistant.channels.base import ChannelDeliveryHandle
from doyoutrade.assistant.service import AssistantStoppedError
from doyoutrade.assistant.repository import InMemoryAgentRepository
from doyoutrade.tools import OperationHandler, OperationRegistry
from doyoutrade.models.base import ModelRequest, ModelResponse
from doyoutrade.assistant.title_generator import generate_session_title


class _StreamingAdapter:
    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        if on_thinking_delta is not None:
            await on_thinking_delta("思考中...")
        if on_text_delta is not None:
            await on_text_delta("你")
            await on_text_delta("好")
        return AgentTurnResponse(content="你好", tool_calls=[], raw=None)


class _ToolCallingAdapter:
    def __init__(self):
        self.calls = 0

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        self.calls += 1
        if self.calls == 1:
            return AgentTurnResponse(
                content="",
                tool_calls=[
                    AgentToolCall(
                        id="call_1",
                        name="dummy_tool",
                        arguments={"symbol": "600000.SH"},
                    )
                ],
                raw=MagicMock(tool_calls=None, content=""),
            )
        return AgentTurnResponse(
            content="done",
            tool_calls=[],
            raw=MagicMock(tool_calls=None, content="done"),
        )


class _ToolCallingWithPrefaceAdapter:
    def __init__(self):
        self.calls = 0

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        self.calls += 1
        if on_thinking_delta is not None:
            await on_thinking_delta(f"thinking-turn-{self.calls}")
        if self.calls == 1:
            return AgentTurnResponse(
                content="先检查行情再回测。",
                tool_calls=[
                    AgentToolCall(
                        id="call_1",
                        name="dummy_tool",
                        arguments={"symbol": "600000.SH"},
                    )
                ],
                raw=MagicMock(tool_calls=None, content="先检查行情再回测。"),
            )
        return AgentTurnResponse(
            content="done",
            tool_calls=[],
            raw=MagicMock(tool_calls=None, content="done"),
        )


class _AlwaysToolCallingWithPrefaceAdapter:
    """Every turn narrates a preface then calls a tool, never finishing on its
    own. Reproduces a model that keeps self-correcting tool arguments until the
    agent's ``max_turns`` budget runs out (regression for the max-turns cutoff
    notice being silently swallowed by a truthy last-turn preface).
    """

    def __init__(self):
        self.calls = 0

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        self.calls += 1
        preface = f"第{self.calls}轮说明。"
        return AgentTurnResponse(
            content=preface,
            tool_calls=[
                AgentToolCall(
                    id=f"call_{self.calls}",
                    name="dummy_tool",
                    arguments={"symbol": "600000.SH"},
                )
            ],
            raw=MagicMock(tool_calls=None, content=preface),
        )


class _StreamingPrefaceThenAnswerToolAdapter:
    """Turn 0 streams a preface and calls a tool; turn 1 streams the final answer.

    Reproduces the cross-turn duplication bug: ``streamed_text`` accumulates the
    preface, so the end-of-turn ``publish(final_text)`` re-sends only the answer
    (shorter) and trips the card controller's "shorter = new reply" boundary
    heuristic, duplicating the answer inside the card.
    """

    def __init__(self):
        self.calls = 0

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        self.calls += 1
        if self.calls == 1:
            if on_text_delta is not None:
                await on_text_delta("先检查")
                await on_text_delta("行情。")
            return AgentTurnResponse(
                content="先检查行情。",
                tool_calls=[
                    AgentToolCall(
                        id="call_1",
                        name="dummy_tool",
                        arguments={"symbol": "600000.SH"},
                    )
                ],
                raw=MagicMock(tool_calls=None, content="先检查行情。"),
            )
        if on_text_delta is not None:
            await on_text_delta("最终答案")
            await on_text_delta("是 42。")
        return AgentTurnResponse(
            content="最终答案是 42。",
            tool_calls=[],
            raw=MagicMock(tool_calls=None, content="最终答案是 42。"),
        )


class _ToolCallingThinkingAdapter:
    def __init__(self):
        self.calls = 0

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        self.calls += 1
        if on_thinking_delta is not None:
            await on_thinking_delta(f"thinking-turn-{self.calls}")
        if self.calls == 1:
            return AgentTurnResponse(
                content="",
                tool_calls=[
                    AgentToolCall(
                        id="call_1",
                        name="dummy_tool",
                        arguments={"symbol": "600000.SH"},
                    )
                ],
                raw=MagicMock(tool_calls=None, content=""),
            )
        return AgentTurnResponse(
            content="done",
            tool_calls=[],
            raw=MagicMock(tool_calls=None, content="done"),
        )


class _StopBeforeToolAdapter:
    def __init__(self, request_stop):
        self._request_stop = request_stop

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        if on_text_delta is not None:
            await on_text_delta("部分")
            await on_text_delta("回答")
        await self._request_stop()
        return AgentTurnResponse(
            content="部分回答",
            tool_calls=[
                AgentToolCall(
                    id="call_stop",
                    name="dummy_tool",
                    arguments={"symbol": "600000.SH"},
                )
            ],
            raw=MagicMock(tool_calls=None, content="部分回答"),
        )


class _BurstStreamingAdapter:
    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        if on_text_delta is not None:
            for delta in ["你", "好", "，", "T", "C"]:
                await on_text_delta(delta)
        return AgentTurnResponse(content="你好，TC", tool_calls=[], raw=None)


class _TimedStreamingAdapter:
    def __init__(self, probe):
        self._probe = probe

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        if on_text_delta is not None:
            await on_text_delta("慢")
            await asyncio.sleep(0.15)
            self._probe()
            await asyncio.sleep(0.2)
        return AgentTurnResponse(content="慢", tool_calls=[], raw=None)


class _CapturingConversationAdapter:
    def __init__(self):
        self.calls = []

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        self.calls.append(list(messages))
        return AgentTurnResponse(content=f"reply-{len(self.calls)}", tool_calls=[], raw=None)


class _DummyTool(OperationHandler):
    name = "dummy_tool"
    description = "Dummy test tool"
    category = "kline"
    parameters = {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    }

    async def execute(self, symbol: str) -> str:
        return f'{{"status":"ok","symbol":"{symbol}"}}'


class _NamedTool(OperationHandler):
    def __init__(self, name: str, description: str = "tool"):
        self.name = name
        self.description = description
        self.category = "agent"
        self.parameters = {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self) -> str:
        return '{"status":"ok"}'


class _DummyStreamingController:
    def __init__(self):
        self.partial_replies = []
        self.reasoning_streams = []
        self.tool_starts = []
        self.tool_results = []

    async def on_partial_reply(self, text):
        self.partial_replies.append(text)

    async def on_reasoning_stream(self, text):
        self.reasoning_streams.append(text)

    async def on_tool_start(self, name, *, tool_call_id=None, arguments=None, category=None):
        self.tool_starts.append(
            {
                "name": name,
                "tool_call_id": tool_call_id,
                "arguments": arguments,
                "category": category,
            }
        )

    async def on_tool_result(self, tool_call_id, *, name=None, preview=None, is_error=False):
        self.tool_results.append(
            {
                "tool_call_id": tool_call_id,
                "name": name,
                "preview": preview,
                "is_error": is_error,
            }
        )


class AssistantServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._services: list[AssistantService] = []

    async def asyncTearDown(self) -> None:
        for service in self._services:
            await service.aclose()

    def _track(self, service: AssistantService) -> AssistantService:
        self._services.append(service)
        return service

    async def test_send_message_includes_prior_session_messages_in_model_context(self):
        adapter = _CapturingConversationAdapter()

        async def _factory(_route_name):
            return adapter

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="history")

        await service.send_message(
            session_id=session["session_id"],
            content="first question",
        )
        await service.send_message(
            session_id=session["session_id"],
            content="second question",
        )

        self.assertEqual(len(adapter.calls), 2)
        second_call = adapter.calls[1]
        contents = [getattr(message, "content", "") for message in second_call]
        # A runtime-context reminder is injected right before the latest
        # user message; everything else preserves order.
        self.assertEqual(contents[0], second_call[0].content)  # system prompt
        self.assertEqual(contents[1], "first question")
        self.assertEqual(contents[2], "reply-1")
        self.assertIn("<system-reminder>", contents[3])
        self.assertIn("currentDate", contents[3])
        self.assertEqual(contents[4], "second question")
        self.assertEqual(len(contents), 5)

    async def test_runtime_context_reminder_carries_utc_plus_8_time_and_is_not_persisted(self):
        adapter = _CapturingConversationAdapter()

        async def _factory(_route_name):
            return adapter

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="time-context")

        await service.send_message(
            session_id=session["session_id"],
            content="when is now?",
        )

        first_call = adapter.calls[0]
        reminder = first_call[-2]  # injected just before the latest user message
        user_msg = first_call[-1]
        self.assertEqual(type(reminder).__name__, "HumanMessage")
        self.assertEqual(user_msg.content, "when is now?")
        self.assertIn("<system-reminder>", reminder.content)
        self.assertIn("# currentDate", reminder.content)
        self.assertIn("# currentTime", reminder.content)
        self.assertIn("# currentWeekday", reminder.content)
        self.assertIn("Asia/Shanghai, UTC+8", reminder.content)
        self.assertIn("+08:00", reminder.content)
        # Knowledge-base root is injected as an absolute path so the model
        # never guesses the server's $HOME (the file tools reject '~').
        self.assertIn("# knowledgeBase", reminder.content)
        self.assertIn(".doyoutrade/knowledge", reminder.content)

        # The reminder lives only in the in-memory prompt — persisted
        # history must stay free of it so replay / compaction stays clean.
        persisted = await repo.list_messages(session["session_id"], limit=100, offset=0)
        persisted_contents = [row.get("content", "") for row in persisted]
        for content in persisted_contents:
            self.assertNotIn("<system-reminder>", content)
            self.assertNotIn("currentDate", content)

    async def test_send_message_injects_turn_context_reminder_without_polluting_user_content(self):
        adapter = _CapturingConversationAdapter()

        async def _factory(_route_name):
            return adapter

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="reply-context")

        await service.send_message(
            session_id=session["session_id"],
            content="这个继续讲",
            turn_context_reminder=(
                "<system-reminder>\n"
                "# feishuReplyContext\n"
                "replyTargetContent:\n上一条是在问 600519 的仓位建议\n"
                "</system-reminder>"
            ),
            user_message_metadata={"channel": {"type": "feishu", "reply_target": "上一条是在问 600519 的仓位建议"}},
        )

        first_call = adapter.calls[0]
        contents = [getattr(message, "content", "") for message in first_call]
        self.assertEqual(contents[0], first_call[0].content)  # system prompt
        self.assertIn("feishuReplyContext", contents[1])
        self.assertIn("600519", contents[1])
        self.assertIn("currentDate", contents[2])
        self.assertEqual(contents[3], "这个继续讲")

        persisted = await repo.list_messages(session["session_id"], limit=100, offset=0)
        self.assertEqual(persisted[0]["content"], "这个继续讲")
        self.assertEqual(
            persisted[0]["metadata"]["channel"]["reply_target"],
            "上一条是在问 600519 的仓位建议",
        )

    async def test_register_and_resolve_channel_delivery_refs(self):
        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(repo, model_adapter_factory=AsyncMock()))
        session = await service.create_session(agent_id="test-agent", title="delivery-ref")

        await service.register_channel_delivery_refs(
            session["session_id"],
            channel_type="feishu",
            handles=[
                ChannelDeliveryHandle(
                    platform_message_id="om_reply_001",
                    platform_message_type="interactive",
                )
            ],
            canonical_text="这是 agent 的标准正文",
            source="assistant_message",
            assistant_message_id="msg-assistant-1",
        )

        ref = await service.resolve_channel_delivery_ref(
            session["session_id"],
            channel_type="feishu",
            platform_message_id="om_reply_001",
        )

        self.assertIsNotNone(ref)
        self.assertEqual(ref["channel_type"], "feishu")
        self.assertEqual(ref["platform_message_id"], "om_reply_001")
        self.assertEqual(ref["platform_message_type"], "interactive")
        self.assertEqual(ref["canonical_text"], "这是 agent 的标准正文")
        self.assertEqual(ref["assistant_message_id"], "msg-assistant-1")

    async def test_create_session_with_template_omits_snapshot_so_j2_changes_take_effect(self):
        repo = InMemoryAssistantRepository()
        agent_repo = InMemoryAgentRepository()
        await agent_repo.create_agent(
            {
                "id": "agent-template",
                "name": "Template Agent",
                "system_prompt": "legacy raw prompt",
                "system_prompt_template_id": "swing-trader",
            }
        )
        service = self._track(
            AssistantService(
                repo,
                agent_repository=agent_repo,
                model_adapter_factory=lambda _route: None,
            )
        )

        with patch(
            "doyoutrade.assistant.service.resolve_agent_system_prompt",
            return_value="initial template render",
        ):
            session = await service.create_session(agent_id="agent-template", title="templated")

        # Template-backed sessions must NOT freeze the rendered prompt —
        # otherwise edits to the .j2 file won't reach future attempts.
        self.assertNotIn("system_prompt_snapshot", session["config"])
        self.assertEqual(session["config"]["system_prompt_template_id"], "swing-trader")
        self.assertEqual(session["config"]["prompt_template_id"], "swing-trader")

    async def test_create_session_without_template_still_snapshots_raw_prompt(self):
        repo = InMemoryAssistantRepository()
        agent_repo = InMemoryAgentRepository()
        await agent_repo.create_agent(
            {
                "id": "agent-raw",
                "name": "Raw Prompt Agent",
                "system_prompt": "stable raw prompt",
                "system_prompt_template_id": None,
            }
        )
        service = self._track(
            AssistantService(
                repo,
                agent_repository=agent_repo,
                model_adapter_factory=lambda _route: None,
            )
        )

        with patch(
            "doyoutrade.assistant.service.resolve_agent_system_prompt",
            return_value="stable raw prompt",
        ):
            session = await service.create_session(agent_id="agent-raw", title="raw")

        # Without a template_id the agent's raw prompt is the user's source of
        # truth; freeze it at session-creation so later agent edits don't
        # retroactively reshape this session.
        self.assertEqual(session["config"]["system_prompt_snapshot"], "stable raw prompt")
        self.assertNotIn("system_prompt_template_id", session["config"])

    async def test_template_session_re_renders_j2_on_each_attempt(self):
        adapter = _CapturingConversationAdapter()

        async def _factory(_route_name):
            return adapter

        repo = InMemoryAssistantRepository()
        agent_repo = InMemoryAgentRepository()
        await agent_repo.create_agent(
            {
                "id": "agent-template",
                "name": "Template Agent",
                "system_prompt": "legacy raw prompt",
                "system_prompt_template_id": "swing-trader",
            }
        )
        service = self._track(
            AssistantService(
                repo,
                agent_repository=agent_repo,
                model_adapter_factory=_factory,
            )
        )

        session = await service.create_session(agent_id="agent-template", title="templated")
        self.assertNotIn("system_prompt_snapshot", session["config"])

        # Simulate the .j2 file content changing between attempts; each
        # attempt should pick up the latest render.
        with patch(
            "doyoutrade.assistant.service.resolve_agent_system_prompt",
            side_effect=["render-v1", "render-v2"],
        ):
            await service.send_message(session_id=session["session_id"], content="first")
            await service.send_message(session_id=session["session_id"], content="second")

        self.assertEqual(adapter.calls[0][0].content, "render-v1")
        self.assertEqual(adapter.calls[1][0].content, "render-v2")

    async def test_legacy_session_with_existing_snapshot_keeps_using_it(self):
        adapter = _CapturingConversationAdapter()

        async def _factory(_route_name):
            return adapter

        repo = InMemoryAssistantRepository()
        agent_repo = InMemoryAgentRepository()
        await agent_repo.create_agent(
            {
                "id": "agent-template",
                "name": "Template Agent",
                "system_prompt": "legacy raw prompt",
                "system_prompt_template_id": "swing-trader",
            }
        )
        service = self._track(
            AssistantService(
                repo,
                agent_repository=agent_repo,
                model_adapter_factory=_factory,
            )
        )

        session = await service.create_session(agent_id="agent-template", title="legacy")

        # Simulate a session created under the previous behaviour: its
        # config already carries a frozen snapshot. The attempt path must
        # honour it instead of re-rendering the .j2 (preserves backward
        # compatibility for sessions that pre-date the link-only change).
        repo.sessions[session["session_id"]]["config"]["system_prompt_snapshot"] = "legacy snapshot"

        with patch(
            "doyoutrade.assistant.service.resolve_agent_system_prompt",
            return_value="fresh render would-be",
        ) as resolve_mock:
            await service.send_message(session_id=session["session_id"], content="hello")

        self.assertEqual(adapter.calls[0][0].content, "legacy snapshot")
        resolve_mock.assert_not_called()

    async def test_new_lifecycle_command_creates_session_without_model_call(self):
        model_called = False

        async def _factory(_route_name):
            nonlocal model_called
            model_called = True
            return _StreamingAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="old")

        result = await service.send_message(
            session_id=session["session_id"],
            content="/new",
        )

        self.assertFalse(model_called)
        self.assertEqual(result["lifecycle_command"]["command"], "new")
        self.assertEqual(result["lifecycle_command"]["previous_session_id"], session["session_id"])
        self.assertNotEqual(result["session"]["session_id"], session["session_id"])
        self.assertEqual(result["session"]["agent_id"], "test-agent")
        self.assertEqual(result["session"]["title"], "")
        self.assertNotEqual(result["session"]["title"], session["title"])
        self.assertEqual(result["messages"], [])
        old_events = await service.list_events(session["session_id"], limit=50)
        self.assertIn("lifecycle.command.received", [e["event_type"] for e in old_events])
        new_events = await service.list_events(result["session"]["session_id"], limit=50)
        self.assertIn("session.created", [e["event_type"] for e in new_events])

    async def test_send_message_uses_baseline_when_model_route_empty(self):
        """Empty model_route_name falls back to baseline adapter (via factory)."""

        async def _factory(route_name):
            return _StreamingAdapter()

        service = self._track(AssistantService(
            InMemoryAssistantRepository(),
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="no-route")
        result = await service.send_message(
            session_id=session["session_id"],
            content="hello",
        )
        self.assertIn("messages", result)
        self.assertEqual(len(result["messages"]), 2)  # user + assistant

    async def test_send_message_publishes_streaming_delta_events(self):
        async def _factory(_route_name):
            return _StreamingAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(
            agent_id="test-agent",
            title="stream",
        )

        result = await service.send_message(
            session_id=session["session_id"],
            content="hello",
        )

        self.assertEqual(result["messages"][1]["content"], "你好")
        events = await service.list_events(session["session_id"], limit=50)
        delta_events = [e for e in events if e["event_type"] == "message.delta"]
        self.assertEqual([e["payload"]["delta"] for e in delta_events], ["你", "好"])
        self.assertEqual(delta_events[0]["payload"]["content"], "你")
        self.assertEqual(delta_events[1]["payload"]["content"], "你好")

    async def test_streaming_controller_batches_five_text_deltas(self):
        async def _factory(_route_name):
            return _BurstStreamingAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="stream-batch-count")
        controller = _DummyStreamingController()

        result = await service.send_message(
            session_id=session["session_id"],
            content="hello",
            streaming_controller=controller,
        )

        self.assertEqual(result["messages"][1]["content"], "你好，TC")
        self.assertEqual(controller.partial_replies, ["你好，TC"])
        events = await service.list_events(session["session_id"], limit=50)
        delta_events = [e for e in events if e["event_type"] == "message.delta"]
        self.assertEqual([e["payload"]["delta"] for e in delta_events], ["你", "好", "，", "T", "C"])
        self.assertEqual(delta_events[-1]["payload"]["content"], "你好，TC")

    async def test_streaming_controller_flushes_text_batch_after_300ms_timeout(self):
        controller = _DummyStreamingController()

        async def _factory(_route_name):
            return _TimedStreamingAdapter(lambda: self.assertEqual(controller.partial_replies, []))

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="stream-batch-time")

        result = await service.send_message(
            session_id=session["session_id"],
            content="hello",
            streaming_controller=controller,
        )

        self.assertEqual(result["messages"][1]["content"], "慢")
        self.assertEqual(controller.partial_replies, ["慢"])

    async def test_streaming_controller_receives_tool_call_and_result_events(self):
        async def _factory(_route_name):
            return _ToolCallingAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
            tool_registry=OperationRegistry([_DummyTool()]),
            max_turns=2,
        ))
        session = await service.create_session(agent_id="test-agent", title="tools")
        controller = _DummyStreamingController()

        await service.send_message(
            session_id=session["session_id"],
            content="check symbol",
            streaming_controller=controller,
        )

        self.assertEqual(
            controller.tool_starts,
            [
                {
                    "name": "dummy_tool",
                    "tool_call_id": "call_1",
                    "arguments": {"symbol": "600000.SH"},
                    "category": "kline",
                }
            ],
        )
        self.assertEqual(len(controller.tool_results), 1)
        self.assertEqual(controller.tool_results[0]["tool_call_id"], "call_1")
        self.assertEqual(controller.tool_results[0]["name"], "dummy_tool")
        self.assertIn("600000.SH", controller.tool_results[0]["preview"])
        self.assertFalse(controller.tool_results[0]["is_error"])
        self.assertIn("done", controller.partial_replies)

    async def test_streamed_answer_not_duplicated_in_card_after_preface_turn(self):
        """Streaming a preface in an earlier turn must not duplicate the final
        answer in the card. Regression for the cross-turn ``streamed_text`` /
        end-of-turn ``publish(final_text)`` interaction.
        """
        from doyoutrade.assistant.channels.feishu.card.streaming import (
            StreamingCardController,
        )

        async def _factory(_route_name):
            return _StreamingPrefaceThenAnswerToolAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
            tool_registry=OperationRegistry([_DummyTool()]),
            max_turns=3,
        ))
        session = await service.create_session(agent_id="test-agent", title="dup-bug")

        mock_cardkit = MagicMock()
        mock_cardkit.send_card_json.return_value = "msg_main"
        mock_cardkit.create_card.return_value = "card_1"
        mock_cardkit.send_card_by_card_id.return_value = "msg_1"
        mock_cardkit.stream_card_content.return_value = True
        mock_cardkit.update_card.return_value = True
        mock_cardkit.patch_message.return_value = True
        mock_cardkit.set_streaming_mode.return_value = True
        controller = StreamingCardController(
            cardkit_client=mock_cardkit,
            chat_id="chat_dup",
            receive_id="user_dup",
        )

        result = await service.send_message(
            session_id=session["session_id"],
            content="check then answer",
            streaming_controller=controller,
        )
        await controller.on_idle()

        # The assistant's final reply is the second turn's content.
        self.assertEqual(result["messages"][1]["content"], "最终答案是 42。")
        # The final answer appears exactly once and is NOT polluted by the preface:
        # each is its own card (the preface segment was finalized when the tool ran).
        self.assertEqual(controller._accumulated_text.count("最终答案是 42。"), 1)
        self.assertNotIn("先检查行情。", controller._accumulated_text)

        # The preface still reached the user, but in a separate, earlier card.
        def _card_payloads():
            payloads = []
            for call in mock_cardkit.send_card_json.call_args_list:
                payloads.append(str(call.args[0] if call.args else call.kwargs.get("card")))
            for call in mock_cardkit.patch_message.call_args_list:
                args = call.args
                payloads.append(str(args[1] if len(args) > 1 else call.kwargs.get("card")))
            return payloads

        payloads = _card_payloads()
        self.assertTrue(
            any("先检查行情。" in p for p in payloads),
            "preface segment must be delivered in its own card",
        )
        self.assertTrue(any("最终答案是 42。" in p for p in payloads))
        # No single card carries both segments.
        self.assertFalse(
            any("先检查行情。" in p and "最终答案是 42。" in p for p in payloads),
            "preface and final answer must not share a card",
        )

    async def test_max_turns_reached_surfaces_distinct_notice_not_stale_preface(self):
        """Regression: when every turn (incl. the last) narrates a preface before
        calling a tool, the loop must not silently reuse that preface as the
        "final answer" once max_turns is exhausted — the user needs a visible,
        distinct cutoff notice instead of the chat looking like it stalled after
        the last tool call.
        """

        async def _factory(_route_name):
            return _AlwaysToolCallingWithPrefaceAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
            tool_registry=OperationRegistry([_DummyTool()]),
            max_turns=2,
        ))
        session = await service.create_session(agent_id="test-agent", title="max-turns-cutoff")

        result = await service.send_message(
            session_id=session["session_id"],
            content="check symbol",
        )

        assistant_message = result["messages"][1]
        self.assertEqual(
            assistant_message["content"],
            "工具调用轮次已达上限，请缩小问题范围后继续。",
        )
        self.assertNotIn("第2轮说明。", assistant_message["content"])
        self.assertTrue(assistant_message["metadata"]["max_turns_reached"])

        content_blocks = assistant_message["metadata"]["content_blocks"]
        text_blocks = [b for b in content_blocks if b["type"] == "text"]
        self.assertEqual(
            text_blocks[-1]["content"],
            "工具调用轮次已达上限，请缩小问题范围后继续。",
        )

        events = await repo.list_events(session["session_id"], after_id=None, limit=1000)
        max_turns_events = [e for e in events if e["event_type"] == "attempt.max_turns_reached"]
        self.assertEqual(len(max_turns_events), 1)
        self.assertEqual(max_turns_events[0]["payload"]["max_turns"], 2)

    async def test_thinking_events_are_split_by_model_turn(self):
        async def _factory(_route_name):
            return _ToolCallingThinkingAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
            tool_registry=OperationRegistry([_DummyTool()]),
            max_turns=2,
        ))
        session = await service.create_session(agent_id="test-agent", title="thinking-turns")
        controller = _DummyStreamingController()

        result = await service.send_message(
            session_id=session["session_id"],
            content="check symbol",
            streaming_controller=controller,
        )

        assistant_message = result["messages"][1]
        self.assertEqual(assistant_message["metadata"]["thinking"], "thinking-turn-2")
        self.assertEqual(
            assistant_message["metadata"]["thinking_blocks"],
            [
                {"turn": 0, "content": "thinking-turn-1"},
                {"turn": 1, "content": "thinking-turn-2"},
            ],
        )
        self.assertEqual(
            assistant_message["metadata"]["content_blocks"],
            [
                {"type": "thinking", "turn": 0, "content": "thinking-turn-1"},
                {
                    "type": "tool_call",
                    "tool_call_id": "call_1",
                    "name": "dummy_tool",
                    "arguments": {"symbol": "600000.SH"},
                    "category": "kline",
                    "status": "completed",
                    "result_preview": '{"status":"ok","symbol":"600000.SH"}',
                    "is_error": False,
                },
                {"type": "thinking", "turn": 1, "content": "thinking-turn-2"},
                {"type": "text", "content": "done"},
            ],
        )
        events = await service.list_events(session["session_id"], limit=50)
        done_events = [e for e in events if e["event_type"] == "thinking.done"]
        self.assertEqual(
            [e["payload"]["thinking"] for e in done_events],
            ["thinking-turn-1", "thinking-turn-2"],
        )
        self.assertEqual([e["payload"]["turn"] for e in done_events], [0, 1])
        self.assertEqual(controller.reasoning_streams, ["thinking-turn-1", "thinking-turn-2"])

    async def test_send_message_preserves_assistant_text_before_tool_call_in_content_blocks(self):
        async def _factory(_route_name):
            return _ToolCallingWithPrefaceAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
            tool_registry=OperationRegistry([_DummyTool()]),
            max_turns=2,
        ))
        session = await service.create_session(agent_id="test-agent", title="tool-preface")

        result = await service.send_message(
            session_id=session["session_id"],
            content="check symbol",
        )

        assistant_message = result["messages"][1]
        self.assertEqual(assistant_message["content"], "done")
        self.assertEqual(
            assistant_message["metadata"]["content_blocks"],
            [
                {"type": "thinking", "turn": 0, "content": "thinking-turn-1"},
                {"type": "text", "turn": 0, "content": "先检查行情再回测。"},
                {
                    "type": "tool_call",
                    "tool_call_id": "call_1",
                    "name": "dummy_tool",
                    "arguments": {"symbol": "600000.SH"},
                    "category": "kline",
                    "status": "completed",
                    "result_preview": '{"status":"ok","symbol":"600000.SH"}',
                    "is_error": False,
                },
                {"type": "thinking", "turn": 1, "content": "thinking-turn-2"},
                {"type": "text", "content": "done"},
            ],
        )

    async def test_send_message_triggers_title_generation_for_empty_title(self):
        """首条消息时 session title 为空，send_message 返回后 title 应已被更新."""
        class _TitleCapturingAdapter:
            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                return AgentTurnResponse(content="收到", tool_calls=[], raw=None)

            def generate(self, req):
                return ModelResponse(text='{"title": "测试标题"}')

        async def _factory(_route_name):
            return _TitleCapturingAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="")

        result = await service.send_message(
            session_id=session["session_id"],
            content="帮我分析股票",
        )

        self.assertEqual(len(result["messages"]), 2)
        # 等待后台任务完成
        await asyncio.sleep(0.5)
        # 检查 session title 是否已更新
        updated = await service.get_session(session["session_id"])
        self.assertEqual(updated["title"], "测试标题")

    async def test_send_message_skips_title_when_title_already_set(self):
        """session 已有标题时，send_message 不改变标题."""

        async def _factory(_route_name):
            return _StreamingAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="已有标题")

        await service.send_message(
            session_id=session["session_id"],
            content="新消息",
        )

        await asyncio.sleep(0.3)
        updated = await service.get_session(session["session_id"])
        self.assertEqual(updated["title"], "已有标题")

    async def test_send_message_triggers_title_generation_for_channel_placeholder_title(self):
        """channel 默认占位标题（Session <sender>）不应阻止首条消息后生成真实标题。"""
        class _TitleCapturingAdapter:
            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                return AgentTurnResponse(content="收到", tool_calls=[], raw=None)

            def generate(self, req):
                return ModelResponse(text='{"title": "分析浦发银行"}')

        async def _factory(_route_name):
            return _TitleCapturingAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="Session open_id_123")

        await service.send_message(
            session_id=session["session_id"],
            content="帮我分析浦发银行这只股票",
        )

        await asyncio.sleep(0.5)
        updated = await service.get_session(session["session_id"])
        self.assertEqual(updated["title"], "分析浦发银行")

    async def test_send_message_triggers_title_generation_via_chat_ainvoke_adapter(self):
        """真实适配器只实现 chat_ainvoke 时，后台标题生成仍应成功。"""

        class _ChatOnlyTitleAdapter:
            def __init__(self):
                self.captured_messages = []

            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                return AgentTurnResponse(content="收到", tool_calls=[], raw=None)

            async def chat_ainvoke(self, messages, *, tools=None):
                self.captured_messages = list(messages)
                return ModelResponse(text='{"title": "量化择时复盘"}')

        adapter = _ChatOnlyTitleAdapter()

        async def _factory(_route_name):
            return adapter

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="")

        await service.send_message(
            session_id=session["session_id"],
            content="帮我复盘一下今天的量化择时信号",
        )

        await asyncio.sleep(0.5)
        updated = await service.get_session(session["session_id"])
        self.assertEqual(updated["title"], "量化择时复盘")
        self.assertEqual(
            [type(message).__name__ for message in adapter.captured_messages],
            ["HumanMessage"],
        )
        self.assertIn("帮我复盘一下今天的量化择时信号", adapter.captured_messages[0].content)

    async def test_list_and_get_session_include_channel_source_metadata(self):
        repo = InMemoryAssistantRepository()
        session = await repo.create_session(
            agent_id="test-agent",
            title="Channel session",
            session_id="channel:feishu-alpha:open_id_123",
            config={"channel": {"channel_id": "feishu-alpha", "channel_type": "feishu"}},
        )

        listed, total = await repo.list_sessions(limit=10, offset=0)
        fetched = await repo.get_session(session["session_id"])

        self.assertEqual(total, 1)
        self.assertEqual(
            listed[0]["channel_source"],
            {
                "is_channel_session": True,
                "channel_id": "feishu-alpha",
                "channel_type": "feishu",
            },
        )
        self.assertEqual(
            fetched["channel_source"],
            {
                "is_channel_session": True,
                "channel_id": "feishu-alpha",
                "channel_type": "feishu",
            },
        )

    async def test_list_sessions_supports_channel_and_web_filters(self):
        repo = InMemoryAssistantRepository()
        await repo.create_session(
            agent_id="test-agent",
            title="Web session",
            session_id="asst-web-1",
            config={},
        )
        await repo.create_session(
            agent_id="test-agent",
            title="Feishu session",
            session_id="channel:feishu-alpha:open_id_123",
            config={"channel": {"channel_id": "feishu-alpha", "channel_type": "feishu"}},
        )
        await repo.create_session(
            agent_id="test-agent",
            title="Other channel session",
            session_id="channel:feishu-beta:open_id_456",
            config={"channel": {"channel_id": "feishu-beta", "channel_type": "feishu"}},
        )

        all_rows, all_total = await repo.list_sessions(limit=10, offset=0)
        web_rows, web_total = await repo.list_sessions(limit=10, offset=0, source="web")
        channel_rows, channel_total = await repo.list_sessions(limit=10, offset=0, source="channel")
        alpha_rows, alpha_total = await repo.list_sessions(limit=10, offset=0, channel_id="feishu-alpha")

        self.assertEqual(all_total, 3)
        self.assertEqual(web_total, 1)
        self.assertEqual({row["session_id"] for row in web_rows}, {"asst-web-1"})
        self.assertEqual(channel_total, 2)
        self.assertEqual(alpha_total, 1)
        self.assertEqual(alpha_rows[0]["session_id"], "channel:feishu-alpha:open_id_123")
        self.assertEqual(len(all_rows), 3)

    async def test_send_message_stopped_by_user_still_generates_title(self):
        class _StopAwareTitleAdapter:
            def generate(self, req):
                return ModelResponse(text='{"title": "盘中信号复盘"}')

        async def _factory(_route_name):
            return _StopAwareTitleAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="")
        service._run_loop = AsyncMock(side_effect=AssistantStoppedError())  # type: ignore[method-assign]

        with self.assertRaisesRegex(ValueError, "Assistant stopped by user"):
            await service.send_message(
                session_id=session["session_id"],
                content="帮我复盘今天盘中的量化信号",
            )

        visible_messages = await service.list_messages(session["session_id"], limit=100, offset=0)
        self.assertEqual([row["role"] for row in visible_messages], ["user"])
        self.assertEqual(visible_messages[0]["content"], "帮我复盘今天盘中的量化信号")

        await asyncio.sleep(0.3)
        updated = await service.get_session(session["session_id"])
        self.assertEqual(updated["title"], "盘中信号复盘")

    async def test_stop_preserves_user_message_and_partial_assistant_message(self):
        repo = InMemoryAssistantRepository()
        service = None
        session = None

        async def _request_stop():
            await service.stop_attempt(session["session_id"])

        async def _factory(_route_name):
            return _StopBeforeToolAdapter(_request_stop)

        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
            tool_registry=OperationRegistry([_DummyTool()]),
        ))
        session = await service.create_session(agent_id="test-agent", title="stopped")

        with self.assertRaisesRegex(ValueError, "Assistant stopped by user"):
            await service.send_message(
                session_id=session["session_id"],
                content="请执行一个会被停止的任务",
            )

        visible_messages = await service.list_messages(session["session_id"], limit=100, offset=0)
        self.assertEqual([row["role"] for row in visible_messages], ["user", "assistant"])
        self.assertEqual(visible_messages[0]["content"], "请执行一个会被停止的任务")
        self.assertEqual(visible_messages[1]["content"], "部分回答")
        self.assertEqual(visible_messages[1]["metadata"]["stopped"], True)
        self.assertEqual(visible_messages[1]["metadata"]["partial"], True)
        self.assertEqual(
            [block["type"] for block in visible_messages[1]["metadata"]["content_blocks"]],
            ["text", "tool_call"],
        )
        self.assertFalse(any(row.get("deleted") for row in repo.messages[session["session_id"]]))

        events = await service.list_events(session["session_id"], limit=100)
        stopped_events = [event for event in events if event["event_type"] == "attempt.stopped"]
        self.assertEqual(len(stopped_events), 1)
        self.assertEqual(stopped_events[0]["payload"]["partial_content"], "部分回答")

    async def test_stop_interrupts_running_tool_call(self):
        """When the user clicks 停止 *while* a tool is executing, the abort event
        must race the in-flight tool task. Without this the tool runs to
        completion and the stop button appears to do nothing — regression
        guard for the fix in AssistantService._run_loop."""

        tool_started = asyncio.Event()

        class _BlockingTool(OperationHandler):
            name = "dummy_tool"
            description = "Blocks until cancelled"
            category = "kline"
            parameters = {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            }

            async def execute(self, symbol: str) -> str:  # type: ignore[override]
                tool_started.set()
                # Sleep long enough that without cancellation the test would
                # time out. asyncio.CancelledError must propagate so the loop
                # raises AssistantStoppedError.
                await asyncio.sleep(30)
                return f'{{"status":"ok","symbol":"{symbol}"}}'

        class _ToolThenDoneAdapter:
            def __init__(self):
                self.calls = 0

            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                self.calls += 1
                if self.calls == 1:
                    return AgentTurnResponse(
                        content="",
                        tool_calls=[
                            AgentToolCall(
                                id="call_blocking",
                                name="dummy_tool",
                                arguments={"symbol": "600000.SH"},
                            )
                        ],
                        raw=MagicMock(tool_calls=None, content=""),
                    )
                return AgentTurnResponse(content="done", tool_calls=[], raw=None)

        async def _factory(_route_name):
            return _ToolThenDoneAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
            tool_registry=OperationRegistry([_BlockingTool()]),
        ))
        session = await service.create_session(agent_id="test-agent", title="stop-mid-tool")

        sender = asyncio.create_task(
            service.send_message(
                session_id=session["session_id"],
                content="跑一个会被中途停止的工具",
            )
        )
        await asyncio.wait_for(tool_started.wait(), timeout=2.0)
        await service.stop_attempt(session["session_id"])

        with self.assertRaisesRegex(ValueError, "Assistant stopped by user"):
            await asyncio.wait_for(sender, timeout=2.0)

        events = await service.list_events(session["session_id"], limit=200)
        stopped_attempts = [e for e in events if e["event_type"] == "attempt.stopped"]
        self.assertEqual(len(stopped_attempts), 1)

        tool_results = [e for e in events if e["event_type"] == "tool.result"]
        self.assertTrue(tool_results, "expected a tool.result event for the cancelled tool")
        self.assertTrue(
            any(e["payload"].get("stopped") for e in tool_results),
            "expected at least one tool.result with stopped=True",
        )

        sess = await service.get_session(session["session_id"])
        self.assertEqual(sess["status"], "idle")

    async def test_stop_interrupts_running_model_turn(self):
        """Stop must cancel an in-flight LLM turn, not only long-running tools."""

        model_started = asyncio.Event()

        class _BlockingModelAdapter:
            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                model_started.set()
                await asyncio.sleep(30)
                return AgentTurnResponse(content="late", tool_calls=[], raw=None)

        async def _factory(_route_name):
            return _BlockingModelAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="stop-mid-model")

        sender = asyncio.create_task(
            service.send_message(
                session_id=session["session_id"],
                content="请生成一段很长的分析",
            )
        )
        await asyncio.wait_for(model_started.wait(), timeout=2.0)
        result = await service.stop_attempt(session["session_id"])
        self.assertTrue(result["stopped"])
        self.assertTrue(result["active"])

        with self.assertRaisesRegex(ValueError, "Assistant stopped by user"):
            await asyncio.wait_for(sender, timeout=2.0)

        events = await service.list_events(session["session_id"], limit=200)
        stopped_attempts = [e for e in events if e["event_type"] == "attempt.stopped"]
        self.assertEqual(len(stopped_attempts), 1)

        sess = await service.get_session(session["session_id"])
        self.assertEqual(sess["status"], "idle")

    async def test_send_message_persists_assistant_message_after_caller_cancellation(self):
        """Browser refresh / client disconnect cancels send_message's awaiter, but the
        shielded run task must keep going and persist the assistant message."""

        started = asyncio.Event()
        allow_finish = asyncio.Event()

        class _SlowAdapter:
            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                started.set()
                await allow_finish.wait()
                return AgentTurnResponse(content="完成回复", tool_calls=[], raw=None)

        async def _factory(_route_name):
            return _SlowAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="t")

        caller = asyncio.create_task(
            service.send_message(session_id=session["session_id"], content="hello")
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)

        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller

        # Run is still pending in background; user message is already persisted but
        # the assistant message must not have been written yet.
        messages_during = await service.list_messages(session["session_id"], limit=100, offset=0)
        self.assertEqual([row["role"] for row in messages_during], ["user"])

        # Let the run finish; the shielded task should write the assistant message.
        allow_finish.set()
        for _ in range(50):
            await asyncio.sleep(0.05)
            messages_after = await service.list_messages(session["session_id"], limit=100, offset=0)
            if len(messages_after) == 2:
                break
        else:
            self.fail("assistant message was not persisted after caller cancellation")

        self.assertEqual([row["role"] for row in messages_after], ["user", "assistant"])
        self.assertEqual(messages_after[1]["content"], "完成回复")

        sess = await service.get_session(session["session_id"])
        self.assertEqual(sess["status"], "idle")

        events = await service.list_events(session["session_id"], limit=100)
        completed = [event for event in events if event["event_type"] == "attempt.completed"]
        self.assertEqual(len(completed), 1)

    async def test_aclose_cancels_background_title_task(self):
        started = threading.Event()

        class _SlowTitleAdapter:
            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                return AgentTurnResponse(content="收到", tool_calls=[], raw=None)

            def generate(self, req):
                import time

                started.set()
                time.sleep(5)
                return ModelResponse(text='{"title": "不会写入"}')

        async def _factory(_route_name):
            return _SlowTitleAdapter()

        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            model_adapter_factory=_factory,
        ))
        session = await service.create_session(agent_id="test-agent", title="")

        await service.send_message(
            session_id=session["session_id"],
            content="触发标题生成",
        )
        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1.0)

        await service.aclose()
        self._services.remove(service)
        updated = await service.get_session(session["session_id"])
        self.assertEqual(updated["title"], "")

    async def test_send_message_only_exposes_base_tools_before_deferred_activation(self):
        captured_tools = []

        class _CapturingToolsAdapter:
            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                captured_tools.append([tool["function"]["name"] for tool in (tools or [])])
                return AgentTurnResponse(content="done", tool_calls=[], raw=None)

        async def _factory(_route_name):
            return _CapturingToolsAdapter()

        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent({
            "id": "agent-tools",
            "name": "Tool Config Agent",
            "system_prompt": "hi",
            "tool_configs": [
                {"name": "read_file", "load_mode": "base"},
                {"name": "create_task", "load_mode": "deferred"},
            ],
        })
        service = self._track(AssistantService(
            InMemoryAssistantRepository(),
            agent_repository=agent_repo,
            model_adapter_factory=_factory,
            tool_registry=OperationRegistry(
                [
                    _NamedTool("read_file"),
                    _NamedTool("create_task"),
                ]
            ),
        ))
        session = await service.create_session(agent_id=agent["id"], title="tools")

        await service.send_message(
            session_id=session["session_id"],
            content="hello",
        )

        self.assertEqual(captured_tools, [["discover_tools", "read_file"]])

    async def test_discover_tools_can_activate_deferred_tools_for_session(self):
        captured_tools = []

        class _ActivatingAdapter:
            def __init__(self):
                self.calls = 0

            async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
                self.calls += 1
                captured_tools.append([tool["function"]["name"] for tool in (tools or [])])
                if self.calls == 1:
                    return AgentTurnResponse(
                        content="",
                        tool_calls=[
                            AgentToolCall(
                                id="call_discover",
                                name="discover_tools",
                                arguments={"activate": ["create_task"]},
                            )
                        ],
                        raw=MagicMock(tool_calls=None, content=""),
                    )
                return AgentTurnResponse(content="done", tool_calls=[], raw=MagicMock(tool_calls=None, content="done"))

        adapter = _ActivatingAdapter()

        async def _factory(_route_name):
            return adapter

        agent_repo = InMemoryAgentRepository()
        agent = await agent_repo.create_agent({
            "id": "agent-tools",
            "name": "Tool Config Agent",
            "system_prompt": "hi",
            "tool_configs": [
                {"name": "read_file", "load_mode": "base"},
                {"name": "create_task", "load_mode": "deferred"},
            ],
        })
        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(
            repo,
            agent_repository=agent_repo,
            model_adapter_factory=_factory,
            tool_registry=OperationRegistry(
                [
                    _NamedTool("read_file"),
                    _NamedTool("create_task"),
                ]
            ),
            max_turns=2,
        ))
        session = await service.create_session(agent_id=agent["id"], title="tools")

        await service.send_message(
            session_id=session["session_id"],
            content="activate deferred",
        )

        self.assertEqual(
            captured_tools,
            [
                ["discover_tools", "read_file"],
                ["discover_tools", "create_task", "read_file"],
            ],
        )
        updated = await service.get_session(session["session_id"])
        self.assertEqual(
            updated["config"].get("tool_inventory_state"),
            {"activated_deferred_tool_names": ["create_task"]},
        )
        events = await service.list_events(session["session_id"], limit=50)
        event_types = [event["event_type"] for event in events]
        self.assertIn("tool_inventory.resolved", event_types)
        self.assertIn("tool_inventory.deferred_activated", event_types)

    async def test_list_events_tail_returns_most_recent_not_oldest(self):
        """`tail=True` with no `after_id` must hand back the newest `limit`
        events (chronologically ordered), not the oldest — the frontend uses
        this to reconstruct "is a run currently in flight" state on page
        load, and a long session's oldest events would otherwise silently
        stand in for its current state (the bug behind an assistant chat
        turn showing a stale tool-call card from a previous, already-finished
        attempt)."""
        repo = InMemoryAssistantRepository()
        service = self._track(AssistantService(repo, model_adapter_factory=AsyncMock()))
        # create_session already appends a "session.created" event, so the
        # oldest-first page below has one non-delta row ahead of "0".
        session = await service.create_session(agent_id="test-agent", title="tail")
        session_id = session["session_id"]
        for i in range(10):
            await repo.append_event(
                session_id=session_id,
                event_type="thinking.delta",
                payload={"attempt_id": "attempt-old", "delta": str(i)},
            )

        default_page = await service.list_events(session_id, limit=5)
        default_deltas = [e["payload"]["delta"] for e in default_page if e["event_type"] == "thinking.delta"]
        self.assertEqual(default_deltas, ["0", "1", "2", "3"])

        tail_page = await service.list_events(session_id, limit=5, tail=True)
        self.assertEqual([e["payload"]["delta"] for e in tail_page], ["5", "6", "7", "8", "9"])

        # A forward-paginated read (after_id set) is unaffected by `tail`.
        forward = await service.list_events(
            session_id, after_id=default_page[-1]["event_id"], limit=3, tail=True
        )
        self.assertEqual([e["payload"]["delta"] for e in forward], ["4", "5", "6"])


class TitleGeneratorTests(unittest.IsolatedAsyncioTestCase):

    async def test_generate_session_title_returns_title_from_model(self):
        """模型返回 {"title": "分析上证指数"} 时，函数返回 "分析上证指数"."""
        captured_request = []

        class _FakeAdapter:
            def generate(self, req: ModelRequest) -> ModelResponse:
                captured_request.append(req)
                return ModelResponse(text='{"title": "分析上证指数"}')

        async def _factory(route):
            return _FakeAdapter()

        title = await generate_session_title(
            "帮我分析上证指数走势",
            "some-route",
            _factory,
        )
        self.assertEqual(title, "分析上证指数")
        self.assertIn("分析上证指数走势", captured_request[0].user_prompt)

    async def test_generate_session_title_truncates_long_message(self):
        """超过800字符的消息被截断."""
        captured_content = []

        class _FakeAdapter:
            def generate(self, req: ModelRequest) -> ModelResponse:
                captured_content.append(req.user_prompt)
                return ModelResponse(text='{"title": "测试"}')

        async def _factory(route):
            return _FakeAdapter()

        long_msg = "帮" + "我" * 1000
        await generate_session_title(long_msg, "route", _factory)
        self.assertLess(len(captured_content[0]), len(long_msg) + 200)

    async def test_generate_session_title_returns_none_on_parse_error(self):
        """模型返回非 JSON 时，返回 None."""
        class _FakeAdapter:
            def generate(self, req: ModelRequest) -> ModelResponse:
                return ModelResponse(text="这不是 JSON")

        async def _factory(route):
            return _FakeAdapter()

        title = await generate_session_title("hello", "route", _factory)
        self.assertIsNone(title)

    async def test_generate_session_title_returns_none_on_adapter_error(self):
        """adapter.generate 抛异常时，返回 None."""
        class _FakeAdapter:
            def generate(self, req: ModelRequest) -> ModelResponse:
                raise RuntimeError("model error")

        async def _factory(route):
            return _FakeAdapter()

        title = await generate_session_title("hello", "route", _factory)
        self.assertIsNone(title)

    async def test_generate_session_title_timeout(self):
        """超过10秒时返回 None."""
        class _SlowAdapter:
            def generate(self, req: ModelRequest) -> ModelResponse:
                raise asyncio.TimeoutError()

        async def _factory(route):
            return _SlowAdapter()

        title = await generate_session_title("hello", "route", _factory)
        self.assertIsNone(title)

    async def test_generate_session_title_falls_back_to_chat_ainvoke(self):
        captured_messages = []

        class _FakeAdapter:
            async def chat_ainvoke(self, messages, *, tools=None):
                captured_messages.extend(messages)
                return ModelResponse(text='{"title": "波段计划"}')

        async def _factory(route):
            return _FakeAdapter()

        title = await generate_session_title("请帮我做一个波段交易计划", "route", _factory)

        self.assertEqual(title, "波段计划")
        self.assertEqual([type(message).__name__ for message in captured_messages], ["HumanMessage"])
        self.assertIn("请帮我做一个波段交易计划", captured_messages[0].content)
