"""End-to-end coverage for the loaded-skills compaction-resilient flow.

This file pins the cross-process / cross-compaction behaviour added in
plan ``mellow-waddling-lovelace.md`` (C-T1..T5):

* ``LoadSkillTool`` persists SKILL.md bodies to ``assistant_loaded_skills``
  via the real ``SqlAlchemyAssistantLoadedSkillRepository`` running on the
  isolated SQLite + Alembic migrations stack — *not* a unit mock.
* ``AssistantService._inject_loaded_skills_reminder`` only injects when a
  ``summary_boundary`` row is present in the assistant message history.
* Cross-process persistence: after dropping the original repository
  instance and rebuilding a new repo against the same ``session_factory``,
  the reminder can still be reconstituted from the DB. This is the key
  property the plan §持久化考虑 calls out — a server restart cannot lose
  loaded-skill state for a live session.
* FK CASCADE: dropping the parent ``AssistantSessionRecord`` reaps its
  loaded-skill rows. Mirrors the unit-level pin in
  ``tests/test_assistant_loaded_skills_repository.py`` but inside the real
  bootstrap'd runtime, so the migration produced by C-T1 must declare the
  FK with ``ondelete=CASCADE``.
* OTel + debug-event run_id threading: a successful ``LoadSkillTool``
  invocation emits the ``operation_load_skill.persisted`` debug event with
  the persisted byte_size / body_hash so trace consumers can correlate
  the row in ``assistant_loaded_skills`` back to a model-invocation run.

We deliberately do NOT drive a real LLM-mediated load_skill round-trip:
the StubModelAdapter in ``tests/e2e/support.py`` returns a fixed JSON
that does not call ``load_skill``. Driving the tool directly is honest
about what we are testing (persistence + injection routing) and avoids
coupling this test to the stub's response shape.
"""

from __future__ import annotations

import unittest
import uuid
from typing import Any

from unittest.mock import patch

from sqlalchemy import event, delete

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
)
from doyoutrade.assistant.service import _inject_loaded_skills_reminder
from doyoutrade.persistence.models import AssistantSessionRecord
from doyoutrade.persistence.repositories import (
    SqlAlchemyAssistantLoadedSkillRepository,
)
from doyoutrade.tools import LoadSkillTool


_REAL_SKILL_NAME = "doyoutrade-data"
# Distinctive substring from the YAML frontmatter of
# .doyoutrade/skills/doyoutrade-data/SKILL.md — we only need a fragment we
# can grep, not the whole body. Picking a phrase that is unlikely to
# appear in unrelated reminder scaffolding.
_REAL_SKILL_BODY_FRAGMENT = "OHLCV"


def _summary_boundary_metadata(
    *, compacted_until_message_id: str = "msg-older", source_message_count: int = 5
) -> dict[str, Any]:
    """Mirror doyoutrade/assistant/context_compaction/full.py."""
    return {
        "context_compaction": {
            "kind": "summary_boundary",
            "strategy": "full",
            "compacted_until_message_id": compacted_until_message_id,
            "source_message_count": int(source_message_count),
        }
    }


