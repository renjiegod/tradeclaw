import unittest
from unittest.mock import MagicMock, AsyncMock, patch

from doyoutrade.assistant.service import AssistantService
from doyoutrade.assistant.repository import InMemoryAssistantRepository


class TestSlashCommandInterception(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.repo = InMemoryAssistantRepository()
        self.service = AssistantService(repository=self.repo)

    async def _create_session(self):
        session = await self.service.repository.create_session(
            agent_id="test-agent", title="", session_id="s1"
        )
        return session

    async def test_slash_injects_skill_invocation_message(self):
        await self._create_session()

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.content = "done"
        mock_response.tool_calls = []
        mock_response.raw = MagicMock(tool_calls=None, content="done")
        mock_adapter.agent_turn = AsyncMock(return_value=mock_response)

        async def _factory(_route_name):
            return mock_adapter

        self.service.model_adapter_factory = _factory

        with patch("doyoutrade.assistant.slash_commands._build_cache"):
            mock_skill = MagicMock()
            mock_skill.name = "technical-basic"
            mock_skill.body = "## Tech\nContent"
            with patch("doyoutrade.assistant.slash_commands._skill_commands_cache", {
                "technical-basic": mock_skill
            }):
                await self.service.send_message(session_id="s1", content="/technical-basic")

        messages = await self.service.repository.list_messages("s1", limit=100, offset=0)
        # 第一条是用户消息（session 创建不产生消息）
        user_msg = messages[0]
        assert "<invoke_skill_loaded" in user_msg["content"]
        assert 'skill="technical-basic"' in user_msg["content"]


class TestSystemPromptWithPreload(unittest.IsolatedAsyncioTestCase):
    """Tests for skill preload append-to-custom-prompt behavior in _run_loop."""

    def setUp(self):
        self.repo = InMemoryAssistantRepository()
        self.service = AssistantService(repository=self.repo)

    async def _create_session(self):
        session = await self.service.repository.create_session(
            agent_id="test-agent", title="", session_id="s1"
        )
        return session

    async def test_append_preload_to_custom_system_prompt(self):
        """When agent has custom system_prompt AND skill_names, metadata-only preload is appended."""
        from langchain_core.messages import SystemMessage

        await self._create_session()

        # Capture messages sent to the model
        captured_messages = []

        async def capture_agent_turn(messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
            captured_messages.extend(messages)
            return MagicMock(
                content="done",
                tool_calls=[],
                raw=MagicMock(tool_calls=None, content="done"),
            )

        mock_adapter = MagicMock()
        mock_adapter.agent_turn = capture_agent_turn

        async def _factory(_route_name):
            return mock_adapter

        self.service.model_adapter_factory = _factory

        # Inject mock agent_repo with custom system_prompt + skill_names
        mock_agent_repo = AsyncMock()
        mock_agent_repo.get_agent = AsyncMock(return_value={
            "system_prompt": "Custom base prompt here.",
            "skill_names": ["technical-basic"],
            "max_turns": 1,
        })
        self.service.agent_repo = mock_agent_repo

        await self.service.send_message(session_id="s1", content="hello")

        # Find the SystemMessage
        system_msgs = [m for m in captured_messages if isinstance(m, SystemMessage)]
        assert len(system_msgs) == 1
        prompt = system_msgs[0].content

        # Should contain both custom prompt AND the metadata-only skill catalog.
        assert "Custom base prompt here." in prompt
        assert "## Reference Skills" in prompt
        assert "technical-basic" in prompt
        # Full SKILL.md bodies are loaded on demand via load_skill, not inlined.
        assert "# Core Technical Indicator Collection" not in prompt


class TestSystemPromptUnit(unittest.TestCase):
    """Unit tests for _build_system_prompt (preload behavior when building base prompt)."""

    def test_build_system_prompt_with_agent_skill_names(self):
        from doyoutrade.assistant.service import _build_system_prompt
        from doyoutrade.tools import OperationRegistry

        registry = OperationRegistry()

        # No custom system_prompt, has skill_names -> preload is built into _build_system_prompt
        mock_agent = {
            "system_prompt": None,
            "skill_names": ["technical-basic"],
        }

        with patch("doyoutrade.assistant.service.build_preloaded_skills_prompt") as mock_preload:
            mock_preload.return_value = "<skill_system><skill name=\"technical-basic\">content</skill></skill_system>"

            prompt = _build_system_prompt(registry, agent=mock_agent)

            assert "<skill_system>" in prompt
            mock_preload.assert_called_once_with(["technical-basic"])

    def test_build_system_prompt_without_agent_returns_base(self):
        from doyoutrade.assistant.service import _build_system_prompt
        from doyoutrade.tools import OperationRegistry

        registry = OperationRegistry()
        prompt = _build_system_prompt(registry, agent=None)
        assert "<skill_system>" not in prompt
        assert "你是 DoYouTrade 的研究与策略 Agent" in prompt

    def test_main_agent_template_contains_cron_routing_rule(self):
        """Regression guard for tmp/error_request.json: the main-agent prompt
        template must route delayed / scheduled intents to
        ``doyoutrade-cli cron create`` instead of letting the model fall
        back to ``execute_bash sleep 60``. The rule lives in main_agent.j2
        (not in code) — this test catches a future edit that drops it.
        Post-2026-05-23 the agent's in-process cron tools are gone; the
        only legal path is ``doyoutrade-cli cron create`` via
        ``execute_bash``."""
        from doyoutrade.assistant.prompt_templates import render_prompt_template

        rendered = render_prompt_template("main-agent")
        assert "doyoutrade-cli cron create" in rendered
        assert "execute_bash" in rendered and "sleep" in rendered

    def test_main_agent_template_contains_resource_tasks_quick_reference(self):
        """Regression guard for the second tmp/error_request.json incident:
        the model loaded ``strategy-authoring`` + ``doyoutrade-backtest`` just
        to discover ``strategy inspect`` for a pure resource task. The
        template must surface the resource-tasks table inline so the model
        doesn't need to ``load_skill`` for the canonical entry points.
        """
        from doyoutrade.assistant.prompt_templates import render_prompt_template

        rendered = render_prompt_template("main-agent")
        assert "资源任务速查表" in rendered
        # The headline CLI verbs MUST be present so the model lands on the
        # right command without skill loads.
        assert "doyoutrade-cli stock lookup" in rendered
        assert "doyoutrade-cli strategy inspect" in rendered
        assert "doyoutrade-cli strategy definition list" in rendered
        # StrategyInstance / ``si-`` bindings were removed; tasks bind a
        # definition directly via strategy bind / promote.
        assert "strategy bind" in rendered
        assert "doyoutrade-cli backtest run" in rendered
        # The report-path contract must be visible at the top level so the
        # model reads the file instead of fabricating KPIs from the inline
        # ToolResult.text.
        assert "data.report_path" in rendered
        assert "read_file" in rendered

    def test_main_agent_template_contains_inspect_first_resource_rule(self):
        """Regression guard for the 'load strategy-authoring just to find
        strategy inspect' pattern: the template must have a hard rule
        telling the agent to start resource tasks with ``stock lookup`` +
        ``strategy inspect`` in parallel — only loading ``strategy-authoring``
        when inspect returns zero results.
        """
        from doyoutrade.assistant.prompt_templates import render_prompt_template

        rendered = render_prompt_template("main-agent")
        # The "起手式" section header anchors the rule.
        assert "资源任务起手式" in rendered
        # Both parallel calls must be named explicitly so the model wires
        # them together in turn 1.
        assert "doyoutrade-cli stock lookup" in rendered
        assert "doyoutrade-cli strategy inspect --query" in rendered
        # The negative trigger — "DO NOT load strategy-authoring for resource
        # reuse" — has to be inline, not deferred to a skill body.
        assert "strategy-authoring" in rendered

    def test_main_agent_template_contains_envelope_contract(self):
        """The envelope shape + ``error.error_code`` dispatch rule used to
        live only inside an opt-in skill (``doyoutrade-shared``, since
        deleted). After merging into main_agent.j2, the contract must be
        inline so every session has it without an extra ``load_skill`` call.
        """
        from doyoutrade.assistant.prompt_templates import render_prompt_template

        rendered = render_prompt_template("main-agent")
        # Envelope shape token + exit code table presence.
        assert "error.error_code" in rendered
        assert "did_you_mean" in rendered or "suggested_path" in rendered
        # Stable error tokens the agent branches on.
        assert "unknown_command" in rendered
        assert "unknown_option" in rendered
