"""Tests for :mod:`doyoutrade.assistant.loaded_skills_reminder`.

Covers the post-compaction skill-reminder builder. The builder reads rows
that :class:`SqlAlchemyAssistantLoadedSkillRepository` persists when
``load_skill`` runs, then formats a single ``<system-reminder>`` message
to be injected just before the tail user message. The model uses this
reminder to keep operating on previously-loaded skills without re-calling
``load_skill`` after the original ``tool_result`` blocks were folded into
a compaction summary.

What we pin down here:

* Empty session → no reminder (no waste).
* Per-skill content is fully rendered (skill_name, path, loaded_at, body).
* Newest skills appear first so eviction under the total budget drops the
  oldest skills, matching Claude Code's POST_COMPACT_SKILLS_TOKEN_BUDGET.
* Per-skill truncation kicks in when a body exceeds the per-skill cap.
* Total budget evicts the oldest skills, not the newest.
* Repository read failure degrades to ``None`` and warns (CLAUDE.md
  §错误可见性 — visibility instead of silent fallback).
* Empty session_id raises (programming-bug surface area).
"""

from __future__ import annotations

import logging
import unittest
from typing import Any

from doyoutrade.assistant.loaded_skills_reminder import (
    LOADED_SKILLS_TOKENS_PER_SKILL,
    LOADED_SKILLS_TOTAL_BUDGET,
    build_loaded_skills_reminder,
)