def _install_sqlite_fk_listener(engine) -> None:
    """Turn on ``PRAGMA foreign_keys = ON`` for every new SQLite connection.

    SQLite ignores ``ondelete=CASCADE`` without this PRAGMA. The runtime
    DB used by the isolated profile is fresh SQLite, so we enable FKs
    here only for the cascade test.
    """
    sync_engine = engine.sync_engine

    def _fk_on(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()

    event.listen(sync_engine, "connect", _fk_on)


@unittest.skipUnless(e2e_enabled(), "set DOYOUTRADE_E2E=1 to run end-to-end tests")
class AssistantLoadedSkillsLifecycleE2ETests(unittest.IsolatedAsyncioTestCase):
    async def _make_agent_and_session(self, ctx) -> tuple[dict[str, Any], dict[str, Any]]:
        assistant_service = ctx.runtime["assistant_service"]
        agent = await assistant_service.agent_repo.create_agent(
            {
                "name": f"E2E LoadedSkill Agent {uuid.uuid4().hex[:6]}",
                "system_prompt": "You are validating loaded-skill persistence.",
                "status": "active",
                "model_route_name": ctx.model_route_name,
                "tool_names": [],
                "tool_configs": [],
            }
        )
        session = await assistant_service.create_session(
            agent_id=agent["id"],
            title="E2E loaded-skill lifecycle",
        )
        return agent, session

    # ------------------------------------------------------------------
    # Scenario 1 — LoadSkillTool persists a real SKILL.md row to the DB.
    # ------------------------------------------------------------------
    async def test_load_skill_persists_to_db(self) -> None:
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            _, session = await self._make_agent_and_session(ctx)
            session_id = session["session_id"]
            repo: SqlAlchemyAssistantLoadedSkillRepository = ctx.runtime[
                "assistant_loaded_skill_repository"
            ]

            tool = LoadSkillTool(loaded_skill_repository=repo)
            result = await tool.execute(
                skill_name=_REAL_SKILL_NAME,
                session_id=session_id,
            )

            # Tool itself succeeded and routed back the SKILL body to the
            # current turn.
            self.assertFalse(result.is_error, result.text)
            self.assertIn(_REAL_SKILL_NAME, result.text)
            self.assertIn(
                "[Loaded and persisted; will be re-injected",
                result.text,
                "missing compaction-survival tail note — persistence row "
                "did not get written",
            )

            rows = await repo.list_by_session(session_id)
            self.assertEqual(len(rows), 1, rows)
            row = rows[0]
            self.assertEqual(row["session_id"], session_id)
            self.assertEqual(row["skill_name"], _REAL_SKILL_NAME)
            self.assertIn(_REAL_SKILL_BODY_FRAGMENT, row["body"])
            # body_hash is a sha256 hex digest — 64 chars.
            self.assertEqual(len(row["body_hash"]), 64, row)
            self.assertGreater(row["byte_size"], 0, row)
            self.assertEqual(row["byte_size"], len(row["body"].encode("utf-8")))
            self.assertEqual(
                row["metadata_json"].get("source"), "load_skill_tool", row
            )
            self.assertIsInstance(row["loaded_at"], str)
            self.assertGreater(len(row["loaded_at"]), 0)

    # ------------------------------------------------------------------
    # Scenario 2 — summary_boundary in history triggers reminder injection.
    # ------------------------------------------------------------------
    async def test_compaction_boundary_triggers_reminder_injection(self) -> None:
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            _, session = await self._make_agent_and_session(ctx)
            session_id = session["session_id"]
            repo: SqlAlchemyAssistantLoadedSkillRepository = ctx.runtime[
                "assistant_loaded_skill_repository"
            ]
            assistant_service = ctx.runtime["assistant_service"]

            # Pretend ``load_skill`` happened previously in this session.
            await repo.upsert(
                session_id=session_id,
                skill_name="strategy-iteration",
                skill_path=".doyoutrade/skills/strategy-iteration/SKILL.md",
                body="# strategy-iteration\nThis is a previously loaded body marker XYZQ.",
                body_hash="a" * 64,
                metadata={"source": "load_skill_tool"},
            )

            # Pretend the session went through compaction.
            await assistant_service.repository.append_message(
                session_id=session_id,
                role="user",
                content="old user prompt before compaction",
                linked_attempt_id=None,
                metadata={},
            )
            await assistant_service.repository.append_message(
                session_id=session_id,
                role="system",
                content="<compaction summary>",
                linked_attempt_id=None,
                metadata=_summary_boundary_metadata(
                    compacted_until_message_id="msg-old", source_message_count=4
                ),
            )
            await assistant_service.repository.append_message(
                session_id=session_id,
                role="user",
                content="tail user message after compaction",
                linked_attempt_id=None,
                metadata={},
            )

            history_rows = await assistant_service.repository.list_messages(
                session_id, limit=100, offset=0
            )
            # Build the kind of history_messages list the service feeds the
            # injector — it uses opaque sentinel strings here because the
            # injector only mutates the list shape.
            history_messages = ["msg-old", "msg-boundary", "msg-tail"]
            self.assertEqual(len(history_messages), len(history_rows))

            result = await _inject_loaded_skills_reminder(
                history_messages,
                session_id=session_id,
                history_rows=history_rows,
                loaded_skill_repository=repo,
            )

            self.assertEqual(len(result), len(history_messages) + 1)
            self.assertEqual(result[0], history_messages[0])
            self.assertEqual(result[-1], "msg-tail")  # tail preserved
            reminder = result[-2]
            content = getattr(reminder, "content", None)
            # `assert isinstance` (vs self.assertIsInstance) narrows for the
            # subsequent assertIn calls — unittest's assertion does not.
            assert isinstance(content, str), (
                f"reminder.content must be str, got {type(content).__name__}"
            )
            self.assertIn("<system-reminder>", content)
            self.assertIn("strategy-iteration", content)
            self.assertIn("previously loaded body marker XYZQ", content)
            self.assertIn("You do not need to call `load_skill` again", content)

    # ------------------------------------------------------------------
    # Scenario 3 — no boundary → no reminder, no DB roundtrip.
    # ------------------------------------------------------------------
    async def test_no_boundary_no_reminder_injection(self) -> None:
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            _, session = await self._make_agent_and_session(ctx)
            session_id = session["session_id"]
            repo: SqlAlchemyAssistantLoadedSkillRepository = ctx.runtime[
                "assistant_loaded_skill_repository"
            ]
            assistant_service = ctx.runtime["assistant_service"]

            await repo.upsert(
                session_id=session_id,
                skill_name="doyoutrade-data",
                skill_path=".doyoutrade/skills/doyoutrade-data/SKILL.md",
                body="ignored body content",
                body_hash="b" * 64,
                metadata={"source": "load_skill_tool"},
            )

            # Two normal messages, neither carries a summary_boundary
            # marker — i.e. compaction has not happened yet, so the
            # original tool_result is presumed to still be in history.
            await assistant_service.repository.append_message(
                session_id=session_id,
                role="user",
                content="just a user prompt",
                linked_attempt_id=None,
                metadata={},
            )
            await assistant_service.repository.append_message(
                session_id=session_id,
                role="assistant",
                content="just an assistant reply",
                linked_attempt_id=None,
                metadata={},
            )

            history_rows = await assistant_service.repository.list_messages(
                session_id, limit=100, offset=0
            )
            self.assertFalse(
                any(
                    (row.get("metadata") or {}).get("context_compaction", {}).get("kind")
                    == "summary_boundary"
                    for row in history_rows
                ),
                "fixture should have no boundary",
            )

            # Wrap the repo so we can spy on list_by_session — it must
            # NOT be called when there's no boundary, otherwise we
            # waste DB roundtrips on every pre-compaction turn.
            calls: list[str] = []
            real_list_by_session = repo.list_by_session

            async def _spy(session_id_arg: str) -> list[dict[str, Any]]:
                calls.append(session_id_arg)
                return await real_list_by_session(session_id_arg)

            repo.list_by_session = _spy  # type: ignore[assignment]
            try:
                history_messages = ["msg-1", "msg-2"]
                result = await _inject_loaded_skills_reminder(
                    history_messages,
                    session_id=session_id,
                    history_rows=history_rows,
                    loaded_skill_repository=repo,
                )
            finally:
                repo.list_by_session = real_list_by_session  # type: ignore[assignment]

            self.assertEqual(result, history_messages)
            self.assertEqual(
                calls, [],
                "repository.list_by_session must not be called without a "
                "summary_boundary marker",
            )

    # ------------------------------------------------------------------
    # Scenario 4 — cross-process persistence (the plan §持久化考虑 core
    # scenario): the SKILL body survives a fresh repo instance built
    # against the same DB, simulating server restart.
    # ------------------------------------------------------------------
    async def test_cross_process_persistence_simulation(self) -> None:
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            _, session = await self._make_agent_and_session(ctx)
            session_id = session["session_id"]
            session_factory = ctx.runtime["session_factory"]
            assistant_service = ctx.runtime["assistant_service"]

            # "Previous process": write the loaded-skill row through the
            # original repo instance, then discard it.
            original_repo: SqlAlchemyAssistantLoadedSkillRepository | None = ctx.runtime[
                "assistant_loaded_skill_repository"
            ]
            original_body = (
                "# strategy-definition-authoring\n"
                "Body persisted by the previous process — survival marker PSPMK1."
            )
            assert original_repo is not None
            await original_repo.upsert(
                session_id=session_id,
                skill_name="strategy-definition-authoring",
                skill_path=".doyoutrade/skills/strategy-definition-authoring/SKILL.md",
                body=original_body,
                body_hash="c" * 64,
                metadata={"source": "load_skill_tool", "process": "prev"},
            )
            # Drop the original repo handle so subsequent reads can't be
            # served from any in-memory cache it might have held.
            original_repo = None
            del original_repo

            # "New process": build a brand-new repo against the same
            # session_factory (== same DB file).
            fresh_repo = SqlAlchemyAssistantLoadedSkillRepository(session_factory)

            # Append a summary_boundary so the injector engages.
            await assistant_service.repository.append_message(
                session_id=session_id,
                role="user",
                content="restart-precursor",
                linked_attempt_id=None,
                metadata={},
            )
            await assistant_service.repository.append_message(
                session_id=session_id,
                role="system",
                content="<compaction summary across restart>",
                linked_attempt_id=None,
                metadata=_summary_boundary_metadata(
                    compacted_until_message_id="msg-old-restart",
                    source_message_count=10,
                ),
            )
            await assistant_service.repository.append_message(
                session_id=session_id,
                role="user",
                content="post-restart tail user turn",
                linked_attempt_id=None,
                metadata={},
            )

            history_rows = await assistant_service.repository.list_messages(
                session_id, limit=100, offset=0
            )
            history_messages = ["m0", "m1", "m2"]
            result = await _inject_loaded_skills_reminder(
                history_messages,
                session_id=session_id,
                history_rows=history_rows,
                loaded_skill_repository=fresh_repo,
            )

            self.assertEqual(len(result), len(history_messages) + 1)
            reminder = result[-2]
            content = getattr(reminder, "content", None)
            # `assert isinstance` (vs self.assertIsInstance) narrows for the
            # subsequent assertIn calls — unittest's assertion does not.
            assert isinstance(content, str), (
                f"reminder.content must be str, got {type(content).__name__}"
            )
            self.assertIn("<system-reminder>", content)
            self.assertIn("strategy-definition-authoring", content)
            self.assertIn("PSPMK1", content)

            # Sanity: the fresh repo can also list the row directly.
            rows_via_fresh = await fresh_repo.list_by_session(session_id)
            self.assertEqual(len(rows_via_fresh), 1, rows_via_fresh)
            self.assertEqual(
                rows_via_fresh[0]["skill_name"], "strategy-definition-authoring"
            )
            self.assertEqual(
                rows_via_fresh[0]["metadata_json"].get("process"), "prev"
            )

    # ------------------------------------------------------------------
    # Scenario 5 — FK CASCADE: deleting the parent session reaps rows.
    # ------------------------------------------------------------------
    async def test_cascade_delete_on_session_removal(self) -> None:
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            engine = ctx.runtime["engine"]
            # SQLite ignores ondelete=CASCADE without PRAGMA foreign_keys=ON.
            _install_sqlite_fk_listener(engine)

            _, session = await self._make_agent_and_session(ctx)
            session_id = session["session_id"]
            repo: SqlAlchemyAssistantLoadedSkillRepository = ctx.runtime[
                "assistant_loaded_skill_repository"
            ]
            session_factory = ctx.runtime["session_factory"]

            await repo.upsert(
                session_id=session_id,
                skill_name="skill-keep-a",
                skill_path=".doyoutrade/skills/skill-keep-a/SKILL.md",
                body="body a",
                body_hash="d" * 64,
            )
            await repo.upsert(
                session_id=session_id,
                skill_name="skill-keep-b",
                skill_path=".doyoutrade/skills/skill-keep-b/SKILL.md",
                body="body b",
                body_hash="e" * 64,
            )
            self.assertEqual(len(await repo.list_by_session(session_id)), 2)

            # Hard-delete the parent session row.
            async with session_factory() as sql_session:
                await sql_session.execute(
                    delete(AssistantSessionRecord).where(
                        AssistantSessionRecord.session_id == session_id
                    )
                )
                await sql_session.commit()

            rows_after = await repo.list_by_session(session_id)
            self.assertEqual(
                rows_after,
                [],
                "FK CASCADE failed: loaded-skill rows should be reaped when "
                "the parent assistant_session is deleted",
            )

    # ------------------------------------------------------------------
    # Scenario 6 — debug event + persisted row both carry the same
    # body_hash prefix, so a debug-trace consumer can correlate the
    # ``operation_load_skill.persisted`` event back to the DB row.
    #
    # Plan calls for ``run_id`` to thread through cycle_runs /
    # debug_session_spans / model_invocations. Driving a real
    # model-mediated load_skill round-trip is heavier than this skill
    # needs — the stub model in tests/e2e/support.py never invokes
    # load_skill, so per the parent agent's fallback guidance we
    # validate the OTel side via debug_event capture + DB row
    # correlation instead.
    # ------------------------------------------------------------------
    async def test_load_skill_emits_debug_event_with_correlatable_hash(self) -> None:
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            _, session = await self._make_agent_and_session(ctx)
            session_id = session["session_id"]
            repo: SqlAlchemyAssistantLoadedSkillRepository = ctx.runtime[
                "assistant_loaded_skill_repository"
            ]

            captured: list[tuple[str, dict[str, Any]]] = []

            # ``emit_debug_event`` is imported lazily inside
            # ``LoadSkillTool.execute`` as
            # ``from doyoutrade.debug import emit_debug_event`` — patch the
            # re-exporting package attribute so the lookup resolves to
            # our spy. We forward to the real implementation so the OTel
            # span still gets the event (i.e. we observe, we don't
            # suppress — CLAUDE.md §错误可见性 disallows muting debug
            # events even in tests).
            import doyoutrade.debug as _debug_pkg

            real_emit = _debug_pkg.emit_debug_event

            async def _spy(event_type: str, payload: dict[str, Any]) -> None:
                captured.append((event_type, dict(payload)))
                await real_emit(event_type, payload)

            with patch.object(_debug_pkg, "emit_debug_event", _spy):
                tool = LoadSkillTool(loaded_skill_repository=repo)
                result = await tool.execute(
                    skill_name=_REAL_SKILL_NAME,
                    session_id=session_id,
                )

            self.assertFalse(result.is_error, result.text)

            persisted_events = [
                payload
                for event_type, payload in captured
                if event_type == "operation_load_skill.persisted"
            ]
            self.assertEqual(
                len(persisted_events),
                1,
                f"expected exactly one persisted event, got {captured!r}",
            )
            event_payload = persisted_events[0]
            self.assertEqual(event_payload.get("session_id"), session_id)
            self.assertEqual(event_payload.get("skill_name"), _REAL_SKILL_NAME)
            self.assertGreater(int(event_payload.get("byte_size") or 0), 0)
            event_hash_prefix = str(event_payload.get("body_hash") or "")
            # Debug event records only the 16-char prefix (see
            # doyoutrade/tools/__init__.py).
            self.assertEqual(len(event_hash_prefix), 16, event_payload)

            rows = await repo.list_by_session(session_id)
            self.assertEqual(len(rows), 1, rows)
            row = rows[0]
            self.assertTrue(
                row["body_hash"].startswith(event_hash_prefix),
                f"debug event hash prefix {event_hash_prefix!r} should be "
                f"a prefix of the persisted body_hash {row['body_hash']!r}",
            )
            self.assertEqual(int(event_payload["byte_size"]), row["byte_size"])

    # ------------------------------------------------------------------
    # Scenario 7 — PR-D short-circuit reuses persisted body without
    #             paying the full SKILL.md token cost again.
    # ------------------------------------------------------------------
    async def test_short_circuit_returns_stub_on_repeat_load(self) -> None:
        """End-to-end proof that a second ``load_skill`` for the same
        skill in the same session does NOT re-deliver the body when the
        on-disk SKILL.md is unchanged. Hits the real
        ``SqlAlchemyAssistantLoadedSkillRepository`` (real Alembic schema),
        so the (skill_name, body_hash) lookup is exercised on actual SQL.
        """
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            _, session = await self._make_agent_and_session(ctx)
            session_id = session["session_id"]
            repo: SqlAlchemyAssistantLoadedSkillRepository = ctx.runtime[
                "assistant_loaded_skill_repository"
            ]

            tool = LoadSkillTool(loaded_skill_repository=repo)

            # First call — full body delivered + persistence.
            first = await tool.execute(
                skill_name=_REAL_SKILL_NAME, session_id=session_id
            )
            self.assertFalse(first.is_error, first.text)
            self.assertIn(_REAL_SKILL_BODY_FRAGMENT, first.text)
            self.assertIn(
                "[Loaded and persisted; will be re-injected",
                first.text,
                "first call must persist (tail-note required)",
            )

            rows_after_first = await repo.list_by_session(session_id)
            self.assertEqual(len(rows_after_first), 1, rows_after_first)
            initial_loaded_at = rows_after_first[0]["loaded_at"]

            # Second call — same skill, same session, unchanged disk.
            second = await tool.execute(
                skill_name=_REAL_SKILL_NAME, session_id=session_id
            )
            self.assertFalse(second.is_error, second.text)
            # Stub markers present.
            self.assertIn("already loaded in this session", second.text)
            self.assertIn("body hash matches", second.text)
            self.assertIn("system-reminder", second.text)
            # The full SKILL.md body fragment must NOT appear in the stub
            # — token savings is the whole point of PR-D.
            self.assertNotIn(_REAL_SKILL_BODY_FRAGMENT, second.text)
            # No persistence-success tail-note: we did not re-upsert.
            self.assertNotIn(
                "[Loaded and persisted; will be re-injected",
                second.text,
            )

            # The DB row is unchanged: still 1 row, loaded_at preserved.
            rows_after_second = await repo.list_by_session(session_id)
            self.assertEqual(len(rows_after_second), 1, rows_after_second)
            self.assertEqual(
                rows_after_second[0]["loaded_at"],
                initial_loaded_at,
                "short-circuit must not bump loaded_at — newest-first "
                "ordering in the compaction reminder relies on the original "
                "load timestamp staying stable",
            )

    # ------------------------------------------------------------------
    # Scenario 8 — PR-D hash mismatch (on-disk edit) falls through to a
    #             full reload so freshness is preserved.
    # ------------------------------------------------------------------
    async def test_no_short_circuit_when_disk_body_changes(self) -> None:
        """Manually pokes a stale ``body_hash`` into the persisted row to
        simulate the SKILL.md on disk being edited after the previous
        load. The second ``load_skill`` call must NOT short-circuit;
        the new body has to reach the model and the row must be updated.
        """
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            _, session = await self._make_agent_and_session(ctx)
            session_id = session["session_id"]
            repo: SqlAlchemyAssistantLoadedSkillRepository = ctx.runtime[
                "assistant_loaded_skill_repository"
            ]

            tool = LoadSkillTool(loaded_skill_repository=repo)
            first = await tool.execute(
                skill_name=_REAL_SKILL_NAME, session_id=session_id
            )
            self.assertFalse(first.is_error)
            row = (await repo.list_by_session(session_id))[0]
            real_hash = row["body_hash"]
            stale_hash = ("f" * 64) if real_hash[0] != "f" else ("a" * 64)
            self.assertNotEqual(real_hash, stale_hash)

            # Overwrite the persisted row's body_hash to simulate
            # "the file on disk now hashes to something else".
            session_factory = ctx.runtime["session_factory"]
            from sqlalchemy import update  # type: ignore[import-not-found]
            from doyoutrade.persistence.models import AssistantLoadedSkillRecord

            async with session_factory() as db_session:
                await db_session.execute(
                    update(AssistantLoadedSkillRecord)
                    .where(
                        AssistantLoadedSkillRecord.session_id == session_id,
                        AssistantLoadedSkillRecord.skill_name == _REAL_SKILL_NAME,
                    )
                    .values(body_hash=stale_hash)
                )
                await db_session.commit()

            # Second call — the on-disk hash will mismatch the stale row.
            second = await tool.execute(
                skill_name=_REAL_SKILL_NAME, session_id=session_id
            )
            self.assertFalse(second.is_error)
            # Full body redelivered (drift case — freshness wins).
            self.assertIn(_REAL_SKILL_BODY_FRAGMENT, second.text)
            self.assertIn(
                "[Loaded and persisted; will be re-injected",
                second.text,
            )
            # Stub markers must NOT appear.
            self.assertNotIn("already loaded in this session", second.text)

            # Row is upserted: body_hash back to the real value.
            updated = (await repo.list_by_session(session_id))[0]
            self.assertEqual(updated["body_hash"], real_hash)
            self.assertNotEqual(updated["body_hash"], stale_hash)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
