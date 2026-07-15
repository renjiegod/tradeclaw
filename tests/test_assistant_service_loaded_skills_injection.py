"""Tests for the loaded-skill reminder injection helpers in
:mod:`doyoutrade.assistant.service`.

We focus on the routing logic (when to query the repo, where to splice
the reminder into ``history_messages``) and leave the reminder body
shape to :mod:`tests.test_loaded_skills_reminder`.

Pinned invariants:

* No ``summary_boundary`` in history → repo is **not** queried (we still
  have the original ``load_skill`` ``tool_result`` in history, so
  re-injecting wastes tokens and inflates the next compaction pass).
* With a boundary → repo is queried, reminder is spliced in immediately
  before the tail user message.
* Missing repository (e.g. ``InMemoryAssistantRepository`` test paths
  that don't wire the loaded-skill store) → no-op, repo not queried.
"""

from __future__ import annotations

import unittest
from typing import Any

from doyoutrade.assistant.service import (
    _history_contains_summary_boundary,
    _inject_loaded_skills_reminder,
)


class _FakeRepository:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = list(rows or [])
        self.calls: list[str] = []

    async def list_by_session(self, session_id: str) -> list[dict[str, Any]]:
        self.calls.append(session_id)
        return list(self._rows)


_BOUNDARY_ROW: dict[str, Any] = {
    "message_id": "msg-boundary",
    "role": "system",
    "content": "summary text",
    "metadata": {
        "context_compaction": {
            "kind": "summary_boundary",
            "strategy": "full",
            "compacted_until_message_id": "msg-older",
            "source_message_count": 12,
        }
    },
}

_LOADED_SKILL_ROW: dict[str, Any] = {
    "session_id": "sess-1",
    "skill_name": "strategy-iteration",
    "skill_path": ".doyoutrade/skills/strategy-iteration/SKILL.md",
    "body": "Iterate carefully.",
    "body_hash": "h",
    "byte_size": 18,
    "loaded_at": "2026-05-28T02:00:00",
    "metadata_json": {},
}


class HistoryContainsSummaryBoundaryTests(unittest.TestCase):
    def test_no_boundary_when_empty(self) -> None:
        self.assertFalse(_history_contains_summary_boundary([]))

    def test_no_boundary_when_only_user(self) -> None:
        rows = [{"role": "user", "content": "hi", "metadata": {}}]
        self.assertFalse(_history_contains_summary_boundary(rows))

    def test_boundary_detected(self) -> None:
        rows = [
            {"role": "user", "content": "hi", "metadata": {}},
            _BOUNDARY_ROW,
            {"role": "user", "content": "next turn", "metadata": {}},
        ]
        self.assertTrue(_history_contains_summary_boundary(rows))

    def test_non_dict_metadata_does_not_crash(self) -> None:
        rows = [
            {"role": "user", "content": "weird", "metadata": "not-a-dict"},
        ]
        self.assertFalse(_history_contains_summary_boundary(rows))


class InjectLoadedSkillsReminderTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_boundary_no_injection(self) -> None:
        repo = _FakeRepository(rows=[_LOADED_SKILL_ROW])
        history_messages = ["msg-a", "msg-b-tail"]
        history_rows = [
            {"role": "user", "content": "hi", "metadata": {}},
            {"role": "assistant", "content": "hello", "metadata": {}},
        ]
        result = await _inject_loaded_skills_reminder(
            history_messages,
            session_id="sess-1",
            history_rows=history_rows,
            loaded_skill_repository=repo,
        )
        # Pre-compaction: the original load_skill tool_result is still in
        # history, so we must not re-inject and we must not even hit the
        # repo (would waste a DB roundtrip on every turn).
        self.assertEqual(result, history_messages)
        self.assertEqual(repo.calls, [])

    async def test_with_boundary_injects_reminder(self) -> None:
        repo = _FakeRepository(rows=[_LOADED_SKILL_ROW])
        history_messages = ["msg-a", "msg-b-tail"]
        history_rows = [
            {"role": "user", "content": "old", "metadata": {}},
            _BOUNDARY_ROW,
            {"role": "user", "content": "tail", "metadata": {}},
        ]
        result = await _inject_loaded_skills_reminder(
            history_messages,
            session_id="sess-1",
            history_rows=history_rows,
            loaded_skill_repository=repo,
        )
        self.assertEqual(repo.calls, ["sess-1"])
        # Reminder is inserted just before the tail.
        self.assertEqual(len(result), len(history_messages) + 1)
        self.assertEqual(result[0], "msg-a")
        self.assertEqual(result[-1], "msg-b-tail")
        reminder_msg = result[-2]
        # The reminder is a HumanMessage-like object; rely only on `.content`.
        content = getattr(reminder_msg, "content", None)
        self.assertIsInstance(content, str)
        self.assertIn("<system-reminder>", content)
        self.assertIn("strategy-iteration", content)

    async def test_no_repository_returns_unchanged(self) -> None:
        history_messages = ["msg-a", "msg-b-tail"]
        history_rows = [
            {"role": "user", "content": "old", "metadata": {}},
            _BOUNDARY_ROW,
            {"role": "user", "content": "tail", "metadata": {}},
        ]
        result = await _inject_loaded_skills_reminder(
            history_messages,
            session_id="sess-1",
            history_rows=history_rows,
            loaded_skill_repository=None,
        )
        self.assertEqual(result, history_messages)

    async def test_boundary_but_no_loaded_skills_returns_unchanged(self) -> None:
        repo = _FakeRepository(rows=[])
        history_messages = ["msg-a", "msg-b-tail"]
        history_rows = [
            {"role": "user", "content": "old", "metadata": {}},
            _BOUNDARY_ROW,
            {"role": "user", "content": "tail", "metadata": {}},
        ]
        result = await _inject_loaded_skills_reminder(
            history_messages,
            session_id="sess-1",
            history_rows=history_rows,
            loaded_skill_repository=repo,
        )
        # The repo *is* queried (we have to know whether there's anything
        # to inject) but the helper returns the history unchanged when the
        # builder hands back None.
        self.assertEqual(repo.calls, ["sess-1"])
        self.assertEqual(result, history_messages)

    async def test_empty_history_messages_with_boundary(self) -> None:
        repo = _FakeRepository(rows=[_LOADED_SKILL_ROW])
        history_rows = [_BOUNDARY_ROW]
        result = await _inject_loaded_skills_reminder(
            [],
            session_id="sess-1",
            history_rows=history_rows,
            loaded_skill_repository=repo,
        )
        # No tail to splice before → reminder becomes the sole message.
        self.assertEqual(len(result), 1)
        content = getattr(result[0], "content", None)
        self.assertIsInstance(content, str)
        self.assertIn("<system-reminder>", content)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
