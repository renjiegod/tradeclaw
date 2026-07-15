"""SwarmStore SQLAlchemy 持久化测试（CRUD + 事件 after_id 分页）。"""

from __future__ import annotations

import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from doyoutrade.persistence.db import Base
from doyoutrade.swarm.models import RunStatus, SwarmEvent, TaskStatus
from doyoutrade.swarm.presets import build_run_from_preset
from doyoutrade.swarm.store import SwarmStore


def _event(etype: str, ts: str, **data) -> SwarmEvent:
    return SwarmEvent(type=etype, timestamp=ts, data=data)


class SwarmStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite://")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sf = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SwarmStore(self.sf)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_create_get_update_run(self) -> None:
        run = build_run_from_preset("investment_committee", {"target": "AAPL", "market": "US"})
        run.status = RunStatus.running
        await self.store.create_run(run)

        loaded = await self.store.get_run(run.id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.preset_name, "investment_committee")
        self.assertEqual(len(loaded.tasks), 4)

        # 更新一个任务状态
        task = run.tasks[0]
        task.status = TaskStatus.completed
        task.summary = "done"
        task.session_id = "asst-xyz"
        await self.store.update_task(run.id, task)

        # 更新 run 级
        run.status = RunStatus.completed
        run.final_report = "报告正文"
        run.total_input_tokens = 100
        run.completed_at = "2026-06-20T00:00:00+00:00"
        await self.store.update_run(run)

        loaded = await self.store.get_run(run.id)
        self.assertEqual(loaded.status, RunStatus.completed)
        self.assertEqual(loaded.final_report, "报告正文")
        self.assertEqual(loaded.total_input_tokens, 100)
        done = {t.id: t for t in loaded.tasks}[task.id]
        self.assertEqual(done.status, TaskStatus.completed)
        self.assertEqual(done.session_id, "asst-xyz")

    async def test_event_pagination_after_id(self) -> None:
        run = build_run_from_preset("quant_strategy_desk", {"universe": "x", "horizon": "1w"})
        await self.store.create_run(run)
        first = await self.store.append_event(run.id, _event("run_started", "2026-06-20T00:00:00+00:00"))
        await self.store.append_event(run.id, _event("task_started", "2026-06-20T00:00:01+00:00", task_id="task-design"))

        # after_id=first → 只返回其后的事件
        rows = await self.store.list_events(run.id, after_id=first["event_id"], limit=50)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "task_started")

        # 无 after_id → 全量
        rows = await self.store.list_events(run.id, after_id=None, limit=50)
        self.assertEqual([r["event_type"] for r in rows], ["run_started", "task_started"])

    async def test_list_runs(self) -> None:
        for _ in range(2):
            run = build_run_from_preset("investment_committee", {"target": "A", "market": "US"})
            await self.store.create_run(run)
        runs = await self.store.list_runs(limit=10)
        self.assertEqual(len(runs), 2)


if __name__ == "__main__":
    unittest.main()
