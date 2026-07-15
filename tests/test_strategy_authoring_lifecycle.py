"""Tests for the strategy authoring session lifecycle tools.

Uses stub repository and compiler classes to keep tests fast and
filesystem-isolated. All tests use a real StrategyStorage backed by a
temporary directory so the on-disk state machine is exercised.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from doyoutrade.persistence.strategy_storage import StrategyStorage, SCAFFOLD_STRATEGY_PY
from doyoutrade.strategy_runtime.compiler import StrategyCompiler, StrategyCompileResult
from doyoutrade.assistant.strategy_tools.authoring_tools import (
    OpenStrategyAuthoringTool,
    CancelStrategyAuthoringTool,
    CompileStrategyDraftTool,
    FinalizeStrategyAuthoringTool,
    locate_session,
    SessionNotFound,
)
from doyoutrade.tools import ToolResult


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _DefinitionRecord:
    definition_id: str
    name: str
    current_version: str | None = None


class _RecordingRepo:
    """Stub strategy definition repository.

    Records ``create_definition`` and ``update_definition`` calls so tests
    can assert state without a real DB.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, _DefinitionRecord] = {}
        # Tracks the most recent current_version set via update_definition
        self.current_version: str | None = None

    async def create_definition(self, **kwargs: Any) -> MagicMock:
        defn_id = kwargs["definition_id"]
        record = _DefinitionRecord(
            definition_id=defn_id,
            name=kwargs.get("name", ""),
            current_version=kwargs.get("current_version"),
        )
        self._definitions[defn_id] = record
        snap = MagicMock()
        snap.definition_id = defn_id
        snap.name = record.name
        snap.current_version = record.current_version
        snap.code_hash = kwargs.get("code_hash", "")
        return snap

    async def get_definition(self, definition_id: str) -> MagicMock:
        if definition_id not in self._definitions:
            from doyoutrade.persistence.errors import RecordNotFoundError
            raise RecordNotFoundError(f"not found: {definition_id}")
        record = self._definitions[definition_id]
        snap = MagicMock()
        snap.definition_id = record.definition_id
        snap.name = record.name
        snap.current_version = record.current_version
        snap.code_hash = ""
        return snap

    async def update_definition(self, definition_id: str, **kwargs: Any) -> MagicMock:
        if definition_id not in self._definitions:
            from doyoutrade.persistence.errors import RecordNotFoundError
            raise RecordNotFoundError(f"not found: {definition_id}")
        record = self._definitions[definition_id]
        if "current_version" in kwargs:
            record.current_version = kwargs["current_version"]
            self.current_version = kwargs["current_version"]
        snap = MagicMock()
        snap.definition_id = record.definition_id
        snap.current_version = record.current_version
        return snap


