import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from doyoutrade.persistence.db import (
    create_engine_and_session_factory,
    dispose_engine,
)
from doyoutrade.persistence.models import AgentRecord, Base, CronJobRecord
from doyoutrade.persistence.repositories import (
    SqlAlchemyCronJobRepository,
    SqlAlchemyCronJobRunRepository,
)


class CronJobRunRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # CronJobRecord.agent_id has an FK to AgentRecord — seed an agent and the
        # parent cron job row directly so this test focuses on the run repository.
        async with self.session_factory() as session:
            session.add(AgentRecord(id="a1", name="agent", system_prompt=""))
            await session.commit()
            session.add(
                CronJobRecord(
                    id="c1",
                    agent_id="a1",
                    name="n",
                    cron_expression="* * * * *",
                    input_template="t",
                )
            )
            await session.commit()

        self.cron_repo = SqlAlchemyCronJobRepository(self.session_factory)
        self.run_repo = SqlAlchemyCronJobRunRepository(self.session_factory)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_create_and_get_run(self):
        now = datetime.utcnow()
        row = await self.run_repo.create_run(
            {
                "id": "r1",
                "job_id": "c1",
                "fired_at": now,
                "started_at": now,
                "status": "running",
            }
        )
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["job_id"], "c1")
        self.assertEqual(row["id"], "r1")
        self.assertIsNone(row["finished_at"])

        got = await self.run_repo.get_run("r1")
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["job_id"], "c1")
        self.assertEqual(got["status"], "running")

    async def test_get_run_missing_returns_none(self):
        self.assertIsNone(await self.run_repo.get_run("does-not-exist"))

    async def test_update_run_with_whitelisted_fields(self):
        now = datetime.utcnow()
        await self.run_repo.create_run(
            {
                "id": "r1",
                "job_id": "c1",
                "fired_at": now,
                "started_at": now,
                "status": "running",
            }
        )
        finished_at = datetime.utcnow()
        result = await self.run_repo.update_run(
            "r1",
            {
                "status": "success",
                "pre_status": "ok",
                "pre_run_id": "run-xyz",
                "pre_result_json": {"fills": []},
                "agent_session_id": "sess-1",
                "finished_at": finished_at,
                # Non-whitelisted updates must be silently ignored:
                "job_id": "tampered",
                "id": "tampered",
                "created_at": datetime.utcnow(),
            },
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["pre_run_id"], "run-xyz")
        self.assertEqual(result["pre_result_json"], {"fills": []})
        self.assertEqual(result["agent_session_id"], "sess-1")
        self.assertIsNotNone(result["finished_at"])

        # Confirm tamper-resistance via fresh read:
        got = await self.run_repo.get_run("r1")
        assert got is not None
        self.assertEqual(got["job_id"], "c1")
        self.assertEqual(got["id"], "r1")
        self.assertEqual(got["status"], "success")
        self.assertEqual(got["pre_run_id"], "run-xyz")
        self.assertEqual(got["pre_result_json"], {"fills": []})

    async def test_update_run_unknown_id_returns_none(self):
        result = await self.run_repo.update_run(
            "does-not-exist", {"status": "success"}
        )
        self.assertIsNone(result)

    async def test_list_for_job_returns_most_recent_first(self):
        base = datetime.utcnow()
        for i in range(3):
            await self.run_repo.create_run(
                {
                    "id": f"r{i}",
                    "job_id": "c1",
                    "fired_at": base - timedelta(seconds=i),
                    "started_at": base - timedelta(seconds=i),
                    "status": "running",
                }
            )
        rows = await self.run_repo.list_for_job("c1", limit=10)
        self.assertEqual(len(rows), 3)
        # r0 has the most recent fired_at (base - 0s); r2 the oldest.
        self.assertEqual(rows[0]["id"], "r0")
        self.assertEqual(rows[-1]["id"], "r2")

    async def test_list_for_job_respects_limit(self):
        base = datetime.utcnow()
        for i in range(5):
            await self.run_repo.create_run(
                {
                    "id": f"r{i}",
                    "job_id": "c1",
                    "fired_at": base - timedelta(seconds=i),
                    "started_at": base - timedelta(seconds=i),
                    "status": "running",
                }
            )
        rows = await self.run_repo.list_for_job("c1", limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "r0")
        self.assertEqual(rows[1]["id"], "r1")

    async def test_list_for_job_respects_before_fired_at(self):
        base = datetime.utcnow()
        for i in range(3):
            await self.run_repo.create_run(
                {
                    "id": f"r{i}",
                    "job_id": "c1",
                    "fired_at": base - timedelta(seconds=i),
                    "started_at": base - timedelta(seconds=i),
                    "status": "running",
                }
            )
        # Cutoff between r0 (fired_at = base) and r1 (fired_at = base - 1s)
        rows = await self.run_repo.list_for_job(
            "c1", before_fired_at=base - timedelta(milliseconds=500)
        )
        self.assertEqual({r["id"] for r in rows}, {"r1", "r2"})

    async def test_list_for_job_filters_by_job_id(self):
        # A second job, also under agent a1.
        async with self.session_factory() as session:
            session.add(
                CronJobRecord(
                    id="c2",
                    agent_id="a1",
                    name="n2",
                    cron_expression="* * * * *",
                    input_template="t",
                )
            )
            await session.commit()
        now = datetime.utcnow()
        await self.run_repo.create_run(
            {"id": "r1", "job_id": "c1", "fired_at": now, "started_at": now, "status": "running"}
        )
        await self.run_repo.create_run(
            {"id": "r2", "job_id": "c2", "fired_at": now, "started_at": now, "status": "running"}
        )
        rows_c1 = await self.run_repo.list_for_job("c1")
        rows_c2 = await self.run_repo.list_for_job("c2")
        self.assertEqual([r["id"] for r in rows_c1], ["r1"])
        self.assertEqual([r["id"] for r in rows_c2], ["r2"])

    async def test_create_run_accepts_aware_utc_datetime(self):
        """Regression: cron_manager emits ``datetime.now(timezone.utc)`` —
        an aware datetime. The cron_job_runs columns are
        ``TIMESTAMP WITHOUT TIME ZONE``; asyncpg refuses aware values
        ("can't subtract offset-naive and offset-aware datetimes"). The
        repo must strip tz at the boundary."""
        aware = datetime.now(timezone.utc)
        row = await self.run_repo.create_run(
            {
                "id": "r-aware",
                "job_id": "c1",
                "fired_at": aware,
                "started_at": aware,
                "status": "running",
            }
        )
        self.assertEqual(row["status"], "running")
        # Re-read so we see what actually landed in storage. The
        # serializer emits ISO strings without offset because the column
        # is naive UTC.
        got = await self.run_repo.get_run("r-aware")
        assert got is not None
        # No "+00:00" suffix — proves tz was stripped before write.
        self.assertNotIn("+00:00", got["fired_at"])
        self.assertNotIn("+00:00", got["started_at"])

    async def test_update_run_accepts_aware_finished_at(self):
        now = datetime.utcnow()
        await self.run_repo.create_run(
            {
                "id": "r-upd",
                "job_id": "c1",
                "fired_at": now,
                "started_at": now,
                "status": "running",
            }
        )
        aware_finish = datetime.now(timezone.utc)
        result = await self.run_repo.update_run(
            "r-upd",
            {"status": "success", "finished_at": aware_finish},
        )
        assert result is not None
        self.assertEqual(result["status"], "success")
        self.assertNotIn("+00:00", result["finished_at"] or "")

    async def test_update_job_state_accepts_aware_last_run_at(self):
        """Same regression for ``cron_jobs.last_run_at`` — that's the
        other naive-DateTime column ``cron_manager._execute`` writes
        through, also using aware UTC."""
        aware = datetime.now(timezone.utc)
        await self.cron_repo.update_job_state(
            "c1",
            last_run_at=aware,
            last_status="running",
        )
        # Read it back via get_job to confirm the row was written.
        got = await self.cron_repo.get_job("c1")
        assert got is not None
        self.assertEqual(got["last_status"], "running")


if __name__ == "__main__":
    unittest.main()
