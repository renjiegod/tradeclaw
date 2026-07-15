"""SwarmOrchestrator 编排测试：用 fake AssistantService 跑通真实 preset DAG。

验证：层间串行 + 层内并发、DAG gating（上游失败 → 下游 blocked）、token 累加、
final_report 取自末层、run 终态正确。
"""

from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from doyoutrade.assistant.repository import (
    InMemoryAgentRepository,
    InMemoryAssistantRepository,
)
from doyoutrade.persistence.db import Base
from doyoutrade.swarm.models import RunStatus
from doyoutrade.swarm.orchestrator import SwarmOrchestrator
from doyoutrade.swarm.store import SwarmStore


class _FakeService:
    """模拟 AssistantService：create_session 走 session repo，send_message 产 canned 回复。"""

    def __init__(self, sessions, agents, fail_agents=()):
        self._sessions = sessions
        self._agents = agents
        self.agent_repo = agents
        self._fail = set(fail_agents)

    async def create_session(self, *, agent_id, title, **kw):
        return await self._sessions.create_session(agent_id=agent_id, title=title)

    async def send_message(self, *, session_id, content, **kw):
        sess = await self._sessions.get_session(session_id)
        agent = await self._agents.get_agent(sess["agent_id"])
        if any(f in agent["id"] for f in self._fail):
            raise RuntimeError("boom from " + agent["id"])
        text = f"[{agent['id']}] 结论（输入 {len(content)} 字）"
        return {
            "session": sess,
            "messages": [
                {"role": "user", "content": content, "metadata": {}},
                {
                    "role": "assistant",
                    "content": text,
                    "metadata": {
                        "trace": {"usage": {"input_tokens": 10, "output_tokens": 5}},
                        "tool_calls": [1, 2],
                    },
                },
            ],
            "trace_id": None,
        }


class SwarmOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def _make(self, fail_agents=()):
        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sf = async_sessionmaker(engine, expire_on_commit=False)
        store = SwarmStore(sf)
        sessions = InMemoryAssistantRepository()
        agents = InMemoryAgentRepository()
        svc = _FakeService(sessions, agents, fail_agents)
        orch = SwarmOrchestrator(store, svc, agents, max_workers=4)
        return engine, store, orch

    async def _run_to_completion(self, orch, preset, user_vars):
        run = await orch.start_run(preset, user_vars)
        task = orch._tasks.get(run.id)
        if task is not None:
            await task
        return run.id

    async def test_happy_path_completes_with_report_and_tokens(self) -> None:
        engine, store, orch = await self._make()
        try:
            run_id = await self._run_to_completion(
                orch, "investment_committee", {"target": "AAPL", "market": "US"}
            )
            final = await store.get_run(run_id)
            self.assertEqual(final.status, RunStatus.completed)
            self.assertTrue(all(t.status.value == "completed" for t in final.tasks))
            # 4 worker × (10 in / 5 out)
            self.assertEqual(final.total_input_tokens, 40)
            self.assertEqual(final.total_output_tokens, 20)
            self.assertTrue(final.final_report)
            # final_report 取自末层 PM 任务
            self.assertIn("portfolio_manager", final.final_report)
        finally:
            await engine.dispose()

    async def test_upstream_failure_blocks_downstream(self) -> None:
        engine, store, orch = await self._make(fail_agents=("bull_advocate",))
        try:
            run_id = await self._run_to_completion(
                orch, "investment_committee", {"target": "AAPL", "market": "US"}
            )
            final = await store.get_run(run_id)
            st = {t.id: t.status.value for t in final.tasks}
            self.assertEqual(final.status, RunStatus.failed)
            self.assertEqual(st["task-bull"], "failed")
            self.assertEqual(st["task-bear"], "completed")
            # risk 依赖 bull+bear，bull 失败 → blocked；decision 依赖 risk → blocked
            self.assertEqual(st["task-risk"], "blocked")
            self.assertEqual(st["task-decision"], "blocked")
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