class _StubCompiler:
    """Minimal stub that succeeds by default; can be overridden per-test."""

    def __init__(self, *, succeed: bool = True) -> None:
        self._succeed = succeed

    def validate_directory(self, code_root: Path) -> StrategyCompileResult:
        if self._succeed:
            artifact = MagicMock()
            artifact.class_name = "Strategy"
            return StrategyCompileResult.ok_result(artifact=artifact)
        return StrategyCompileResult.failure(
            error_code="syntax_error",
            errors=("intentional test failure",),
            error_dicts=({"error_code": "syntax_error", "message": "bad syntax"},),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tools(
    *,
    storage: StrategyStorage,
    repo: _RecordingRepo,
    compiler: Any = None,
) -> tuple[
    OpenStrategyAuthoringTool,
    CancelStrategyAuthoringTool,
    CompileStrategyDraftTool,
    FinalizeStrategyAuthoringTool,
]:
    if compiler is None:
        compiler = _StubCompiler(succeed=True)
    open_tool = OpenStrategyAuthoringTool(storage=storage, repository=repo, compiler=compiler)
    cancel_tool = CancelStrategyAuthoringTool(storage=storage, repository=repo, compiler=compiler)
    compile_tool = CompileStrategyDraftTool(storage=storage, repository=repo, compiler=compiler)
    finalize_tool = FinalizeStrategyAuthoringTool(storage=storage, repository=repo, compiler=compiler)
    return open_tool, cancel_tool, compile_tool, finalize_tool


def _run(coro):
    """Run an async coroutine synchronously for test convenience."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _json_payload(result: ToolResult) -> dict:
    """Extract the JSON payload embedded in a ToolResult's text.

    ``append_json_payload`` formats the payload as a fenced ```json block.
    For error results formatted by ``format_error_text`` there is no JSON
    block, so we return ``{"error_code": <extracted from [error:...] prefix>}``.
    """
    import json
    import re
    text = result.text
    # Try to find fenced ```json block first
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Fall back: extract from [error:<code>] prefix
    m2 = re.match(r"\[error:([^\]]+)\]", text)
    if m2:
        return {"error_code": m2.group(1), "status": "error"}
    return {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpenCreatesDefinitionAndDraft(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._storage = StrategyStorage(Path(self._tmp.name))
        self._repo = _RecordingRepo()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_open_creates_definition_and_draft_when_no_id_supplied(self) -> None:
        open_tool, _, _, _ = _make_tools(storage=self._storage, repo=self._repo)
        result = _run(open_tool.execute(name="My Strategy"))

        self.assertFalse(result.is_error, f"unexpected error: {result.text}")
        payload = _json_payload(result)
        self.assertEqual(payload["status"], "created")
        # definition_id must be a new sd- id
        defn_id = payload["definition_id"]
        self.assertTrue(defn_id.startswith("sd-"), defn_id)
        # session_id must start with sess-
        sess_id = payload["session_id"]
        self.assertTrue(sess_id.startswith("sess-"), sess_id)
        # work_dir must exist on disk
        work_dir = Path(payload["work_dir"])
        self.assertTrue(work_dir.is_dir(), f"work_dir not found: {work_dir}")
        # base_version is None for a new definition
        self.assertIsNone(payload["base_version"])
        # scaffold was written
        self.assertTrue((work_dir / "strategy.py").is_file())


class TestOpenAgainstExistingDefinitionCopiesCurrentVersion(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._storage = StrategyStorage(Path(self._tmp.name))
        self._repo = _RecordingRepo()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_open_against_existing_definition_copies_current_version(self) -> None:
        # Seed a definition + a finalized version
        open_tool, _, _, finalize_tool = _make_tools(storage=self._storage, repo=self._repo)

        # First: create new via open
        r1 = _run(open_tool.execute(name="Seeded Strategy"))
        self.assertFalse(r1.is_error)
        p1 = _json_payload(r1)
        defn_id = p1["definition_id"]
        sess1 = p1["session_id"]

        # Finalize first session to get a v1
        r_fin = _run(finalize_tool.execute(session_id=sess1))
        self.assertFalse(r_fin.is_error, f"finalize failed: {r_fin.text}")
        v1_label = _json_payload(r_fin)["version_label"]

        # Confirm repo updated
        self.assertEqual(self._repo.current_version, v1_label)

        # Re-open against the same definition — should copy v1
        r2 = _run(open_tool.execute(definition_id=defn_id))
        self.assertFalse(r2.is_error, f"re-open failed: {r2.text}")
        p2 = _json_payload(r2)
        self.assertEqual(p2["status"], "ok")
        self.assertEqual(p2["definition_id"], defn_id)
        self.assertEqual(p2["base_version"], v1_label)
        # New session
        self.assertNotEqual(p2["session_id"], sess1)
        work_dir2 = Path(p2["work_dir"])
        self.assertTrue((work_dir2 / "strategy.py").is_file())


class TestCompileDraftReturnsSmokeFailureWithoutPromoting(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._storage = StrategyStorage(Path(self._tmp.name))
        self._repo = _RecordingRepo()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_compile_draft_returns_smoke_failure_without_promoting(self) -> None:
        failing_compiler = _StubCompiler(succeed=False)
        open_tool, _, compile_tool, _ = _make_tools(
            storage=self._storage, repo=self._repo, compiler=failing_compiler
        )

        r_open = _run(open_tool.execute(name="Bad Strategy"))
        self.assertFalse(r_open.is_error)
        p_open = _json_payload(r_open)
        sess_id = p_open["session_id"]
        defn_id = p_open["definition_id"]

        # Write intentionally broken Python
        work_dir = Path(p_open["work_dir"])
        (work_dir / "strategy.py").write_text("def broken(:\n    pass\n")

        r_compile = _run(compile_tool.execute(session_id=sess_id))
        self.assertTrue(r_compile.is_error, "expected compile to fail")
        payload = _json_payload(r_compile)
        self.assertIn("error_code", payload)

        # Draft must still exist
        draft_still_there = work_dir.is_dir()
        self.assertTrue(draft_still_there, "draft was removed after compile failure")

        # current_version still None (no promote happened)
        self.assertIsNone(self._repo.current_version)


class TestFinalizationPromotesAndBumpsCurrentVersion(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._storage = StrategyStorage(Path(self._tmp.name))
        self._repo = _RecordingRepo()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finalize_promotes_and_bumps_current_version(self) -> None:
        open_tool, _, _, finalize_tool = _make_tools(
            storage=self._storage, repo=self._repo
        )

        r_open = _run(open_tool.execute(name="Good Strategy"))
        self.assertFalse(r_open.is_error)
        p_open = _json_payload(r_open)
        sess_id = p_open["session_id"]
        defn_id = p_open["definition_id"]

        r_fin = _run(finalize_tool.execute(session_id=sess_id))
        self.assertFalse(r_fin.is_error, f"finalize failed: {r_fin.text}")
        p_fin = _json_payload(r_fin)

        self.assertEqual(p_fin["status"], "ok")
        version_label = p_fin["version_label"]
        self.assertTrue(version_label.startswith("v0001-"), version_label)

        # Repository must have current_version updated
        self.assertEqual(self._repo.current_version, version_label)

        # Versioned dir must exist on disk
        versions_dir = self._storage.versions_dir(defn_id)
        self.assertTrue((versions_dir / version_label).is_dir())

        # Draft dir must be gone (promoted)
        draft_dir = self._storage.draft_dir(defn_id, sess_id)
        self.assertFalse(draft_dir.exists(), "draft dir should be gone after finalize")


class TestCancelRemovesDraft(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._storage = StrategyStorage(Path(self._tmp.name))
        self._repo = _RecordingRepo()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cancel_removes_draft(self) -> None:
        open_tool, cancel_tool, _, _ = _make_tools(storage=self._storage, repo=self._repo)

        r_open = _run(open_tool.execute(name="Cancel Me"))
        self.assertFalse(r_open.is_error)
        p_open = _json_payload(r_open)
        sess_id = p_open["session_id"]
        defn_id = p_open["definition_id"]
        work_dir = Path(p_open["work_dir"])

        self.assertTrue(work_dir.is_dir())

        r_cancel = _run(cancel_tool.execute(session_id=sess_id))
        self.assertFalse(r_cancel.is_error, f"cancel failed: {r_cancel.text}")

        self.assertFalse(work_dir.is_dir(), "draft dir should be removed after cancel")


class TestFinalizeUnknownSession(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._storage = StrategyStorage(Path(self._tmp.name))
        self._repo = _RecordingRepo()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finalize_unknown_session(self) -> None:
        _, _, _, finalize_tool = _make_tools(storage=self._storage, repo=self._repo)

        r = _run(finalize_tool.execute(session_id="sess-doesnotexist"))
        self.assertTrue(r.is_error)
        self.assertIn("session_not_found", r.text)


class TestOpenAgainstNonExistentDefinitionId(unittest.TestCase):
    """Extra test (gap discovered during implementation): open with an unknown definition_id."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._storage = StrategyStorage(Path(self._tmp.name))
        self._repo = _RecordingRepo()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_open_against_nonexistent_definition_id_returns_error(self) -> None:
        open_tool, _, _, _ = _make_tools(storage=self._storage, repo=self._repo)

        r = _run(open_tool.execute(definition_id="sd-doesnotexist"))
        self.assertTrue(r.is_error)
        payload = _json_payload(r)
        # error_code may be in the payload or the text
        self.assertIn("definition_not_found", r.text)


class TestLocateSessionHelper(unittest.TestCase):
    """Unit test for the locate_session shared helper."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._storage = StrategyStorage(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_locate_session_raises_when_not_found(self) -> None:
        with self.assertRaises(SessionNotFound):
            locate_session(self._storage, "sess-bogus")

    def test_locate_session_returns_definition_id_and_path(self) -> None:
        # Manually create the draft layout
        defn_id = "sd-test123"
        sess_id = "sess-abc123"
        draft = self._storage.draft_dir(defn_id, sess_id)
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.mkdir()

        found_defn_id, found_path = locate_session(self._storage, sess_id)
        self.assertEqual(found_defn_id, defn_id)
        self.assertEqual(found_path, draft)


if __name__ == "__main__":
    unittest.main()
