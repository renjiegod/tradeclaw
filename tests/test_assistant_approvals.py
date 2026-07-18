"""Blocking tool-call approval regression: matching calls suspend on the
broker future; approve lets the tool run, reject/timeout return structured
errors the model can react to, approve_always persists a session allowlist,
and every transition lands as an approval.* event."""

import asyncio
import unittest

from doyoutrade.assistant import AssistantService, InMemoryAssistantRepository
from doyoutrade.assistant.approvals import (
    DEFAULT_APPROVAL_RULES,
    ApprovalRule,
    match_approval_rule,
)
from doyoutrade.tools import OperationHandler, OperationRegistry
from tests.scripted_model import ScriptedModelAdapter, call_tool, say

_RULES = (
    ApprovalRule(
        key="dangerous_echo",
        tool="execute_bash",
        command_pattern=r"\btask\s+start\b",
        description="启动交易任务（测试）",
        timeout_seconds=5.0,
    ),
)


class _FakeBashTool(OperationHandler):
    name = "execute_bash"
    description = "fake bash"
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, command: str) -> str:
        self.executed.append(command)
        return f'{{"status":"ok","command":"{command}"}}'


class ApprovalRuleMatchTests(unittest.TestCase):
    def test_default_rules_cover_real_money_paths(self):
        cases = {
            "doyoutrade-cli strategy promote sd-1 --task task-1": "strategy_promote",
            "doyoutrade-cli task start task-9": "task_start",
            "doyoutrade-cli task stop task-9": "task_stop",
            "doyoutrade-cli task delete task-9": "task_delete",
            "doyoutrade-cli task trigger add task-1 --cron '* * * * *' --intent trade": "trigger_trade_intent",
            "doyoutrade-cli account set-default acct-1": "account_write",
            "doyoutrade-cli knowledge graph-sync": "knowledge_graph_write",
        }
        for command, expected_key in cases.items():
            rule = match_approval_rule(
                DEFAULT_APPROVAL_RULES, "execute_bash", {"command": command}
            )
            self.assertIsNotNone(rule, command)
            self.assertEqual(rule.key, expected_key, command)

    def test_graph_writes_never_allow_session_allowlisting(self):
        rule = match_approval_rule(
            DEFAULT_APPROVAL_RULES,
            "execute_bash",
            {"command": "doyoutrade-cli knowledge graph-sync"},
        )

        self.assertIsNotNone(rule)
        self.assertFalse(rule.allow_always)

    def test_benign_commands_pass(self):
        for command in (
            "doyoutrade-cli task list",
            "doyoutrade-cli backtest run --definition sd-1",
            "doyoutrade-cli stock lookup 招商银行",
        ):
            self.assertIsNone(
                match_approval_rule(
                    DEFAULT_APPROVAL_RULES, "execute_bash", {"command": command}
                ),
                command,
            )


class ApprovalPrefixHelpersTests(unittest.TestCase):
    def test_suggest_and_match_command_prefix(self):
        from doyoutrade.assistant.approvals import (
            command_matches_prefix,
            is_auto_approved,
            suggest_command_prefix,
        )

        command = "doyoutrade-cli task start task-1"
        prefix = suggest_command_prefix(command)
        self.assertEqual(prefix, "doyoutrade-cli task start:*")
        self.assertTrue(command_matches_prefix(command, prefix))
        self.assertTrue(
            command_matches_prefix("doyoutrade-cli task start other", prefix)
        )
        self.assertFalse(command_matches_prefix("doyoutrade-cli task stop x", prefix))

        rule = ApprovalRule(
            key="task_start",
            tool="execute_bash",
            description="启动交易任务",
            command_pattern=r"\btask\s+start\b",
        )
        ok, source = is_auto_approved(
            rule=rule,
            command=command,
            session_rule_keys=[],
            session_prefixes=[],
            persistent_rule_keys=[],
            persistent_prefixes=[prefix],
        )
        self.assertTrue(ok)
        self.assertEqual(source, "persistent_prefix")

    def test_one_time_rule_never_auto_approves_even_with_prefix(self):
        from doyoutrade.assistant.approvals import is_auto_approved

        rule = ApprovalRule(
            key="knowledge_graph_write",
            tool="execute_bash",
            description="修改知识图谱",
            command_pattern=r"graph-sync",
            allow_always=False,
        )
        ok, source = is_auto_approved(
            rule=rule,
            command="doyoutrade-cli knowledge graph-sync",
            session_rule_keys=["knowledge_graph_write"],
            session_prefixes=["doyoutrade-cli knowledge:*"],
            persistent_rule_keys=["knowledge_graph_write"],
            persistent_prefixes=["doyoutrade-cli knowledge:*"],
        )
        self.assertFalse(ok)
        self.assertEqual(source, "")


class ApprovalServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._services: list[AssistantService] = []

    async def asyncTearDown(self) -> None:
        for service in self._services:
            await service.aclose()

    def _build(self, adapter: ScriptedModelAdapter, rules=_RULES):
        repo = InMemoryAssistantRepository()
        tool = _FakeBashTool()
        service = AssistantService(
            repo,
            model_adapter_factory=adapter.factory,
            tool_registry=OperationRegistry([tool]),
            approval_rules=rules,
        )
        self._services.append(service)
        return service, repo, tool

    async def _event_types(self, service, session_id: str) -> list[str]:
        events = await service.list_events(session_id, limit=200)
        return [e["event_type"] for e in events]

    async def _resolve_when_pending(
        self, service, action: str, *, reason: str = "", command_prefix: str = ""
    ) -> str:
        """Wait for a pending approval to appear, then resolve it."""
        for _ in range(200):
            pending = service.approval_broker.list_pending()
            if pending:
                approval_id = pending[0]["approval_id"]
                self.assertTrue(
                    service.approval_broker.resolve(
                        approval_id,
                        action=action,
                        source="test",
                        resolver_id="tester",
                        reason=reason,
                        command_prefix=command_prefix,
                    )
                )
                return approval_id
            await asyncio.sleep(0.01)
        raise AssertionError("no approval became pending")

    async def test_approve_once_runs_tool(self):
        adapter = ScriptedModelAdapter(
            [
                call_tool("execute_bash", {"command": "doyoutrade-cli task start task-1"}),
                say("已启动。"),
            ]
        )
        service, repo, tool = self._build(adapter)
        session = await service.create_session(agent_id="a", title="approve")
        sid = session["session_id"]

        send = asyncio.create_task(service.send_message(session_id=sid, content="启动任务"))
        await self._resolve_when_pending(service, "approve_once")
        result = await send

        self.assertEqual(tool.executed, ["doyoutrade-cli task start task-1"])
        self.assertEqual(result["messages"][-1]["content"], "已启动。")
        events = await self._event_types(service, sid)
        self.assertIn("approval.requested", events)
        self.assertIn("approval.resolved", events)
        adapter.assert_exhausted()

    async def test_reject_blocks_tool_and_model_sees_structured_error(self):
        def _expect_rejection_visible(messages, _tools):
            joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
            assert "approval_rejected" in joined, "model did not see the rejection"

        adapter = ScriptedModelAdapter(
            [
                call_tool("execute_bash", {"command": "doyoutrade-cli task start task-1"}),
                say("操作被拒绝，已停止。", expect=_expect_rejection_visible),
            ]
        )
        service, repo, tool = self._build(adapter)
        session = await service.create_session(agent_id="a", title="reject")
        sid = session["session_id"]

        send = asyncio.create_task(service.send_message(session_id=sid, content="启动任务"))
        await self._resolve_when_pending(service, "reject")
        await send

        self.assertEqual(tool.executed, [], "rejected tool must not execute")
        events = await self._event_types(service, sid)
        self.assertIn("approval.resolved", events)
        adapter.assert_exhausted()

    async def test_reject_with_reason_surfaces_to_model(self):
        def _expect_reason_visible(messages, _tools):
            joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
            assert "approval_rejected" in joined
            assert "市场未开盘" in joined

        adapter = ScriptedModelAdapter(
            [
                call_tool("execute_bash", {"command": "doyoutrade-cli task start task-1"}),
                say("已按拒绝原因停止。", expect=_expect_reason_visible),
            ]
        )
        service, repo, tool = self._build(adapter)
        session = await service.create_session(agent_id="a", title="reject-reason")
        sid = session["session_id"]

        send = asyncio.create_task(service.send_message(session_id=sid, content="启动"))
        await self._resolve_when_pending(service, "reject", reason="市场未开盘")
        await send

        self.assertEqual(tool.executed, [])
        adapter.assert_exhausted()

    async def test_approve_persist_prefix_auto_approves_next_call(self):
        from unittest.mock import patch

        from doyoutrade.config import AssistantApprovalAllowlist, AssistantSettings

        remembered: dict = {"rule_keys": [], "command_prefixes": []}

        class _Cfg:
            assistant = AssistantSettings(
                approval_allowlist=AssistantApprovalAllowlist()
            )

        def _write(patch_doc):
            allow = patch_doc["assistant"]["approval_allowlist"]
            remembered["rule_keys"] = list(allow["rule_keys"])
            remembered["command_prefixes"] = list(allow["command_prefixes"])
            _Cfg.assistant = AssistantSettings(
                approval_allowlist=AssistantApprovalAllowlist(
                    rule_keys=tuple(remembered["rule_keys"]),
                    command_prefixes=tuple(remembered["command_prefixes"]),
                )
            )
            return {"path": "test", "restart_required": False}

        adapter = ScriptedModelAdapter(
            [
                call_tool("execute_bash", {"command": "doyoutrade-cli task start task-1"}),
                say("第一次完成。"),
                call_tool("execute_bash", {"command": "doyoutrade-cli task start task-2"}),
                say("第二次完成。"),
            ]
        )
        service, repo, tool = self._build(adapter)
        session = await service.create_session(agent_id="a", title="persist")
        sid = session["session_id"]

        with (
            patch("doyoutrade.assistant.service.get_config", lambda: _Cfg()),
            patch("doyoutrade.config_store.write_config", side_effect=_write),
        ):
            send = asyncio.create_task(
                service.send_message(session_id=sid, content="启动1")
            )
            await self._resolve_when_pending(
                service,
                "approve_persist",
                command_prefix="doyoutrade-cli task start:*",
            )
            await send
            await service.send_message(session_id=sid, content="启动2")

        self.assertEqual(len(tool.executed), 2)
        self.assertEqual(
            remembered["command_prefixes"], ["doyoutrade-cli task start:*"]
        )
        events = await self._event_types(service, sid)
        self.assertIn("approval.remembered", events)
        self.assertIn("approval.auto_approved", events)
        self.assertEqual(events.count("approval.requested"), 1)
        adapter.assert_exhausted()

    async def test_approve_always_remembers_for_session(self):
        adapter = ScriptedModelAdapter(
            [
                call_tool("execute_bash", {"command": "doyoutrade-cli task start task-1"}),
                say("第一次完成。"),
                call_tool("execute_bash", {"command": "doyoutrade-cli task start task-2"}),
                say("第二次完成。"),
            ]
        )
        service, repo, tool = self._build(adapter)
        session = await service.create_session(agent_id="a", title="always")
        sid = session["session_id"]

        send = asyncio.create_task(service.send_message(session_id=sid, content="启动1"))
        await self._resolve_when_pending(service, "approve_always")
        await send

        # Second matching call must auto-approve without a pending request.
        await service.send_message(session_id=sid, content="启动2")

        self.assertEqual(len(tool.executed), 2)
        events = await self._event_types(service, sid)
        self.assertIn("approval.remembered", events)
        self.assertIn("approval.auto_approved", events)
        self.assertEqual(events.count("approval.requested"), 1)
        fresh = await repo.get_session(sid)
        self.assertEqual(fresh["config"]["approval_allowlist"], ["dangerous_echo"])
        adapter.assert_exhausted()

    async def test_one_time_rule_rejects_approve_always(self):
        def _expect_forbidden_visible(messages, _tools):
            joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
            assert "approval_always_forbidden" in joined

        adapter = ScriptedModelAdapter(
            [
                call_tool(
                    "execute_bash",
                    {"command": "doyoutrade-cli knowledge graph-sync"},
                ),
                say("图谱同步未执行。", expect=_expect_forbidden_visible),
            ]
        )
        rules = (
            ApprovalRule(
                key="knowledge_graph_write",
                tool="execute_bash",
                command_pattern=r"\bknowledge\s+graph-sync\b",
                description="修改知识图谱",
                timeout_seconds=5.0,
                allow_always=False,
            ),
        )
        service, repo, tool = self._build(adapter, rules=rules)
        session = await service.create_session(agent_id="a", title="one-time")
        sid = session["session_id"]

        send = asyncio.create_task(
            service.send_message(session_id=sid, content="同步图谱")
        )
        await self._resolve_when_pending(service, "approve_always")
        await send

        self.assertEqual(tool.executed, [])
        fresh = await repo.get_session(sid)
        self.assertEqual(
            fresh["config"].get("approval_allowlist", []),
            [],
        )
        adapter.assert_exhausted()

    async def test_timeout_returns_structured_error(self):
        rules = (
            ApprovalRule(
                key="dangerous_echo",
                tool="execute_bash",
                command_pattern=r"\btask\s+start\b",
                description="启动交易任务（测试）",
                timeout_seconds=0.05,
            ),
        )

        def _expect_timeout_visible(messages, _tools):
            joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
            assert "approval_timeout" in joined

        adapter = ScriptedModelAdapter(
            [
                call_tool("execute_bash", {"command": "doyoutrade-cli task start task-1"}),
                say("审批超时，未执行。", expect=_expect_timeout_visible),
            ]
        )
        service, repo, tool = self._build(adapter, rules=rules)
        session = await service.create_session(agent_id="a", title="timeout")
        sid = session["session_id"]

        await service.send_message(session_id=sid, content="启动任务")

        self.assertEqual(tool.executed, [])
        events = await self._event_types(service, sid)
        self.assertIn("approval.timeout", events)
        adapter.assert_exhausted()

    async def test_late_resolve_after_timeout_is_refused(self):
        rules = (
            ApprovalRule(
                key="dangerous_echo",
                tool="execute_bash",
                command_pattern=r"\btask\s+start\b",
                description="t",
                timeout_seconds=0.05,
            ),
        )
        adapter = ScriptedModelAdapter(
            [
                call_tool("execute_bash", {"command": "doyoutrade-cli task start task-1"}),
                say("超时。"),
            ]
        )
        service, repo, tool = self._build(adapter, rules=rules)
        session = await service.create_session(agent_id="a", title="late")
        sid = session["session_id"]
        await service.send_message(session_id=sid, content="启动")
        # The request is discarded after the wait; a late click must report False.
        self.assertFalse(
            service.approval_broker.resolve(
                "appr-deadbeef", action="approve_once", source="test"
            )
        )


