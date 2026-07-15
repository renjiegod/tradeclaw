"""Tests for :class:`SqlAlchemyAssistantLoadedSkillRepository`.

Covers the persistence-layer contract that lets the assistant service
rebuild a ``<system-reminder>`` of loaded SKILL.md bodies after context
compaction. The repo is intentionally minimal — upsert / list / clear —
so the tests pin down:

* the (session_id, skill_name) composite-PK upsert semantics
* per-session isolation + newest-first ordering
* FK CASCADE from ``assistant_sessions`` (so deleting a session reaps the rows)
* hard rejection of empty session_id / skill_name (CLAUDE.md error-visibility
  forbids silent coercion of schema violations)
* metadata_json round-trip (the LoadSkillTool / reminder builder will lean on
  this for "where did this skill come from").
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

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
from doyoutrade.persistence.repositories import (
    SqlAlchemyAssistantLoadedSkillRepository,
)


_SKILL_BODY_A = "# strategy-definition-authoring\n\nbody A " + "x" * 32
_SKILL_BODY_B = "# strategy-iteration\n\nbody B " + "y" * 32


class AssistantLoadedSkillRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        # SQLite ignores FOREIGN KEY clauses unless ``PRAGMA foreign_keys=ON``
        # is set per connection. NullPool means each session opens a fresh
        # connection, so the listener fires for every new session.
        sync_engine = self.engine.sync_engine

        def _fk_on(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.close()

        event.listen(sync_engine, "connect", _fk_on)

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        # AssistantSessionRecord requires an agent (FK to agents.id). Seed
        # the rows the loaded-skill tests need to attach to.
        async with self.session_factory() as session:
            session.add(AgentRecord(id="a1", name="agent", system_prompt=""))
            await session.commit()
            session.add(
                AssistantSessionRecord(session_id="sess-a", agent_id="a1")
            )
            session.add(
                AssistantSessionRecord(session_id="sess-b", agent_id="a1")
            )
            await session.commit()

        self.repo = SqlAlchemyAssistantLoadedSkillRepository(self.session_factory)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_upsert_inserts_new_row(self) -> None:
        await self.repo.upsert(
            session_id="sess-a",
            skill_name="strategy-definition-authoring",
            skill_path="/skills/strategy-definition-authoring/SKILL.md",
            body=_SKILL_BODY_A,
            body_hash="hash-a-1",
            metadata={"source": "load_skill_tool"},
        )
        rows = await self.repo.list_by_session("sess-a")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["session_id"], "sess-a")
        self.assertEqual(row["skill_name"], "strategy-definition-authoring")
        self.assertEqual(
            row["skill_path"], "/skills/strategy-definition-authoring/SKILL.md"
        )
        self.assertEqual(row["body"], _SKILL_BODY_A)
        self.assertEqual(row["body_hash"], "hash-a-1")
        self.assertEqual(row["byte_size"], len(_SKILL_BODY_A.encode("utf-8")))
        self.assertEqual(row["metadata_json"], {"source": "load_skill_tool"})
        # loaded_at must serialize to ISO, non-empty.
        self.assertIsInstance(row["loaded_at"], str)
        self.assertGreater(len(row["loaded_at"]), 0)

    async def test_upsert_overwrites_existing_row(self) -> None:
        await self.repo.upsert(
            session_id="sess-a",
            skill_name="strategy-iteration",
            skill_path="/skills/strategy-iteration/SKILL.md",
            body="v1 body",
            body_hash="hash-v1",
        )
        first_rows = await self.repo.list_by_session("sess-a")
        first_loaded_at = first_rows[0]["loaded_at"]

        # Ensure the wall-clock advances enough for loaded_at to differ.
        # asyncio.sleep(0) is too tight on fast machines; a tiny real sleep
        # is safe inside an async test.
        await asyncio.sleep(0.01)

        await self.repo.upsert(
            session_id="sess-a",
            skill_name="strategy-iteration",
            skill_path="/skills/strategy-iteration/SKILL_v2.md",
            body="v2 body has different length",
            body_hash="hash-v2",
            metadata={"version": 2},
        )
        rows = await self.repo.list_by_session("sess-a")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["body"], "v2 body has different length")
        self.assertEqual(row["body_hash"], "hash-v2")
        self.assertEqual(
            row["byte_size"], len("v2 body has different length".encode("utf-8"))
        )
        self.assertEqual(row["skill_path"], "/skills/strategy-iteration/SKILL_v2.md")
        self.assertEqual(row["metadata_json"], {"version": 2})
        self.assertGreaterEqual(row["loaded_at"], first_loaded_at)

    async def test_list_by_session_orders_newest_first(self) -> None:
        for name in ("skill-1", "skill-2", "skill-3"):
            await self.repo.upsert(
                session_id="sess-a",
                skill_name=name,
                skill_path=f"/skills/{name}/SKILL.md",
                body=f"body of {name}",
                body_hash=f"hash-{name}",
            )
            # Tiny delay so loaded_at strictly increases between rows.
            await asyncio.sleep(0.005)
        rows = await self.repo.list_by_session("sess-a")
        self.assertEqual([r["skill_name"] for r in rows], ["skill-3", "skill-2", "skill-1"])

    async def test_list_by_session_isolates_sessions(self) -> None:
        await self.repo.upsert(
            session_id="sess-a",
            skill_name="skill-a",
            skill_path="/skills/skill-a/SKILL.md",
            body=_SKILL_BODY_A,
            body_hash="hash-a",
        )
        await self.repo.upsert(
            session_id="sess-b",
            skill_name="skill-b",
            skill_path="/skills/skill-b/SKILL.md",
            body=_SKILL_BODY_B,
            body_hash="hash-b",
        )
        rows_a = await self.repo.list_by_session("sess-a")
        rows_b = await self.repo.list_by_session("sess-b")
        self.assertEqual([r["skill_name"] for r in rows_a], ["skill-a"])
        self.assertEqual([r["skill_name"] for r in rows_b], ["skill-b"])

    async def test_clear_for_session_removes_all_rows(self) -> None:
        await self.repo.upsert(
            session_id="sess-a",
            skill_name="skill-1",
            skill_path="/x/SKILL.md",
            body="b1",
            body_hash="h1",
        )
        await self.repo.upsert(
            session_id="sess-a",
            skill_name="skill-2",
            skill_path="/y/SKILL.md",
            body="b2",
            body_hash="h2",
        )
        count = await self.repo.clear_for_session("sess-a")
        self.assertEqual(count, 2)
        self.assertEqual(await self.repo.list_by_session("sess-a"), [])

    async def test_cascade_delete_on_session_deletion(self) -> None:
        """Deleting the parent AssistantSessionRecord cascades to its loaded skills."""
        await self.repo.upsert(
            session_id="sess-a",
            skill_name="skill-1",
            skill_path="/x/SKILL.md",
            body="b1",
            body_hash="h1",
        )
        await self.repo.upsert(
            session_id="sess-a",
            skill_name="skill-2",
            skill_path="/y/SKILL.md",
            body="b2",
            body_hash="h2",
        )
        self.assertEqual(len(await self.repo.list_by_session("sess-a")), 2)

        async with self.session_factory() as session:
            row = await session.get(AssistantSessionRecord, "sess-a")
            assert row is not None
            await session.delete(row)
            await session.commit()

        self.assertEqual(await self.repo.list_by_session("sess-a"), [])

    async def test_upsert_rejects_empty_session_id(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            await self.repo.upsert(
                session_id="",
                skill_name="skill-1",
                skill_path="/x/SKILL.md",
                body="b",
                body_hash="h",
            )
        self.assertIn("session_id", str(ctx.exception))
        self.assertIn("''", str(ctx.exception))

    async def test_upsert_rejects_empty_skill_name(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            await self.repo.upsert(
                session_id="sess-a",
                skill_name="",
                skill_path="/x/SKILL.md",
                body="b",
                body_hash="h",
            )
        self.assertIn("skill_name", str(ctx.exception))
        self.assertIn("''", str(ctx.exception))

    async def test_upsert_records_metadata_json(self) -> None:
        await self.repo.upsert(
            session_id="sess-a",
            skill_name="skill-1",
            skill_path="/x/SKILL.md",
            body="body",
            body_hash="hash",
            metadata={"source": "load_skill_tool", "extra": 1, "nested": {"k": "v"}},
        )
        rows = await self.repo.list_by_session("sess-a")
        self.assertEqual(
            rows[0]["metadata_json"],
            {"source": "load_skill_tool", "extra": 1, "nested": {"k": "v"}},
        )

    async def test_upsert_defaults_metadata_to_empty_dict(self) -> None:
        await self.repo.upsert(
            session_id="sess-a",
            skill_name="skill-1",
            skill_path="/x/SKILL.md",
            body="body",
            body_hash="hash",
        )
        rows = await self.repo.list_by_session("sess-a")
        self.assertEqual(rows[0]["metadata_json"], {})


if __name__ == "__main__":
    unittest.main()
