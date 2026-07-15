"""Tests for ``LoadSkillTool`` persistence wiring (plan C-T2).

The tool must:

* deliver the SKILL.md body to the model on every successful match (existing
  contract — covered by ``tests/test_assistant_load_skill_tool.py``),
* additionally upsert a row into ``assistant_loaded_skills`` when a calling
  ``session_id`` was injected and the repository dependency is present, so
  the body can be replayed as a ``<system-reminder>`` after context
  compaction folds the original ``tool_result`` block away,
* fail visibly on persistence errors (``logger.warning`` + debug event)
  while still returning a *successful* ``ToolResult`` so the current turn
  is not derailed — only the compaction-survival promise is lost,
* never fabricate a ``session_id``: a missing one is a legitimate path
  (CLI / out-of-band invocations) and the response just omits the
  compaction-survival tail-note.

Also pins the ``OperationRegistry.execute`` injection contract: tools that
opt in via ``requires_session_id = True`` receive ``session_id`` in their
kwargs from the calling session without the model having to recite it; the
default (flag absent) leaves kwargs untouched.
"""

from __future__ import annotations

import hashlib
import logging
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from doyoutrade.tools import (
    LoadSkillTool,
    OperationHandler,
    OperationRegistry,
    ToolResult,
)


_TAIL_NOTE_FRAGMENT = "[Loaded and persisted; will be re-injected"


def _make_skill(
    *,
    name: str = "strategy-definition-authoring",
    skill_path: str = "strategy-definition-authoring",
    skill_dir: Path = Path("/abs/.doyoutrade/skills/strategy-definition-authoring"),
    body: str = "# strategy-definition-authoring\nbody content here.",
) -> MagicMock:
    skill = MagicMock(enabled=True)
    skill.name = name
    skill.skill_path = skill_path
    skill.skill_dir = skill_dir
    skill.body = body
    return skill


class LoadSkillToolPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_session_id_skips_persistence(self) -> None:
        repo = MagicMock()
        repo.upsert = AsyncMock()
        tool = LoadSkillTool(loaded_skill_repository=repo)
        skill = _make_skill()
        with patch("doyoutrade.tools.load_skills", return_value=[skill]):
            result = await tool.execute(skill_name="strategy-definition-authoring")

        self.assertIsInstance(result, ToolResult)
        self.assertFalse(result.is_error)
        # Body still reaches the model on this turn.
        self.assertIn("body content here.", result.text)
        # No tail-note: we did not persist, so we cannot promise survival.
        self.assertNotIn(_TAIL_NOTE_FRAGMENT, result.text)
        repo.upsert.assert_not_awaited()

    async def test_with_session_id_persists_skill(self) -> None:
        repo = MagicMock()
        repo.upsert = AsyncMock()
        # PR-D: tool now checks for an existing persisted row before
        # delivering the full body. Empty list keeps the short-circuit
        # branch off so the upsert path remains the one under test.
        repo.list_by_session = AsyncMock(return_value=[])
        tool = LoadSkillTool(loaded_skill_repository=repo)
        skill = _make_skill()
        with patch("doyoutrade.tools.load_skills", return_value=[skill]):
            result = await tool.execute(
                skill_name="strategy-definition-authoring",
                session_id="sess-test",
            )

        self.assertIsInstance(result, ToolResult)
        self.assertFalse(result.is_error)
        self.assertIn("body content here.", result.text)
        self.assertIn(_TAIL_NOTE_FRAGMENT, result.text)

        repo.upsert.assert_awaited_once()
        kwargs = repo.upsert.await_args.kwargs
        self.assertEqual(kwargs["session_id"], "sess-test")
        self.assertEqual(kwargs["skill_name"], "strategy-definition-authoring")
        self.assertEqual(kwargs["skill_path"], "strategy-definition-authoring")
        self.assertEqual(kwargs["body"], skill.body)
        expected_hash = hashlib.sha256(skill.body.encode("utf-8")).hexdigest()
        self.assertEqual(kwargs["body_hash"], expected_hash)
        self.assertEqual(kwargs["metadata"], {"source": "load_skill_tool"})

    async def test_persistence_failure_returns_success_anyway(self) -> None:
        repo = MagicMock()
        repo.upsert = AsyncMock(side_effect=RuntimeError("db down"))
        repo.list_by_session = AsyncMock(return_value=[])
        tool = LoadSkillTool(loaded_skill_repository=repo)
        skill = _make_skill()
        with patch("doyoutrade.tools.load_skills", return_value=[skill]):
            result = await tool.execute(
                skill_name="strategy-definition-authoring",
                session_id="sess-test",
            )

        # Current turn is not derailed: SKILL.md body still in text, no
        # is_error flag, but the tail-note is suppressed because we
        # cannot honestly promise compaction survival.
        self.assertIsInstance(result, ToolResult)
        self.assertFalse(result.is_error)
        self.assertIn("body content here.", result.text)
        self.assertNotIn(_TAIL_NOTE_FRAGMENT, result.text)

    async def test_persistence_failure_logs_and_emits_event(self) -> None:
        repo = MagicMock()
        repo.upsert = AsyncMock(side_effect=RuntimeError("db down"))
        repo.list_by_session = AsyncMock(return_value=[])
        tool = LoadSkillTool(loaded_skill_repository=repo)
        skill = _make_skill()

        with patch("doyoutrade.tools.load_skills", return_value=[skill]), patch(
            "doyoutrade.debug.emit_debug_event", new=AsyncMock()
        ) as mock_emit, self.assertLogs(
            "doyoutrade.tools", level=logging.WARNING
        ) as captured_logs:
            result = await tool.execute(
                skill_name="strategy-definition-authoring",
                session_id="sess-test",
            )

        self.assertFalse(result.is_error)
        # WARNING log with the structured fields.
        joined = "\n".join(captured_logs.output)
        self.assertIn("load_skill.persistence_failed", joined)
        self.assertIn("sess-test", joined)
        self.assertIn("RuntimeError", joined)
        self.assertIn("db down", joined)

        # Debug event with the expected name + payload shape.
        event_calls = [
            (args[0], args[1])
            for args, _ in (
                (call.args, call.kwargs) for call in mock_emit.await_args_list
            )
            if args
        ]
        self.assertTrue(
            any(
                name == "operation_load_skill.persistence_failed"
                for name, _payload in event_calls
            ),
            f"expected persistence_failed event, got {event_calls!r}",
        )
        for name, payload in event_calls:
            if name == "operation_load_skill.persistence_failed":
                self.assertEqual(payload["session_id"], "sess-test")
                self.assertEqual(payload["skill_name"], "strategy-definition-authoring")
                self.assertEqual(payload["error_type"], "RuntimeError")
                self.assertEqual(payload["error_message"], "db down")
                self.assertIn("compaction", payload["hint"])

    async def test_session_id_injected_via_registry(self) -> None:
        """Tools flagged ``requires_session_id`` get session_id auto-filled."""

        captured: dict[str, object] = {}

        class _FakeTool(OperationHandler):
            name = "fake_session_aware"
            description = "test"
            category = "agent"
            requires_session_id = True
            parameters = {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": [],
            }

            async def execute(self, **kwargs):  # type: ignore[override]
                captured.update(kwargs)
                return ToolResult(text="ok")

        registry = OperationRegistry([_FakeTool()])
        await registry.execute("fake_session_aware", {}, session_id="sess-x")
        self.assertEqual(captured.get("session_id"), "sess-x")

    async def test_session_id_not_injected_when_flag_off(self) -> None:
        captured: dict[str, object] = {}

        class _FakeTool(OperationHandler):
            name = "fake_session_unaware"
            description = "test"
            category = "agent"
            # No requires_session_id flag.
            parameters = {
                "type": "object",
                "properties": {},
                "required": [],
            }

            async def execute(self, **kwargs):  # type: ignore[override]
                captured.update(kwargs)
                return ToolResult(text="ok")

        registry = OperationRegistry([_FakeTool()])
        await registry.execute("fake_session_unaware", {}, session_id="sess-x")
        self.assertNotIn("session_id", captured)

    # ------------------------------------------------------------------
    # PR-D — short-circuit on body_hash match
    # ------------------------------------------------------------------
    #
    # Defensive re-loads (the agent calling ``load_skill`` for the same
    # skill multiple times in one session, e.g. once at the start of each
    # "writing turn") are common while PR-B's rule forbids hallucinating
    # the SDK shape. PR-D makes those re-loads cheap: when an existing
    # ``assistant_loaded_skills`` row matches (skill_name, body_hash), the
    # tool returns a short stub instead of the ~5K-token SKILL.md body.
    # On-disk edits invalidate the cache via the ``body_hash`` mismatch
    # path, so freshness is preserved.

    async def test_short_circuit_when_body_hash_matches(self) -> None:
        skill = _make_skill()
        cached_hash = hashlib.sha256(skill.body.encode("utf-8")).hexdigest()
        repo = MagicMock()
        repo.upsert = AsyncMock()
        repo.list_by_session = AsyncMock(
            return_value=[
                {
                    "session_id": "sess-test",
                    "skill_name": skill.name,
                    "skill_path": skill.skill_path,
                    "body_hash": cached_hash,
                    "loaded_at": "2026-05-28T04:00:00",
                }
            ]
        )
        tool = LoadSkillTool(loaded_skill_repository=repo)
        with patch("doyoutrade.tools.load_skills", return_value=[skill]):
            result = await tool.execute(
                skill_name=skill.name,
                session_id="sess-test",
            )

        self.assertFalse(result.is_error)
        # Stub: announces the prior load and the system-reminder fallback,
        # asks the agent to stop re-loading.
        self.assertIn("already loaded in this session", result.text)
        self.assertIn("body hash matches", result.text)
        self.assertIn("system-reminder", result.text)
        # Full body must NOT be in the stub — that is the whole point of
        # the short-circuit.
        self.assertNotIn(skill.body, result.text)
        # No new upsert: ``loaded_at`` stays at the first-delivery time so
        # the reminder's newest-first sort keeps its meaning.
        repo.upsert.assert_not_awaited()

    async def test_no_short_circuit_when_body_hash_mismatches(self) -> None:
        # Simulates the on-disk SKILL.md being edited after the previous
        # load: the persisted row's body_hash no longer matches what's on
        # disk, so the tool must fall through to the full reload + upsert.
        skill = _make_skill(body="# strategy-definition-authoring\nEDITED body.")
        stale_hash = hashlib.sha256(b"old body").hexdigest()
        repo = MagicMock()
        repo.upsert = AsyncMock()
        repo.list_by_session = AsyncMock(
            return_value=[
                {
                    "session_id": "sess-test",
                    "skill_name": skill.name,
                    "skill_path": skill.skill_path,
                    "body_hash": stale_hash,
                    "loaded_at": "2026-05-28T03:00:00",
                }
            ]
        )
        tool = LoadSkillTool(loaded_skill_repository=repo)
        with patch("doyoutrade.tools.load_skills", return_value=[skill]):
            result = await tool.execute(
                skill_name=skill.name,
                session_id="sess-test",
            )

        self.assertFalse(result.is_error)
        # Full body delivered (drift case — model must see the new content).
        self.assertIn("EDITED body.", result.text)
        # Tail-note present: persistence ran (overwrites the stale row).
        self.assertIn(_TAIL_NOTE_FRAGMENT, result.text)
        repo.upsert.assert_awaited_once()
        # New row carries the new hash.
        kwargs = repo.upsert.await_args.kwargs
        expected_hash = hashlib.sha256(skill.body.encode("utf-8")).hexdigest()
        self.assertEqual(kwargs["body_hash"], expected_hash)
        self.assertNotEqual(kwargs["body_hash"], stale_hash)

    async def test_short_circuit_check_failure_falls_through(self) -> None:
        # Transient repo failure on the existence check must NOT block the
        # current turn. We log a warning, fall through to the normal
        # deliver-and-upsert path, and let the agent get its SKILL.md.
        skill = _make_skill()
        repo = MagicMock()
        repo.upsert = AsyncMock()
        repo.list_by_session = AsyncMock(side_effect=RuntimeError("db hiccup"))
        tool = LoadSkillTool(loaded_skill_repository=repo)
        with patch(
            "doyoutrade.tools.load_skills", return_value=[skill]
        ), self.assertLogs("doyoutrade.tools", level=logging.WARNING) as captured:
            result = await tool.execute(
                skill_name=skill.name,
                session_id="sess-test",
            )

        self.assertFalse(result.is_error)
        # Body still delivered.
        self.assertIn(skill.body, result.text)
        # WARNING log is structured (visible per §错误可见性).
        joined = "\n".join(captured.output)
        self.assertIn("load_skill.shortcircuit_check_failed", joined)
        self.assertIn("sess-test", joined)
        self.assertIn("RuntimeError", joined)
        self.assertIn("db hiccup", joined)
        # Upsert still ran (the read failure does not block the write).
        repo.upsert.assert_awaited_once()

    async def test_short_circuit_emits_debug_event(self) -> None:
        skill = _make_skill()
        cached_hash = hashlib.sha256(skill.body.encode("utf-8")).hexdigest()
        repo = MagicMock()
        repo.upsert = AsyncMock()
        repo.list_by_session = AsyncMock(
            return_value=[
                {
                    "session_id": "sess-test",
                    "skill_name": skill.name,
                    "skill_path": skill.skill_path,
                    "body_hash": cached_hash,
                    "loaded_at": "2026-05-28T04:00:00",
                }
            ]
        )
        tool = LoadSkillTool(loaded_skill_repository=repo)
        with patch("doyoutrade.tools.load_skills", return_value=[skill]), patch(
            "doyoutrade.debug.emit_debug_event", new=AsyncMock()
        ) as mock_emit:
            await tool.execute(
                skill_name=skill.name,
                session_id="sess-test",
            )

        event_names = [call.args[0] for call in mock_emit.await_args_list if call.args]
        self.assertIn("operation_load_skill.shortcircuit", event_names)
        # Make sure we did NOT emit the regular persisted event — that
        # would be misleading observability (no row was written).
        self.assertNotIn("operation_load_skill.persisted", event_names)
        # Payload pins the fields needed for trace correlation.
        for call in mock_emit.await_args_list:
            if call.args and call.args[0] == "operation_load_skill.shortcircuit":
                payload = call.args[1]
                self.assertEqual(payload["session_id"], "sess-test")
                self.assertEqual(payload["skill_name"], skill.name)
                self.assertEqual(payload["body_hash"], cached_hash[:16])
                self.assertEqual(payload["saved_bytes"], len(skill.body.encode("utf-8")))
                break

    async def test_short_circuit_only_matches_same_skill(self) -> None:
        # An existing row for a different skill in the same session must
        # NOT cause a short-circuit on this lookup — each skill is cached
        # independently.
        skill = _make_skill(name="strategy-iteration", body="iteration body")
        other_hash = hashlib.sha256(b"unrelated body").hexdigest()
        repo = MagicMock()
        repo.upsert = AsyncMock()
        repo.list_by_session = AsyncMock(
            return_value=[
                {
                    "session_id": "sess-test",
                    "skill_name": "strategy-definition-authoring",  # different skill
                    "skill_path": "strategy-definition-authoring",
                    "body_hash": other_hash,
                    "loaded_at": "2026-05-28T03:00:00",
                }
            ]
        )
        tool = LoadSkillTool(loaded_skill_repository=repo)
        with patch("doyoutrade.tools.load_skills", return_value=[skill]):
            result = await tool.execute(
                skill_name="strategy-iteration",
                session_id="sess-test",
            )

        self.assertFalse(result.is_error)
        # Full body for the new skill is delivered.
        self.assertIn("iteration body", result.text)
        # Tail-note: this is a fresh load, persistence ran.
        self.assertIn(_TAIL_NOTE_FRAGMENT, result.text)
        repo.upsert.assert_awaited_once()
        kwargs = repo.upsert.await_args.kwargs
        self.assertEqual(kwargs["skill_name"], "strategy-iteration")

    async def test_unknown_skill_returns_error(self) -> None:
        repo = MagicMock()
        repo.upsert = AsyncMock()
        repo.list_by_session = AsyncMock(return_value=[])
        tool = LoadSkillTool(loaded_skill_repository=repo)
        with patch("doyoutrade.tools.load_skills", return_value=[]):
            result = await tool.execute(
                skill_name="does-not-exist",
                session_id="sess-x",
            )

        self.assertTrue(result.is_error)
        self.assertIn("skill_not_found", result.text)
        repo.upsert.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
