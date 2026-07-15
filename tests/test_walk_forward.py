"""Tests for the ``walk_forward_backtest`` operation + ``backtest walk-forward``.

A fake platform service stands in for the real async-backtest machinery so no
server / DB is needed: each window's ``create_task`` → ``start_backtest_job`` →
``get_backtest_job`` (terminal immediately) → ``get_backtest_summary`` returns a
preset metric dict. We assert the verdict logic (robust / fragile /
inconclusive), per-window failure isolation, task cleanup, and the CLI gate.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass
from datetime import date
from typing import Any
from unittest.mock import patch

from doyoutrade.api.operations.walk_forward import (
    WalkForwardBacktestTool,
    _compute_windows,
    _extract_window_metrics,
)
from doyoutrade.cli._envelope import parse_tool_result


@dataclass
class _Task:
    task_id: str


def _summary(*, return_pct: str, trades: int, sharpe: str = "1.0") -> dict[str, Any]:
    return {
        "return_pct": return_pct,
        "sharpe": sharpe,
        "max_drawdown_pct": "5.0",
        "win_rate": "0.5",
        "profit_factor": "1.2",
        "trade_count_closed": trades,
        "fills_count": trades * 2,
    }


class _FakeService:
    """Minimal stand-in: one summary per window, in creation order."""

    def __init__(self, summaries: list[dict[str, Any]], *, fail_windows: frozenset[int] = frozenset()):
        self._summaries = summaries
        self._fail_windows = fail_windows
        self._counter = 0
        self._run_to_idx: dict[str, int] = {}
        self.created_tasks: list[str] = []
        self.deleted_tasks: list[str] = []

    async def create_task(self, *, name, mode, description, data_provider, settings, **_):  # noqa: ANN001
        idx = self._counter
        self._counter += 1
        task_id = f"task-{idx}"
        self.created_tasks.append(task_id)
        # stash idx on the task_id via the mapping when the job starts
        self._pending_idx = idx
        return _Task(task_id=task_id)

    async def start_backtest_job(self, identifier, *, range_start, range_end, debug_enabled=False, **_):  # noqa: ANN001
        idx = int(str(identifier).split("-")[1])
        run_id = f"run-{idx}"
        self._run_to_idx[run_id] = idx
        return {"run_id": run_id, "task_id": identifier, "status": "running"}

    async def get_backtest_job(self, identifier, run_id):  # noqa: ANN001
        idx = self._run_to_idx[run_id]
        status = "failed" if idx in self._fail_windows else "completed"
        return {"run_id": run_id, "task_id": identifier, "status": status}

    async def get_backtest_summary(self, run_id):  # noqa: ANN001
        idx = self._run_to_idx[run_id]
        return {"summary_state": "ok", "summary": self._summaries[idx]}

    async def delete_task(self, identifier):  # noqa: ANN001
        self.deleted_tasks.append(identifier)


def _run(tool: WalkForwardBacktestTool, **kwargs) -> tuple[dict, bool]:
    base = {
        "definition_id": "sd-abc123",
        "universe": ["600519.SH"],
        "range_start": "2024-01-01",
        "range_end": "2024-12-31",
    }
    base.update(kwargs)
    result = asyncio.run(tool.execute(**base))
    data, _s, error_info = parse_tool_result(result.text, is_error=result.is_error)
    payload = data if isinstance(data, dict) else (error_info or {})
    return payload, result.is_error


class WalkForwardHelperTests(unittest.TestCase):
    def test_compute_windows_contiguous_nonoverlapping(self) -> None:
        wins = _compute_windows(date(2024, 1, 1), date(2024, 12, 31), 3)
        self.assertEqual(len(wins), 3)
        self.assertEqual(wins[0][0], date(2024, 1, 1))
        self.assertEqual(wins[-1][1], date(2024, 12, 31))
        # non-overlapping: each window starts after the previous ends
        for (s0, e0), (s1, _e1) in zip(wins, wins[1:]):
            self.assertLessEqual(s0, e0)
            self.assertGreater(s1, e0)

    def test_extract_metrics_parses_strings(self) -> None:
        m = _extract_window_metrics(_summary(return_pct="3.5", trades=4))
        self.assertAlmostEqual(m["return_pct"], 3.5)
        self.assertEqual(m["trade_count_closed"], 4)
        self.assertEqual(m["fills_count"], 8)


class WalkForwardVerdictTests(unittest.TestCase):
    def test_all_profitable_is_robust(self) -> None:
        svc = _FakeService([
            _summary(return_pct="2.0", trades=3),
            _summary(return_pct="1.0", trades=2),
            _summary(return_pct="3.0", trades=5),
        ])
        payload, is_error = _run(WalkForwardBacktestTool(svc))
        self.assertFalse(is_error, msg=f"payload: {payload}")
        self.assertEqual(payload.get("status"), "robust", msg=f"payload: {payload}")
        self.assertEqual(payload.get("eligible_windows"), 3)
        self.assertEqual(payload.get("positive_windows"), 3)
        # default keep_tasks=False → all per-window tasks deleted
        self.assertEqual(len(svc.deleted_tasks), 3)

    def test_mixed_is_fragile(self) -> None:
        svc = _FakeService([
            _summary(return_pct="5.0", trades=4),
            _summary(return_pct="-2.0", trades=3),
            _summary(return_pct="-1.0", trades=2),
        ])
        payload, is_error = _run(WalkForwardBacktestTool(svc))
        self.assertFalse(is_error)
        self.assertEqual(payload.get("status"), "fragile", msg=f"payload: {payload}")
        self.assertEqual(payload.get("positive_windows"), 1)
        self.assertEqual(payload.get("eligible_windows"), 3)

    def test_no_trades_is_inconclusive(self) -> None:
        svc = _FakeService([
            _summary(return_pct="0.0", trades=0),
            _summary(return_pct="0.0", trades=0),
            _summary(return_pct="0.0", trades=0),
        ])
        payload, is_error = _run(WalkForwardBacktestTool(svc))
        self.assertFalse(is_error)
        self.assertEqual(payload.get("status"), "inconclusive", msg=f"payload: {payload}")
        self.assertEqual(payload.get("eligible_windows"), 0)

    def test_one_failed_window_isolated(self) -> None:
        svc = _FakeService(
            [
                _summary(return_pct="2.0", trades=3),
                _summary(return_pct="0.0", trades=0),  # this run reports failed status
                _summary(return_pct="3.0", trades=4),
            ],
            fail_windows=frozenset({1}),
        )
        payload, is_error = _run(WalkForwardBacktestTool(svc))
        self.assertFalse(is_error)
        self.assertEqual(payload.get("failed_windows"), 1)
        # 2 eligible profitable windows remain → robust
        self.assertEqual(payload.get("status"), "robust", msg=f"payload: {payload}")
        self.assertEqual(payload.get("eligible_windows"), 2)

    def test_all_failed_is_error(self) -> None:
        svc = _FakeService(
            [_summary(return_pct="0", trades=0)] * 3,
            fail_windows=frozenset({0, 1, 2}),
        )
        payload, is_error = _run(WalkForwardBacktestTool(svc))
        self.assertTrue(is_error)
        self.assertEqual(payload.get("error_code"), "all_windows_failed")

    def test_keep_tasks_retains(self) -> None:
        svc = _FakeService([
            _summary(return_pct="1.0", trades=2),
            _summary(return_pct="1.0", trades=2),
        ])
        payload, _is_error = _run(WalkForwardBacktestTool(svc), segments=2, keep_tasks=True)
        self.assertEqual(len(svc.deleted_tasks), 0)
        # run_id surfaced for drill-in when kept
        self.assertTrue(any(w.get("run_id") for w in payload.get("windows", [])))


class WalkForwardInputTests(unittest.TestCase):
    def test_missing_definition_is_error(self) -> None:
        # identifier guard rejects a non-sd definition id
        tool = WalkForwardBacktestTool(_FakeService([]))
        result = asyncio.run(
            tool.execute(definition_id="not-an-sd", universe=["600519.SH"], range_start="2024-01-01", range_end="2024-12-31")
        )
        self.assertTrue(result.is_error)

    def test_unknown_kwarg_rejected(self) -> None:
        tool = WalkForwardBacktestTool(_FakeService([]))
        result = asyncio.run(
            tool.execute(
                definition_id="sd-abc", universe=["600519.SH"],
                range_start="2024-01-01", range_end="2024-12-31", bogus=1,
            )
        )
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)

    def test_invalid_segments_is_error(self) -> None:
        payload, is_error = _run(WalkForwardBacktestTool(_FakeService([])), segments=9)
        self.assertTrue(is_error)
        self.assertEqual(payload.get("error_code"), "invalid_segments")

    def test_service_unavailable(self) -> None:
        tool = WalkForwardBacktestTool(None)
        result = asyncio.run(
            tool.execute(definition_id="sd-abc", universe=["600519.SH"], range_start="2024-01-01", range_end="2024-12-31")
        )
        self.assertTrue(result.is_error)


class WalkForwardCliTests(unittest.TestCase):
    def _invoke(self, fake_envelope: dict, extra_args=None):
        from click.testing import CliRunner

        from doyoutrade.cli.commands.backtest import backtest as backtest_group

        captured: dict = {}

        async def _fake_invoke_api(method, path, *, json=None, meta=None, timeout_seconds=None, **kwargs):
            captured["path"] = path
            captured["json"] = json
            captured["timeout_seconds"] = timeout_seconds
            return fake_envelope, 0

        runner = CliRunner()
        args = [
            "walk-forward", "--definition", "sd-abc123",
            "--universe", "600519.SH,000001.SZ",
            "--range-start", "2024-01-01", "--range-end", "2024-12-31",
        ] + (extra_args or [])
        with patch("doyoutrade.cli.commands.backtest_runs.invoke_api", new=_fake_invoke_api):
            result = runner.invoke(backtest_group, args, catch_exceptions=False, obj={"fmt": "json"})
        return result, captured

    def test_robust_exits_0(self) -> None:
        result, captured = self._invoke({"ok": True, "data": {"status": "robust"}})
        self.assertEqual(result.exit_code, 0, msg=f"out: {result.output}")
        self.assertEqual(captured["path"], "/backtest/walk-forward")
        self.assertEqual(captured["json"]["universe"], ["600519.SH", "000001.SZ"])
        self.assertEqual(captured["json"]["definition_id"], "sd-abc123")

    def test_fragile_exits_1_gate(self) -> None:
        result, _captured = self._invoke({"ok": True, "data": {"status": "fragile"}})
        self.assertEqual(result.exit_code, 1, msg=f"out: {result.output}")

    def test_inconclusive_exits_0(self) -> None:
        result, _captured = self._invoke({"ok": True, "data": {"status": "inconclusive"}})
        self.assertEqual(result.exit_code, 0)

    def test_api_timeout_scaled_by_segments(self) -> None:
        _result, captured = self._invoke(
            {"ok": True, "data": {"status": "robust"}},
            extra_args=["--segments", "4", "--timeout", "30"],
        )
        # 4 windows * 30s + 30 buffer = 150
        self.assertEqual(captured["timeout_seconds"], 150.0)
        self.assertEqual(captured["json"]["segments"], 4)


if __name__ == "__main__":
    unittest.main()
