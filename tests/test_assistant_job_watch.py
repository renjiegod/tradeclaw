"""Job-watch wake-up regression: watch_job registers, JobWatchService fires
on terminal status (compose in a worker session → deliver into the
originating session), and every failure mode resolves visibly."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

from doyoutrade.assistant import AssistantService, InMemoryAssistantRepository
from doyoutrade.assistant.job_watcher import JobWatchService
from doyoutrade.tools import OperationRegistry
from doyoutrade.tools.watch_job import WatchJobTool
from tests.scripted_model import ScriptedModelAdapter, call_tool, say


class _FakeWatchRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def create(self, *, session_id, agent_id, job_id, job_kind="backtest", note=None):
        row = {
            "watch_id": f"wjob-{uuid4().hex[:12]}",
            "session_id": session_id,
            "agent_id": agent_id,
            "job_kind": job_kind,
            "job_id": job_id,
            "note": note,
            "status": "pending",
            "last_error": None,
            "created_at": "2026-06-13T00:00:00",
            "fired_at": None,
        }
        self.rows[row["watch_id"]] = row
        return dict(row)

    async def list_pending(self, *, limit: int = 100):
        return [dict(r) for r in self.rows.values() if r["status"] == "pending"][:limit]

    async def resolve(self, watch_id, *, status, last_error=None):
        row = self.rows.get(watch_id)
        if row is None:
            return None
        row["status"] = status
        row["last_error"] = last_error
        return dict(row)


class _FakeRunRepo:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}

    async def get(self, run_id: str):
        run = self.runs.get(run_id)
        return dict(run) if run else None


class WatchJobToolTests(unittest.IsolatedAsyncioTestCase):
    def _tool(self, runs: dict[str, dict[str, Any]] | None = None):
        run_repo = _FakeRunRepo()
        run_repo.runs.update(runs or {})
        watch_repo = _FakeWatchRepo()
        return WatchJobTool(watch_repository=watch_repo, run_repository=run_repo), watch_repo

    async def test_creates_watch_for_running_job(self):
        tool, watch_repo = self._tool({"btjob-1": {"status": "running"}})
        result = await tool.execute(
            job_id="btjob-1", note="看夏普", session_id="sess-1", agent_id="agent-1"
        )
        self.assertFalse(bool(getattr(result, "is_error", False)))
        self.assertIn('"status":"created"', result.text)
        rows = await watch_repo.list_pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], "btjob-1")
        self.assertEqual(rows[0]["session_id"], "sess-1")
        self.assertEqual(rows[0]["note"], "看夏普")

    async def test_terminal_job_short_circuits(self):
        tool, watch_repo = self._tool({"btjob-1": {"status": "completed"}})
        result = await tool.execute(job_id="btjob-1", session_id="s", agent_id="a")
        self.assertFalse(bool(getattr(result, "is_error", False)))
        self.assertIn("already_terminal", result.text)
        self.assertEqual(await watch_repo.list_pending(), [])

    async def test_job_not_found(self):
        tool, _ = self._tool({})
        result = await tool.execute(job_id="btjob-miss", session_id="s", agent_id="a")
        self.assertTrue(result.is_error)
        self.assertIn("job_not_found", result.text)

    async def test_unwired_runtime_fails_loudly(self):
        tool = WatchJobTool()
        result = await tool.execute(job_id="btjob-1", session_id="s", agent_id="a")
        self.assertTrue(result.is_error)
        self.assertIn("watch_unwired", result.text)

    async def test_unknown_argument_rejected(self):
        tool, _ = self._tool({"btjob-1": {"status": "running"}})
        result = await tool.execute(
            job_id="btjob-1", jobid_typo="x", session_id="s", agent_id="a"
        )
        self.assertTrue(result.is_error)
        self.assertIn("unknown", result.text.lower())


class JobWatchServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._services: list[AssistantService] = []

    async def asyncTearDown(self) -> None:
        for service in self._services:
            await service.aclose()

    def _build(self, adapter: ScriptedModelAdapter):
        repo = InMemoryAssistantRepository()
        service = AssistantService(
            repo,
            model_adapter_factory=adapter.factory,
            tool_registry=OperationRegistry([]),
        )
        self._services.append(service)
        watch_repo = _FakeWatchRepo()
        run_repo = _FakeRunRepo()
        watcher = JobWatchService(
            watch_repository=watch_repo,
            run_repository=run_repo,
            assistant_service=service,
        )
        return service, repo, watch_repo, run_repo, watcher

    async def test_fire_composes_and_delivers_into_origin_session(self):
        adapter = ScriptedModelAdapter(
            [say("回测 btjob-9 已完成：年化 +12%，最大回撤 8%。建议保持参数。")]
        )
        service, repo, watch_repo, run_repo, watcher = self._build(adapter)
        origin = await service.create_session(agent_id="agent-1", title="origin")
        sid = origin["session_id"]
        watch = await watch_repo.create(
            session_id=sid, agent_id="agent-1", job_id="btjob-9", note="看回撤"
        )
        run_repo.runs["btjob-9"] = {"status": "running"}

        # Not terminal yet: nothing fires.
        self.assertEqual(await watcher.poll_once(), 0)
        self.assertEqual((await watch_repo.list_pending())[0]["status"], "pending")

        run_repo.runs["btjob-9"]["status"] = "completed"
        self.assertEqual(await watcher.poll_once(), 1)

        # Watch resolved.
        self.assertEqual(watch_repo.rows[watch["watch_id"]]["status"], "fired")
        # The composer saw the framing (job id + note).
        framing_seen = "\n".join(adapter.calls[0].message_texts())
        self.assertIn("btjob-9", framing_seen)
        self.assertIn("看回撤", framing_seen)
        self.assertIn("[SYSTEM: job-completed wake-up", framing_seen)
        # The reply landed on the ORIGIN session with job_watch metadata.
        messages = await service.list_messages(sid, limit=50, offset=0)
        delivered = [
            m
            for m in messages
            if m["role"] == "assistant"
            and (m.get("metadata") or {}).get("source") == "job_watch"
        ]
        self.assertEqual(len(delivered), 1)
        self.assertIn("年化 +12%", delivered[0]["content"])
        self.assertEqual(delivered[0]["metadata"]["job_id"], "btjob-9")
        events = await service.list_events(sid, limit=100)
        self.assertIn("job_watch.fired", [e["event_type"] for e in events])
        adapter.assert_exhausted()

    async def test_job_not_found_resolves_failed_with_event(self):
        adapter = ScriptedModelAdapter([])
        service, repo, watch_repo, run_repo, watcher = self._build(adapter)
        origin = await service.create_session(agent_id="agent-1", title="origin")
        sid = origin["session_id"]
        watch = await watch_repo.create(
            session_id=sid, agent_id="agent-1", job_id="btjob-gone"
        )

        self.assertEqual(await watcher.poll_once(), 1)
        row = watch_repo.rows[watch["watch_id"]]
        self.assertEqual(row["status"], "failed")
        self.assertIn("job_not_found", row["last_error"])
        events = await service.list_events(sid, limit=100)
        self.assertIn("job_watch.failed", [e["event_type"] for e in events])

    async def test_composer_failure_resolves_failed_not_silent(self):
        # Empty script → composer turn raises (script exhausted) → failed.
        adapter = ScriptedModelAdapter([])
        service, repo, watch_repo, run_repo, watcher = self._build(adapter)
        origin = await service.create_session(agent_id="agent-1", title="origin")
        watch = await watch_repo.create(
            session_id=origin["session_id"], agent_id="agent-1", job_id="btjob-2"
        )
        run_repo.runs["btjob-2"] = {"status": "failed"}

        self.assertEqual(await watcher.poll_once(), 1)
        row = watch_repo.rows[watch["watch_id"]]
        self.assertEqual(row["status"], "failed")
        self.assertTrue(row["last_error"])
        events = await service.list_events(origin["session_id"], limit=100)
        self.assertIn("job_watch.failed", [e["event_type"] for e in events])


class JobWatchSqlRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from sqlalchemy import event

        from doyoutrade.persistence.db import (
            create_engine_and_session_factory,
            dispose_engine,
        )
        from doyoutrade.persistence.models import (
            AgentRecord,
            AssistantSessionRecord,
            Base,
        )

        self._dispose_engine = dispose_engine
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        sync_engine = self.engine.sync_engine

        def _fk_on(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

        event.listen(sync_engine, "connect", _fk_on)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.session_factory() as session:
            session.add(AgentRecord(id="a1", name="agent", system_prompt=""))
            await session.commit()
            session.add(AssistantSessionRecord(session_id="sess-a", agent_id="a1"))
            await session.commit()

        from doyoutrade.persistence.job_watches import (
            SqlAlchemyAssistantJobWatchRepository,
        )

        self.repo = SqlAlchemyAssistantJobWatchRepository(self.session_factory)

    async def asyncTearDown(self) -> None:
        await self._dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_create_list_resolve_roundtrip(self):
        created = await self.repo.create(
            session_id="sess-a", agent_id="a1", job_id="btjob-1", note="n"
        )
        self.assertTrue(created["watch_id"].startswith("wjob-"))
        pending = await self.repo.list_pending()
        self.assertEqual([r["watch_id"] for r in pending], [created["watch_id"]])

        fired = await self.repo.resolve(created["watch_id"], status="fired")
        self.assertEqual(fired["status"], "fired")
        self.assertIsNotNone(fired["fired_at"])
        self.assertEqual(await self.repo.list_pending(), [])

        rows = await self.repo.list_for_session("sess-a")
        self.assertEqual(len(rows), 1)

    async def test_resolve_failed_keeps_error_and_rejects_bad_status(self):
        created = await self.repo.create(
            session_id="sess-a", agent_id="a1", job_id="btjob-2"
        )
        failed = await self.repo.resolve(
            created["watch_id"], status="failed", last_error="boom"
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["last_error"], "boom")
        with self.assertRaises(ValueError):
            await self.repo.resolve(created["watch_id"], status="done")
        self.assertIsNone(await self.repo.resolve("wjob-missing", status="fired"))


if __name__ == "__main__":
    unittest.main()