class _FakeRepository:
    """Async-compatible stand-in for ``SqlAlchemyAssistantLoadedSkillRepository``.

    Stores a fixed payload (or exception) to return from ``list_by_session``;
    the reminder builder only depends on that one method, so we don't need
    a real SQLite engine here.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        exc: BaseException | None = None,
    ) -> None:
        self._rows = list(rows or [])
        self._exc = exc
        self.calls: list[str] = []

    async def list_by_session(self, session_id: str) -> list[dict[str, Any]]:
        self.calls.append(session_id)
        if self._exc is not None:
            raise self._exc
        return list(self._rows)


def _row(
    *,
    name: str,
    path: str = "",
    body: str = "body",
    loaded_at: str = "2026-05-28T00:00:00",
) -> dict[str, Any]:
    return {
        "session_id": "sess-1",
        "skill_name": name,
        "skill_path": path or f".doyoutrade/skills/{name}/SKILL.md",
        "body": body,
        "body_hash": "h",
        "byte_size": len(body.encode("utf-8")),
        "loaded_at": loaded_at,
        "metadata_json": {},
    }


class BuildLoadedSkillsReminderTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_session_returns_none(self) -> None:
        repo = _FakeRepository(rows=[])
        reminder = await build_loaded_skills_reminder("sess-1", repo)
        self.assertIsNone(reminder)
        self.assertEqual(repo.calls, ["sess-1"])

    async def test_single_skill_in_reminder(self) -> None:
        repo = _FakeRepository(
            rows=[
                _row(
                    name="strategy-iteration",
                    path=".doyoutrade/skills/strategy-iteration/SKILL.md",
                    body="Iterate carefully.",
                    loaded_at="2026-05-28T01:00:00",
                ),
            ]
        )
        reminder = await build_loaded_skills_reminder("sess-1", repo)
        assert reminder is not None  # for type-checkers
        text = reminder.content
        self.assertIsInstance(text, str)
        self.assertTrue(text.startswith("<system-reminder>"))
        self.assertTrue(text.endswith("</system-reminder>"))
        self.assertIn("strategy-iteration", text)
        self.assertIn(".doyoutrade/skills/strategy-iteration/SKILL.md", text)
        self.assertIn("2026-05-28T01:00:00", text)
        self.assertIn("Iterate carefully.", text)
        self.assertIn("load_skill", text)  # mentions no-recall hint

    async def test_multiple_skills_newest_first(self) -> None:
        repo = _FakeRepository(
            rows=[
                _row(name="newer", loaded_at="2026-05-28T02:00:00", body="NEW_BODY"),
                _row(name="older", loaded_at="2026-05-28T01:00:00", body="OLD_BODY"),
            ]
        )
        reminder = await build_loaded_skills_reminder("sess-1", repo)
        assert reminder is not None
        text = reminder.content
        self.assertLess(text.index("newer"), text.index("older"))
        self.assertLess(text.index("NEW_BODY"), text.index("OLD_BODY"))

    async def test_unsorted_repository_rows_resorted_newest_first(self) -> None:
        # Defensive resort path: feed rows out of order, ensure newer wins.
        repo = _FakeRepository(
            rows=[
                _row(name="older", loaded_at="2026-05-28T01:00:00"),
                _row(name="newer", loaded_at="2026-05-28T02:00:00"),
            ]
        )
        reminder = await build_loaded_skills_reminder("sess-1", repo)
        assert reminder is not None
        text = reminder.content
        self.assertLess(text.index("newer"), text.index("older"))

    async def test_per_skill_truncation(self) -> None:
        # 30 000 bytes ~= 7 500 tokens, well over the 5 000-token per-skill cap.
        oversized_body = "X" * 30_000
        repo = _FakeRepository(
            rows=[
                _row(name="huge", body=oversized_body, loaded_at="2026-05-28T03:00:00"),
            ]
        )
        reminder = await build_loaded_skills_reminder("sess-1", repo)
        assert reminder is not None
        text = reminder.content
        self.assertIn("[...truncated to fit per-skill budget]", text)
        # The truncated body itself should be roughly under
        # LOADED_SKILLS_TOKENS_PER_SKILL * 4 bytes; with framing this gives
        # a comfortable upper bound. Use total content size as a coarse check.
        body_section_end = text.index("[...truncated")
        body_section_start = text.index("(loaded at")
        body_bytes = len(text[body_section_start:body_section_end].encode("utf-8"))
        self.assertLessEqual(
            body_bytes,
            (LOADED_SKILLS_TOKENS_PER_SKILL * 4) + 200,  # +slack for framing
        )

    async def test_total_budget_drops_oldest(self) -> None:
        # Each body ~= 5 000 tokens worth of bytes. Seven such skills would
        # need 35 000 tokens; the 25 000-token total budget should keep the
        # newest few and drop the oldest.
        big_body = "Y" * (LOADED_SKILLS_TOKENS_PER_SKILL * 4)
        rows = [
            _row(
                name=f"skill-{i}",
                body=big_body,
                loaded_at=f"2026-05-28T{i:02d}:00:00",  # later i = newer
            )
            for i in range(1, 8)
        ]
        repo = _FakeRepository(rows=rows)
        reminder = await build_loaded_skills_reminder("sess-1", repo)
        assert reminder is not None
        text = reminder.content
        # Newest (i=7) must be present; oldest (i=1) must be evicted.
        self.assertIn("skill-7", text)
        self.assertNotIn("skill-1", text)
        # Sanity-check: at least one of the older ones is also dropped so
        # we're actually evicting (not just including everything).
        evicted = [name for name in ("skill-1", "skill-2") if name not in text]
        self.assertTrue(evicted)
        # And we never include more bytes than the total budget allows.
        self.assertLessEqual(
            len(text.encode("utf-8")),
            (LOADED_SKILLS_TOTAL_BUDGET * 4) + 1024,  # +slack for framing
        )

    async def test_repository_failure_returns_none_and_logs(self) -> None:
        repo = _FakeRepository(exc=RuntimeError("db down"))
        with self.assertLogs(
            "doyoutrade.assistant.loaded_skills_reminder", level="WARNING"
        ) as captured:
            reminder = await build_loaded_skills_reminder("sess-1", repo)
        self.assertIsNone(reminder)
        joined = "\n".join(captured.output)
        self.assertIn("session_id=sess-1", joined)
        self.assertIn("error_type=RuntimeError", joined)
        self.assertIn("db down", joined)

    async def test_empty_session_id_raises(self) -> None:
        repo = _FakeRepository(rows=[])
        with self.assertRaises(ValueError) as ctx:
            await build_loaded_skills_reminder("", repo)
        self.assertIn("session_id", str(ctx.exception))
        # The error must include the actual offending value so operators
        # can see what was passed in.
        self.assertIn("''", str(ctx.exception))
        # Repository must not have been queried for an invalid id.
        self.assertEqual(repo.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