class ApprovalFeishuCardTests(unittest.TestCase):
    def test_card_shape(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_approval_card
        from doyoutrade.assistant.channels.feishu.card.validation import (
            assert_valid_card_json_v2,
        )

        card = build_approval_card(
            {
                "approval_id": "appr-1",
                "description": "启动交易任务",
                "command_preview": "doyoutrade-cli task start task-1",
                "timeout_seconds": 300,
            }
        )
        self.assertEqual(card["schema"], "2.0")
        assert_valid_card_json_v2(card, name="approval_card")

        def _buttons(value):
            if isinstance(value, dict):
                found = [value] if value.get("tag") == "button" else []
                for child in value.values():
                    found.extend(_buttons(child))
                return found
            if isinstance(value, list):
                found = []
                for child in value:
                    found.extend(_buttons(child))
                return found
            return []

        buttons = _buttons(card)
        decisions = [b["value"]["decision"] for b in buttons]
        self.assertEqual(
            decisions,
            ["approve_once", "approve_always", "approve_persist", "reject"],
        )
        self.assertTrue(all(b["value"]["action"] == "approval_resolve" for b in buttons))
        self.assertTrue(all(b["value"]["approval_id"] == "appr-1" for b in buttons))
        # Editable prefix + reject-reason inputs must be present.
        names = []

        def _names(value):
            if isinstance(value, dict):
                if value.get("tag") == "input" and value.get("name"):
                    names.append(value["name"])
                for child in value.values():
                    _names(child)
            elif isinstance(value, list):
                for child in value:
                    _names(child)

        _names(card)
        self.assertIn("approval_command_prefix", names)
        self.assertIn("approval_reject_reason", names)

    def test_one_time_card_omits_approve_always(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_approval_card

        card = build_approval_card(
            {
                "approval_id": "appr-graph",
                "description": "修改知识图谱",
                "command_preview": "doyoutrade-cli knowledge graph-sync",
                "timeout_seconds": 300,
                "allow_always": False,
            }
        )

        def _decisions(value):
            if isinstance(value, dict):
                current = (
                    [value["value"]["decision"]]
                    if value.get("tag") == "button"
                    else []
                )
                for child in value.values():
                    current.extend(_decisions(child))
                return current
            if isinstance(value, list):
                current = []
                for child in value:
                    current.extend(_decisions(child))
                return current
            return []

        self.assertEqual(_decisions(card), ["approve_once", "reject"])


if __name__ == "__main__":
    unittest.main()
