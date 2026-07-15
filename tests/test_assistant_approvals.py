"""Blocking tool-call approval regression: matching calls suspend on the
broker future; approve lets the tool run, reject/timeout return structured
errors the model can react to, approve_always persists a session allowlist,
and every transition lands as an approval.* event."""

import asyncio
import unittest
from typing import Any

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
        }
        for command, expected_key in cases.items():
            rule = match_approval_rule(
                DEFAULT_APPROVAL_RULES, "execute_bash", {"command": command}
            )
            self.assertIsNotNone(rule, command)
            self.assertEqual(rule.key, expected_key, command)

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

    async def _resolve_when_pending(self, service, action: str) -> str:
        """Wait for a pending approval to appear, then resolve it."""
        for _ in range(200):
            pending = service.approval_broker.list_pending()
            if pending:
                approval_id = pending[0]["approval_id"]
                self.assertTrue(
                    service.approval_broker.resolve(
                        approval_id, action=action, source="test", resolver_id="tester"
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
        self.assertEqual(decisions, ["approve_once", "approve_always", "reject"])
        self.assertTrue(all(b["value"]["action"] == "approval_resolve" for b in buttons))
        self.assertTrue(all(b["value"]["approval_id"] == "appr-1" for b in buttons))


if __name__ == "__main__":
    unittest.main()
