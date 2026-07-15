from __future__ import annotations

import unittest

from doyoutrade.api.operations.cycle_run_tools import ListCycleRunsTool
from doyoutrade.persistence.errors import RecordNotFoundError

from tests._tool_result_helpers import payload as _payload


def _extract_candidates(text):
    """Parse `- <task_id> [<status>] <name> (<mode>)` lines from result.text."""
    import re as _re
    out = []
    for line in text.splitlines():
        m = _re.match(r"^- (\S+) \[([^\]]*)\] (\S*) \(([^)]*)\)$", line.strip())
        if m:
            out.append({"task_id": m.group(1), "status": m.group(2), "name": m.group(3), "mode": m.group(4)})
    return out


class _FakeService:
    """Minimal platform-service stand-in for ListCycleRunsTool tests.

    Mirrors the production behaviour: ``list_cycle_runs_summary`` performs a
    PK-only lookup on the supplied identifier (so passing a name raises
    ``RecordNotFoundError``), and ``list_tasks_summary`` is consulted by the
    name-fallback helper for an exact-name match.
    """

    def __init__(self, *, tasks: list[dict]) -> None:
        self.tasks = tasks
        self.tasks_by_id = {t["task_id"]: t for t in tasks}
        self.calls: list[tuple[str, dict]] = []

    async def list_cycle_runs_summary(self, identifier: str, **kwargs):
        self.calls.append(("list_cycle_runs_summary", {"identifier": identifier, **kwargs}))
        if identifier not in self.tasks_by_id:
            raise RecordNotFoundError(f"task not found: {identifier}")
        return {
            "items": [{"run_id": "run-1", "task_id": identifier}],
            "total": 1,
            "limit": kwargs.get("limit", 50),
            "offset": kwargs.get("offset", 0),
        }

    async def list_tasks_summary(self, **payload):
        self.calls.append(("list_tasks_summary", payload))
        q = payload.get("q")
        items = (
            [t for t in self.tasks if q in (t.get("name") or "")]
            if q
            else list(self.tasks)
        )
        return {
            "items": items,
            "total": len(items),
            "limit": payload.get("limit", 20),
            "offset": payload.get("offset", 0),
        }


class ListCycleRunsToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_by_task_id_succeeds(self) -> None:
        svc = _FakeService(
            tasks=[{"task_id": "abc-uuid", "name": "MACD_Backtest", "status": "completed", "mode": "backtest"}]
        )
        tool = ListCycleRunsTool(svc)
        result = await tool.execute(identifier="abc-uuid")
        self.assertFalse(result.is_error)
        self.assertEqual(_payload(result)["status"], "ok")
        self.assertEqual(_payload(result)["items"][0]["run_id"], "run-1")
        # No name fallback should run when the primary call succeeded.
        self.assertNotIn("resolved_from_name", _payload(result))
        self.assertEqual(
            [c[0] for c in svc.calls],
            ["list_cycle_runs_summary"],
        )

    async def test_resolves_unique_task_name(self) -> None:
        svc = _FakeService(
            tasks=[{"task_id": "abc-uuid", "name": "MACD_Backtest", "status": "completed", "mode": "backtest"}]
        )
        tool = ListCycleRunsTool(svc)
        result = await tool.execute(identifier="MACD_Backtest")
        self.assertFalse(result.is_error)
        self.assertEqual(_payload(result)["status"], "ok")
        self.assertEqual(_payload(result)["items"][0]["task_id"], "abc-uuid")
        self.assertEqual(_payload(result)["resolved_from_name"], "MACD_Backtest")
        # Two list_cycle_runs_summary attempts (name miss + retry) and one list_tasks_summary probe.
        op_sequence = [c[0] for c in svc.calls]
        self.assertEqual(
            op_sequence,
            ["list_cycle_runs_summary", "list_tasks_summary", "list_cycle_runs_summary"],
        )

    async def test_returns_ambiguous_candidates_when_name_collides(self) -> None:
        svc = _FakeService(
            tasks=[
                {"task_id": "id-1", "name": "MACD_Backtest", "status": "completed", "mode": "backtest"},
                {"task_id": "id-2", "name": "MACD_Backtest", "status": "configured", "mode": "backtest"},
            ]
        )
        tool = ListCycleRunsTool(svc)
        result = await tool.execute(identifier="MACD_Backtest")
        self.assertTrue(result.is_error)
        self.assertIn("[error:ambiguous_task_name]", result.text)
        ids = sorted(c["task_id"] for c in _extract_candidates(result.text))
        self.assertEqual(ids, ["id-1", "id-2"])
        self.assertIn("Hint:", result.text)
        # The retry attempt should not happen when ambiguous.
        op_sequence = [c[0] for c in svc.calls]
        self.assertEqual(
            op_sequence,
            ["list_cycle_runs_summary", "list_tasks_summary"],
        )

    async def test_returns_helpful_error_when_name_does_not_exist(self) -> None:
        svc = _FakeService(
            tasks=[{"task_id": "abc-uuid", "name": "MACD_Backtest", "status": "completed", "mode": "backtest"}]
        )
        tool = ListCycleRunsTool(svc)
        result = await tool.execute(identifier="ghost-name")
        self.assertTrue(result.is_error)
        self.assertIn("[error:task_not_found]", result.text)
        self.assertIn("list_tasks(q=...)", result.text)

    async def test_schema_describes_id_and_name(self) -> None:
        tool = ListCycleRunsTool(_FakeService(tasks=[]))
        desc = tool.parameters["properties"]["identifier"]["description"]
        self.assertIn("task_id", desc)
        self.assertIn("task name", desc.lower())
        self.assertIn("ambiguous_task_name", desc)


if __name__ == "__main__":
    unittest.main()
