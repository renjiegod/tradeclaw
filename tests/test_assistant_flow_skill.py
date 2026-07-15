"""End-to-end flow-skill regression: load_skill starts the flow, per-attempt
reminders carry the current node, <choice> replies advance / complete /
abort, transitions persist as flow.* events, and <choice> tags never reach
the persisted assistant message."""

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from doyoutrade.assistant import AssistantService, InMemoryAssistantRepository
from doyoutrade.skills.types import Skill
from doyoutrade.tools import LoadSkillTool, OperationRegistry
from tests.scripted_model import ScriptedModelAdapter, call_tool, say

_FLOW_BODY = """目的：演示流程。

```mermaid
flowchart TB
    A(["BEGIN"]) --> B[收集需求]
    B --> C{需求清晰吗}
    C -->|清晰| D[实现]
    C -->|不清晰| B
    D --> E(["END"])
```
"""


def _flow_skill() -> Skill:
    return Skill(
        name="demo-flow",
        description="demo flow skill",
        skill_dir=Path("/tmp/demo-flow"),
        skill_file=Path("/tmp/demo-flow/SKILL.md"),
        relative_path=Path("demo-flow"),
        body=_FLOW_BODY,
        skill_type="flow",
    )


class _FakeLoadedSkillRepo:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def upsert(self, **kwargs: Any) -> None:
        self.rows = [r for r in self.rows if r.get("skill_name") != kwargs.get("skill_name")]
        self.rows.append({**kwargs, "loaded_at": "now"})

    async def list_by_session(self, session_id: str) -> list[dict[str, Any]]:
        return [r for r in self.rows if r.get("session_id") == session_id]


class FlowSkillServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._services: list[AssistantService] = []

    async def asyncTearDown(self) -> None:
        for service in self._services:
            await service.aclose()

    def _build(self, adapter: ScriptedModelAdapter):
        repo = InMemoryAssistantRepository()
        loaded = _FakeLoadedSkillRepo()
        service = AssistantService(
            repo,
            model_adapter_factory=adapter.factory,
            tool_registry=OperationRegistry(
                [
                    LoadSkillTool(
                        loaded_skill_repository=loaded,
                        assistant_repository=repo,
                    )
                ]
            ),
            loaded_skill_repository=loaded,
        )
        self._services.append(service)
        return service, repo

    async def _active_flow(self, repo, session_id: str):
        session = await repo.get_session(session_id)
        return (session.get("config") or {}).get("active_flow")

    async def _event_types(self, service, session_id: str) -> list[str]:
        events = await service.list_events(session_id, limit=200)
        return [e["event_type"] for e in events]

    async def test_full_flow_lifecycle(self):
        def _expect_task_reminder(messages, _tools):
            joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
            assert "# activeFlow" in joined and "收集需求" in joined, (
                "flow reminder for the entry node was not injected"
            )

        def _expect_invalid_note(messages, _tools):
            joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
            assert "也许" in joined and "matched no" in joined, (
                "corrective note for the invalid choice was not injected"
            )

        adapter = ScriptedModelAdapter(
            [
                # attempt 1: load the flow skill, then confirm
                call_tool("load_skill", {"skill_name": "demo-flow"}),
                say("流程已开始"),
                # attempt 2: still working — no tag, node must not move
                say("我先调研一下"),
                # attempt 3: finish entry task
                say("收集完成 <choice>next</choice>", expect=_expect_task_reminder),
                # attempt 4: invalid decision choice
                say("<choice>也许</choice>"),
                # attempt 5: valid decision after corrective reminder
                say("判断：清晰 <choice>清晰</choice>", expect=_expect_invalid_note),
                # attempt 6: final task → END
                say("实现完毕 <choice>next</choice>"),
            ]
        )
        service, repo = self._build(adapter)
        session = await service.create_session(agent_id="test-agent", title="flow")
        sid = session["session_id"]

        with patch("doyoutrade.tools.load_skills", return_value=[_flow_skill()]):
            result = await service.send_message(session_id=sid, content="开始演示流程")
            state = await self._active_flow(repo, sid)
            self.assertEqual(state["skill_name"], "demo-flow")
            self.assertEqual(state["node_id"], "B")
            # tool result told the model the flow engaged
            joined = "\n".join(adapter.calls[1].message_texts())
            self.assertIn("Flow skill engaged", joined)

            await service.send_message(session_id=sid, content="进展如何")
            state = await self._active_flow(repo, sid)
            self.assertEqual(state["node_id"], "B", "no-tag reply must not advance")

            result = await service.send_message(session_id=sid, content="继续")
            state = await self._active_flow(repo, sid)
            self.assertEqual(state["node_id"], "C")
            # the persisted reply must not contain flow-control markup
            self.assertNotIn("<choice>", result["messages"][-1]["content"])
            self.assertIn("收集完成", result["messages"][-1]["content"])

            await service.send_message(session_id=sid, content="然后呢")
            state = await self._active_flow(repo, sid)
            self.assertEqual(state["node_id"], "C", "invalid choice must not advance")
            self.assertEqual(state["invalid_choice"], "也许")

            await service.send_message(session_id=sid, content="再试")
            state = await self._active_flow(repo, sid)
            self.assertEqual(state["node_id"], "D")
            self.assertIsNone(state["invalid_choice"])

            await service.send_message(session_id=sid, content="收尾")
            self.assertFalse(await self._active_flow(repo, sid))

        adapter.assert_exhausted()
        event_types = await self._event_types(service, sid)
        self.assertEqual(event_types.count("flow.advanced"), 2)
        self.assertEqual(event_types.count("flow.choice_invalid"), 1)
        self.assertEqual(event_types.count("flow.completed"), 1)

    async def test_abort_flow_choice_clears_state(self):
        adapter = ScriptedModelAdapter(
            [
                call_tool("load_skill", {"skill_name": "demo-flow"}),
                say("开始"),
                say("不做了 <choice>abort-flow</choice>"),
            ]
        )
        service, repo = self._build(adapter)
        session = await service.create_session(agent_id="test-agent", title="abort")
        sid = session["session_id"]

        with patch("doyoutrade.tools.load_skills", return_value=[_flow_skill()]):
            await service.send_message(session_id=sid, content="开始")
            await service.send_message(session_id=sid, content="算了")

        self.assertFalse(await self._active_flow(repo, sid))
        self.assertIn("flow.aborted", await self._event_types(service, sid))

    async def test_flow_skill_without_assistant_repo_fails_loudly(self):
        tool = LoadSkillTool(loaded_skill_repository=_FakeLoadedSkillRepo())
        with patch("doyoutrade.tools.load_skills", return_value=[_flow_skill()]):
            result = await tool.execute(skill_name="demo-flow", session_id="asst-x")
        self.assertTrue(result.is_error)
        self.assertIn("flow_runtime_unwired", result.text)

    async def test_flow_skill_with_bad_mermaid_fails_loudly(self):
        broken = _flow_skill()
        broken.body = "```mermaid\nA[BEGIN] --> B[task]\n```"  # no END
        repo = InMemoryAssistantRepository()
        tool = LoadSkillTool(
            loaded_skill_repository=_FakeLoadedSkillRepo(),
            assistant_repository=repo,
        )
        with patch("doyoutrade.tools.load_skills", return_value=[broken]):
            result = await tool.execute(skill_name="demo-flow", session_id="asst-x")
        self.assertTrue(result.is_error)
        self.assertIn("flow_parse_error", result.text)

    async def test_reloading_flow_skill_restarts_at_entry(self):
        adapter = ScriptedModelAdapter(
            [
                call_tool("load_skill", {"skill_name": "demo-flow"}),
                say("开始"),
                say("收集完成 <choice>next</choice>"),
                call_tool("load_skill", {"skill_name": "demo-flow"}),
                say("重新开始"),
            ]
        )
        service, repo = self._build(adapter)
        session = await service.create_session(agent_id="test-agent", title="restart")
        sid = session["session_id"]

        with patch("doyoutrade.tools.load_skills", return_value=[_flow_skill()]):
            await service.send_message(session_id=sid, content="开始")
            await service.send_message(session_id=sid, content="继续")
            state = await self._active_flow(repo, sid)
            self.assertEqual(state["node_id"], "C")
            await service.send_message(session_id=sid, content="重来")

        state = await self._active_flow(repo, sid)
        self.assertEqual(state["node_id"], "B", "reload must restart at the entry node")


if __name__ == "__main__":
    unittest.main()
