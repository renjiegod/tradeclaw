"""Tests for ``GetBacktestSummaryTool`` and the inline summary attached by
``RunStrategyBacktestTool`` after a successful run.

Both pieces are pure read-side wiring: the service-layer aggregator already
exists (``service.get_backtest_summary``) and the dense summary JSON is
persisted by the run loop (``doyoutrade.backtest.summary``). These tests lock
the contract the agent sees so the "agent recomputes MACD" failure mode
(see ``tmp/messages.json``) cannot regress.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from doyoutrade.assistant.strategy_tools.resource_tools import GetBacktestSummaryTool
from doyoutrade.assistant.strategy_tools.run_tools import RunStrategyBacktestTool
from doyoutrade.persistence.errors import RecordNotFoundError

from tests._tool_result_helpers import payload as _payload


class _ReportsDirOverride:
    """Test helper: redirect DOYOUTRADE_REPORTS_DIR to a fresh temp dir for the
    duration of a single test. Restores the previous value on exit so other
    tests are unaffected.
    """

    def __init__(self, *, override: str | None = None) -> None:
        self._override = override
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._previous: str | None = None
        self._previously_set: bool = False

    def __enter__(self) -> str:
        self._previously_set = "DOYOUTRADE_REPORTS_DIR" in os.environ
        self._previous = os.environ.get("DOYOUTRADE_REPORTS_DIR")
        if self._override is not None:
            os.environ["DOYOUTRADE_REPORTS_DIR"] = self._override
            return self._override
        self._tmp = tempfile.TemporaryDirectory(prefix="doyoutrade-reports-")
        os.environ["DOYOUTRADE_REPORTS_DIR"] = self._tmp.name
        return self._tmp.name

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._previously_set:
            os.environ["DOYOUTRADE_REPORTS_DIR"] = self._previous or ""
        else:
            os.environ.pop("DOYOUTRADE_REPORTS_DIR", None)
        if self._tmp is not None:
            self._tmp.cleanup()


_SUMMARY_PAYLOAD = {
    "schema_version": 1,
    "run_id": "btjob-1",
    "completed_at": "2026-05-15T08:00:00Z",
    "range_start_utc": "2026-04-01T00:00:00Z",
    "range_end_utc": "2026-05-15T00:00:00Z",
    "bar_interval": "1d",
    "starting_equity": "100000",
    "ending_equity": "166240.86",
    "return_pct": "66.24",
    "final_cash": "0",
    "final_market_value": "266353.02",
    "final_positions": [
        {
            "symbol": "600522.SH",
            "name": None,
            "quantity": 6498,
            "available": 6498,
            "cost_price": "30.80",
            "last_price": "40.99",
            "market_value": "266353.02",
        }
    ],
    "trade_count_closed": 0,
    "trade_count_open": 1,
    "fills_count": 1,
    "win_rate": "1.00",
    "win_rate_sample_size": 1,
    "avg_holding_trading_days": "16.0",
    "avg_holding_sample_size": 1,
    "max_drawdown_pct": "3.42",
    "max_drawdown_peak_equity": "120000",
    "max_drawdown_trough_equity": "115900",
    "max_drawdown_peak_at": "2026-04-22T00:00:00Z",
    "max_drawdown_trough_at": "2026-04-29T00:00:00Z",
    "equity_curve_meta": {"downsampled": False, "raw_length": 30},
    "equity_curve": [
        {"t": "2026-04-01T00:00:00Z", "equity": "100000"},
        {"t": "2026-05-15T00:00:00Z", "equity": "166240.86"},
    ],
}


class _SummaryServiceStub:
    """Service stub for the standalone ``get_backtest_summary`` tool tests."""

    def __init__(
        self,
        *,
        responses: dict[str, dict] | None = None,
        not_found: set[str] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._not_found = not_found or set()
        self.calls: list[str] = []

    async def get_backtest_summary(self, run_id: str) -> dict:
        self.calls.append(run_id)
        if run_id in self._not_found:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        if run_id not in self._responses:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        return self._responses[run_id]


class GetBacktestSummaryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_ok_default_returns_markdown_report(self) -> None:
        service = _SummaryServiceStub(
            responses={
                "btjob-1": {
                    "run": {
                        "run_id": "btjob-1",
                        "task_id": "task-1",
                        "status": "completed",
                        "starting_equity": 100000.0,
                        "ending_equity": 166240.86,
                        "return_pct": 0.6624,
                    },
                    "task_id": "task-1",
                    "summary": _SUMMARY_PAYLOAD,
                    "summary_state": "ok",
                    "latest_summary_run_id": "btjob-1",
                }
            }
        )
        tool = GetBacktestSummaryTool(service)

        result = await tool.execute(run_id="btjob-1")

        self.assertFalse(result.is_error)
        # Markdown body in prose head.
        self.assertIn("## 回测报告 · `btjob-1`", result.text)
        self.assertIn("66.24%", result.text)  # return_pct
        # JSON tail keeps the run header only — no inline summary dict.
        self.assertIn('"status": "ok"', result.text)
        self.assertIn('"run_id": "btjob-1"', result.text)
        self.assertNotIn('"backtest_summary"', result.text)
        self.assertEqual(service.calls, ["btjob-1"])

    async def test_ok_json_format_returns_dense_field_pack(self) -> None:
        service = _SummaryServiceStub(
            responses={
                "btjob-1": {
                    "run": {
                        "run_id": "btjob-1",
                        "task_id": "task-1",
                        "status": "completed",
                    },
                    "task_id": "task-1",
                    "summary": _SUMMARY_PAYLOAD,
                    "summary_state": "ok",
                    "latest_summary_run_id": "btjob-1",
                }
            }
        )
        tool = GetBacktestSummaryTool(service)

        result = await tool.execute(run_id="btjob-1", format="json")

        self.assertFalse(result.is_error)
        self.assertIn('"return_pct": "66.24"', result.text)
        self.assertIn('"max_drawdown_pct": "3.42"', result.text)
        self.assertIn('"fills_count": 1', result.text)
        # Markdown report does *not* leak into the JSON-mode response.
        self.assertNotIn("## 回测报告", result.text)

    async def test_invalid_format_returns_validation_error(self) -> None:
        service = _SummaryServiceStub()
        tool = GetBacktestSummaryTool(service)

        result = await tool.execute(run_id="btjob-1", format="yaml")

        self.assertTrue(result.is_error)
        self.assertIn("[error:validation_error]", result.text)
        self.assertIn("format must be", result.text)

    async def test_stale_reports_latest_summary_run_id(self) -> None:
        service = _SummaryServiceStub(
            responses={
                "btjob-old": {
                    "run": {"run_id": "btjob-old", "task_id": "task-1", "status": "completed"},
                    "task_id": "task-1",
                    "summary": None,
                    "summary_state": "stale",
                    "latest_summary_run_id": "btjob-new",
                }
            }
        )
        tool = GetBacktestSummaryTool(service)

        result = await tool.execute(run_id="btjob-old")

        self.assertTrue(result.is_error)
        self.assertIn("[error:backtest_summary_stale]", result.text)
        self.assertIn("btjob-new", result.text)

    async def test_missing_summary_returns_not_ready(self) -> None:
        service = _SummaryServiceStub(
            responses={
                "btjob-inflight": {
                    "run": {"run_id": "btjob-inflight", "task_id": "task-1", "status": "running"},
                    "task_id": "task-1",
                    "summary": None,
                    "summary_state": "missing",
                    "latest_summary_run_id": None,
                }
            }
        )
        tool = GetBacktestSummaryTool(service)

        result = await tool.execute(run_id="btjob-inflight")

        self.assertTrue(result.is_error)
        self.assertIn("[error:backtest_summary_not_ready]", result.text)
        self.assertIn("running", result.text)
        # Repair hint surfaces in the error text via format_error_text.
        self.assertIn("Hint", result.text)

    async def test_unknown_run_id_returns_not_found(self) -> None:
        service = _SummaryServiceStub(not_found={"btjob-ghost"})
        tool = GetBacktestSummaryTool(service)

        result = await tool.execute(run_id="btjob-ghost")

        self.assertTrue(result.is_error)
        self.assertIn("[error:backtest_summary_not_found]", result.text)

    async def test_unknown_kwarg_is_rejected(self) -> None:
        service = _SummaryServiceStub()
        tool = GetBacktestSummaryTool(service)

        result = await tool.execute(run_id="btjob-1", extra="nope")

        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)
        # Unknown kwargs name is surfaced in the prose.
        self.assertIn("extra", result.text)

    async def test_missing_run_id_returns_validation_error(self) -> None:
        service = _SummaryServiceStub()
        tool = GetBacktestSummaryTool(service)

        result = await tool.execute(run_id="   ")

        self.assertTrue(result.is_error)
        self.assertIn("[error:validation_error]", result.text)
        self.assertIn("run_id is required", result.text)


class _RunBacktestSummaryAwareStub:
    """Run-strategy-backtest service stub that *also* answers get_backtest_summary."""

    def __init__(
        self,
        *,
        run_response: dict,
        summary_response: dict | None,
    ) -> None:
        self._run_response = run_response
        self._summary_response = summary_response
        self.start_calls: list[dict] = []
        self.summary_calls: list[str] = []

    async def start_backtest_job(self, task_id: str, **kwargs):
        self.start_calls.append({"task_id": task_id, **kwargs})
        return {"run_id": self._run_response["run_id"], "task_id": task_id, "status": "running"}

    async def get_backtest_job(self, task_id: str, run_id: str):
        return self._run_response

    async def get_backtest_summary(self, run_id: str) -> dict:
        self.summary_calls.append(run_id)
        if self._summary_response is None:
            raise RecordNotFoundError(f"backtest job not found: {run_id}")
        return self._summary_response


class RunStrategyBacktestInlineSummaryTests(unittest.IsolatedAsyncioTestCase):
    """``run_strategy_backtest`` renders the persisted summary as a markdown
    body in ``ToolResult.text`` so the agent can forward it to the user
    verbatim. The dense ``backtest_summary`` JSON is *not* inlined into the
    payload — fronts ends fetch it separately via REST, and agents that
    need raw fields call ``get_backtest_summary``.
    """

    async def test_terminal_ok_persists_report_to_disk(self) -> None:
        service = _RunBacktestSummaryAwareStub(
            run_response={
                "run_id": "btjob-1",
                "task_id": "task-1",
                "status": "completed",
            },
            summary_response={
                "run": {"run_id": "btjob-1", "task_id": "task-1", "status": "completed"},
                "task_id": "task-1",
                "summary": _SUMMARY_PAYLOAD,
                "summary_state": "ok",
                "latest_summary_run_id": "btjob-1",
            },
        )
        tool = RunStrategyBacktestTool(service)

        with _ReportsDirOverride() as reports_dir:
            raw = await tool.execute(
                task_id="task-1",
                range_start="2026-04-01",
                range_end="2026-05-15",
                poll_interval_seconds=0,
                timeout_seconds=5,
            )

            decoded = _payload(raw)
            self.assertEqual(decoded["status"], "ok")
            self.assertEqual(decoded["backtest_job"]["status"], "completed")
            # The bulky summary dict no longer ships inside the tool payload.
            self.assertNotIn("backtest_summary", decoded)
            # The report file lives on disk under DOYOUTRADE_REPORTS_DIR/<run_id>.md.
            expected_path = os.path.join(reports_dir, "btjob-1.md")
            self.assertEqual(decoded.get("report_path"), expected_path)
            with open(expected_path, "r", encoding="utf-8") as fh:
                on_disk = fh.read()
            self.assertIn("## 回测报告 · `btjob-1`", on_disk)
            self.assertIn("66.24%", on_disk)  # return_pct
            # Inline text now points at the file instead of inlining markdown.
            text = raw.text
            self.assertIn(expected_path, text)
            self.assertNotIn("## 回测报告", text)
            # Inline text stays small.
            self.assertLess(len(text.encode("utf-8")), 1024)
            # The next-step hint is now the only follow-up pointer.
            self.assertTrue(
                any("suggest_strategy_iteration" in s for s in decoded.get("next_steps") or [])
            )
            self.assertEqual(service.summary_calls, ["btjob-1"])

    async def test_terminal_ok_with_stale_summary_emits_pointer(self) -> None:
        service = _RunBacktestSummaryAwareStub(
            run_response={
                "run_id": "btjob-1",
                "task_id": "task-1",
                "status": "completed",
            },
            summary_response={
                "run": {"run_id": "btjob-1", "task_id": "task-1", "status": "completed"},
                "task_id": "task-1",
                "summary": None,
                "summary_state": "stale",
                "latest_summary_run_id": "btjob-newer",
            },
        )
        tool = RunStrategyBacktestTool(service)

        with _ReportsDirOverride():
            result = _payload(await tool.execute(task_id="task-1",
                    range_start="2026-04-01",
                    range_end="2026-05-15",
                    poll_interval_seconds=0,
                    timeout_seconds=5,))

        self.assertEqual(result["status"], "ok")
        self.assertNotIn("backtest_summary", result)
        # When the persisted summary is stale we cannot render markdown, so
        # no report file is written and ``report_path`` must NOT appear in
        # the payload (otherwise the agent would try to read a missing file).
        self.assertNotIn("report_path", result)
        next_steps = " ".join(result.get("next_steps") or [])
        self.assertIn("btjob-newer", next_steps)

    async def test_summary_attach_is_best_effort_and_never_breaks_ok(self) -> None:
        """If the summary service raises, the OK payload must still come back."""

        class _RaisingSummaryStub(_RunBacktestSummaryAwareStub):
            async def get_backtest_summary(self, run_id: str) -> dict:
                self.summary_calls.append(run_id)
                raise RuntimeError("transient db error")

        service = _RaisingSummaryStub(
            run_response={
                "run_id": "btjob-1",
                "task_id": "task-1",
                "status": "completed",
            },
            summary_response=None,
        )
        tool = RunStrategyBacktestTool(service)

        with _ReportsDirOverride():
            result = _payload(await tool.execute(task_id="task-1",
                    range_start="2026-04-01",
                    range_end="2026-05-15",
                    poll_interval_seconds=0,
                    timeout_seconds=5,))

        self.assertEqual(result["status"], "ok")
        # Summary fetch failed → no markdown → no report file → no
        # ``report_path`` advertised. The agent must rely on next_steps.
        self.assertNotIn("report_path", result)
        # Even when summary fetch failed we still surface the pointer so the
        # agent has a known recovery path.
        next_steps = result.get("next_steps") or []
        self.assertTrue(any("get_backtest_summary" in s for s in next_steps))


class ToolResultTruncationReplayTests(unittest.IsolatedAsyncioTestCase):
    """Regression: the original failure mode was the agent ignoring the
    summary because it lived inside a JSON tail that got chopped by
    ``micro_compact_messages`` at 4000 chars. The current design renders
    the summary as markdown in ``ToolResult.text`` *before* the JSON fence
    — the prose head is the most truncation-resistant slot. These tests
    lock both placements so the markdown body always reaches the model.
    """

    def _full_summary(self) -> dict:
        # Real-shape summary — schema mirrors what ``compute_summary`` /
        # ``summary_to_json`` produce after the metric extension landed.
        return {
            "schema_version": 1,
            "run_id": "btjob-replay-1",
            "backtest_job_id": "btjob-replay-1",
            "completed_at": "2026-05-15T08:00:00Z",
            "range_start_utc": "2026-04-15T00:00:00Z",
            "range_end_utc": "2026-05-15T00:00:00Z",
            "bar_interval": "1d",
            "starting_equity": "100000",
            "ending_equity": "107900.20",
            "return_pct": "7.9002",
            "annual_return_pct": "133.5421",
            "final_cash": "70025.44",
            "final_market_value": "37874.76",
            "sharpe": "1.4231",
            "sortino": "2.1054",
            "calmar": "51.4783",
            "volatility_annual_pct": "12.8743",
            "max_drawdown_pct": "2.594150",
            "max_drawdown_peak_equity": "110773.84",
            "max_drawdown_trough_equity": "107900.20",
            "max_drawdown_peak_at": "2026-05-14T07:00:00Z",
            "max_drawdown_trough_at": "2026-05-15T07:00:00Z",
            "fills_count": 1,
            "trade_count_closed": 0,
            "trade_count_open": 1,
            "win_rate": "0",
            "win_rate_sample_size": 0,
            "avg_holding_trading_days": "15",
            "avg_holding_sample_size": 1,
            "profit_factor": None,
            "avg_win_pnl": None,
            "avg_loss_pnl": None,
            "profit_loss_ratio": None,
            "max_consecutive_losses": 0,
            "by_symbol": [
                {
                    "symbol": "600522.SH",
                    "trade_count_closed": 0,
                    "pnl": "0",
                    "win_rate": "0",
                    "win_rate_sample_size": 0,
                    "avg_holding_trading_days": "15",
                }
            ],
            "final_positions": [
                {
                    "symbol": "600522.SH",
                    "name": None,
                    "quantity": 924,
                    "available": None,
                    "cost_price": "32.44",
                    "last_price": None,
                    "market_value": None,
                }
            ],
            "equity_curve_meta": {"downsampled": False, "raw_length": 20},
            # 20 points — same shape as the production payload that triggered
            # the regression.
            "equity_curve": [
                {"t": f"2026-04-{15 + (i % 15):02d}T07:00:00Z", "equity": f"{100000 + i * 500}"}
                for i in range(20)
            ],
        }

    async def test_tool_result_persists_report_and_keeps_inline_pointer(self) -> None:
        service = _RunBacktestSummaryAwareStub(
            run_response={
                "run_id": "btjob-replay-1",
                "task_id": "task-1",
                "status": "completed",
            },
            summary_response={
                "run": {
                    "run_id": "btjob-replay-1",
                    "task_id": "task-1",
                    "status": "completed",
                },
                "task_id": "task-1",
                "summary": self._full_summary(),
                "summary_state": "ok",
                "latest_summary_run_id": "btjob-replay-1",
            },
        )
        tool = RunStrategyBacktestTool(service)
        with _ReportsDirOverride() as reports_dir:
            result = await tool.execute(
                task_id="task-1",
                range_start="2026-04-15",
                range_end="2026-05-15",
                poll_interval_seconds=0,
                timeout_seconds=5,
            )

            decoded = _payload(result)
            # The bulky summary dict is no longer inlined into the JSON tail —
            # the markdown body lives on disk and is retrievable via
            # ``report_path``.
            self.assertNotIn("backtest_summary", decoded)
            expected_path = os.path.join(reports_dir, "btjob-replay-1.md")
            self.assertEqual(decoded.get("report_path"), expected_path)

            with open(expected_path, "r", encoding="utf-8") as fh:
                on_disk = fh.read()
            # Headline KPIs from the summary appear in the persisted markdown.
            for token in (
                "7.9002%",  # return_pct
                "133.5421%",  # annual_return_pct
                "Sharpe：1.4231",
                "Sortino：2.1054",
                "Calmar：51.4783",
            ):
                self.assertIn(token, on_disk, f"missing token {token!r} in persisted markdown")

            text = result.text
            # Inline text now points at the file rather than containing the
            # full markdown body — this is what protects the agent from
            # tool-result truncation.
            self.assertIn(expected_path, text)
            self.assertNotIn("## 回测报告", text)
            self.assertLess(len(text.encode("utf-8")), 1024)

    async def test_replay_through_4000_char_compactor_keeps_report_path(self) -> None:
        """End-to-end replay: the inline text now points at the on-disk
        report path instead of inlining the markdown body, so even an
        aggressive 4000-char compaction must keep ``report_path`` reachable
        for the agent to follow up with ``read_file``.
        """

        from doyoutrade.assistant.context_compaction.micro import (
            _compact_tool_content,
        )

        service = _RunBacktestSummaryAwareStub(
            run_response={
                "run_id": "btjob-replay-2",
                "task_id": "task-1",
                "status": "completed",
            },
            summary_response={
                "run": {
                    "run_id": "btjob-replay-2",
                    "task_id": "task-1",
                    "status": "completed",
                },
                "task_id": "task-1",
                "summary": self._full_summary(),
                "summary_state": "ok",
                "latest_summary_run_id": "btjob-replay-2",
            },
        )
        tool = RunStrategyBacktestTool(service)
        with _ReportsDirOverride() as reports_dir:
            raw = (
                await tool.execute(
                    task_id="task-1",
                    range_start="2026-04-15",
                    range_end="2026-05-15",
                    poll_interval_seconds=0,
                    timeout_seconds=5,
                )
            ).text
            expected_path = os.path.join(reports_dir, "btjob-replay-2.md")

            compacted = _compact_tool_content(raw, tool_result_max_chars=4000)

            # The on-disk path must survive compaction so the agent has a
            # known path forward. The prose explicitly directs the agent to
            # "Read" the path (read_file is the agent's only built-in
            # equivalent, so an explicit name is redundant).
            self.assertIn(expected_path, compacted)
            self.assertIn("Read", compacted)


class _DebugEventCapture:
    """Async context manager that records ``emit_debug_event`` calls fired
    inside ``run_tools``. Patches the symbol re-imported into
    ``doyoutrade.assistant.strategy_tools.run_tools`` so we observe exactly
    what the tool emits without depending on the global OTel exporter.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def _record(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, dict(payload)))

    def __enter__(self) -> "_DebugEventCapture":
        from unittest.mock import patch

        from doyoutrade.assistant.strategy_tools import run_tools as rt_module

        self._patcher = patch.object(rt_module, "emit_debug_event", self._record)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._patcher.stop()

    def by_name(self, name: str) -> list[dict]:
        return [payload for event, payload in self.events if event == name]


class RunStrategyBacktestReportPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """The on-disk report contract: when ``run_strategy_backtest`` succeeds
    and the persisted summary is ready, the rendered markdown is written to
    ``$DOYOUTRADE_REPORTS_DIR/<run_id>.md`` and the inline ``ToolResult.text``
    only points at the file. This guarantees the agent can always read the
    full report via ``read_file`` regardless of tool-result truncation.
    """

    async def test_report_written_under_env_var_override(self) -> None:
        service = _RunBacktestSummaryAwareStub(
            run_response={
                "run_id": "btjob-env-override",
                "task_id": "task-1",
                "status": "completed",
            },
            summary_response={
                "run": {"run_id": "btjob-env-override", "task_id": "task-1", "status": "completed"},
                "task_id": "task-1",
                "summary": dict(_SUMMARY_PAYLOAD, run_id="btjob-env-override"),
                "summary_state": "ok",
                "latest_summary_run_id": "btjob-env-override",
            },
        )
        tool = RunStrategyBacktestTool(service)

        with _ReportsDirOverride() as reports_dir:
            with _DebugEventCapture() as cap:
                raw = await tool.execute(
                    task_id="task-1",
                    range_start="2026-04-01",
                    range_end="2026-05-15",
                    poll_interval_seconds=0,
                    timeout_seconds=5,
                )

            decoded = _payload(raw)
            expected_path = os.path.join(reports_dir, "btjob-env-override.md")

            # report_path lives in the JSON payload …
            self.assertEqual(decoded.get("report_path"), expected_path)
            # … and the file actually exists on disk with the markdown body.
            self.assertTrue(os.path.exists(expected_path))
            with open(expected_path, "r", encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("## 回测报告 · `btjob-env-override`", body)

            # Inline ToolResult.text stays under 1 KB (no markdown body).
            self.assertLess(len(raw.text.encode("utf-8")), 1024)

            # The persistence-success debug event fires with the expected keys.
            persisted_events = cap.by_name("backtest_run_report_persisted")
            self.assertEqual(len(persisted_events), 1)
            event = persisted_events[0]
            self.assertEqual(event["run_id"], "btjob-env-override")
            self.assertEqual(event["report_path"], expected_path)
            self.assertGreater(int(event["byte_size"]), 0)
            self.assertEqual(int(event["byte_size"]), len(body.encode("utf-8")))
            # No failure events should fire on the happy path.
            self.assertEqual(cap.by_name("backtest_run_report_persist_failed"), [])

    async def test_unwritable_reports_dir_falls_back_to_inline_markdown(self) -> None:
        """If the reports dir cannot be created (e.g. parent is a regular
        file), we emit ``backtest_run_report_persist_failed``, omit
        ``report_path`` from the payload, and fall back to inlining the
        markdown body so the agent doesn't silently lose the report.
        """

        service = _RunBacktestSummaryAwareStub(
            run_response={
                "run_id": "btjob-unwritable",
                "task_id": "task-1",
                "status": "completed",
            },
            summary_response={
                "run": {"run_id": "btjob-unwritable", "task_id": "task-1", "status": "completed"},
                "task_id": "task-1",
                "summary": dict(_SUMMARY_PAYLOAD, run_id="btjob-unwritable"),
                "summary_state": "ok",
                "latest_summary_run_id": "btjob-unwritable",
            },
        )
        tool = RunStrategyBacktestTool(service)

        # Stage a sandbox tempdir containing a regular file; use a *child*
        # path under that file as the override so ``mkdir(parents=True)``
        # raises ``NotADirectoryError`` (a subclass of ``OSError``).
        with tempfile.TemporaryDirectory(prefix="doyoutrade-reports-fail-") as parent:
            blocker_path = os.path.join(parent, "not-a-dir")
            with open(blocker_path, "w", encoding="utf-8") as fh:
                fh.write("blocker")
            unwritable_path = os.path.join(blocker_path, "reports")
            with _ReportsDirOverride(override=unwritable_path):
                with _DebugEventCapture() as cap:
                    raw = await tool.execute(
                        task_id="task-1",
                        range_start="2026-04-01",
                        range_end="2026-05-15",
                        poll_interval_seconds=0,
                        timeout_seconds=5,
                    )

        decoded = _payload(raw)
        # Failure path: no report_path advertised in the payload (otherwise
        # the agent would dereference a missing file).
        self.assertNotIn("report_path", decoded)
        # … and the markdown body is inlined back into ToolResult.text so
        # the report still reaches the model.
        self.assertIn("## 回测报告", raw.text)

        # The structured failure event fires with type + message + run_id.
        failed_events = cap.by_name("backtest_run_report_persist_failed")
        self.assertGreaterEqual(len(failed_events), 1)
        failure = failed_events[-1]
        self.assertEqual(failure["run_id"], "btjob-unwritable")
        self.assertIn("error_type", failure)
        self.assertIn("message", failure)
        self.assertIn("hint", failure)
        # Sanity: success event must NOT have fired.
        self.assertEqual(cap.by_name("backtest_run_report_persisted"), [])

    async def test_unsafe_run_id_falls_back_to_timestamped_path(self) -> None:
        """A ``run_id`` containing path separators is unsafe — the helper
        substitutes a timestamped filename so the report still lands on
        disk, and emits a structured ``backtest_run_report_persist_failed``
        event so the gap is visible.
        """

        unsafe_run_id = "btjob/../escape"
        service = _RunBacktestSummaryAwareStub(
            run_response={
                "run_id": unsafe_run_id,
                "task_id": "task-1",
                "status": "completed",
            },
            summary_response={
                "run": {"run_id": unsafe_run_id, "task_id": "task-1", "status": "completed"},
                "task_id": "task-1",
                "summary": dict(_SUMMARY_PAYLOAD, run_id=unsafe_run_id),
                "summary_state": "ok",
                "latest_summary_run_id": unsafe_run_id,
            },
        )
        tool = RunStrategyBacktestTool(service)

        with _ReportsDirOverride() as reports_dir:
            with _DebugEventCapture() as cap:
                raw = await tool.execute(
                    task_id="task-1",
                    range_start="2026-04-01",
                    range_end="2026-05-15",
                    poll_interval_seconds=0,
                    timeout_seconds=5,
                )

        decoded = _payload(raw)
        report_path = decoded.get("report_path")
        self.assertIsInstance(report_path, str)
        # The fallback filename starts with "backtest-report-" and lives
        # directly under the configured reports dir (not under any path
        # segment the malicious run_id could have injected).
        self.assertTrue(report_path.startswith(reports_dir + os.sep))
        self.assertIn("backtest-report-", os.path.basename(report_path))

        # An unsafe-run-id event must have fired with reason set.
        failed_events = cap.by_name("backtest_run_report_persist_failed")
        self.assertTrue(
            any(evt.get("reason") == "unsafe_or_missing_run_id" for evt in failed_events),
            f"expected unsafe_or_missing_run_id event, got {failed_events!r}",
        )

    async def test_payload_orders_report_path_before_backtest_job(self) -> None:
        """``report_path`` must appear in the serialized JSON payload BEFORE
        ``backtest_job``, and bulk fields like ``ledger_checkpoint_json``
        must be stripped from ``backtest_job``. Together this guarantees
        the bash-tool's per-result truncation can't bury ``report_path``
        behind the worker's end-of-run ledger snapshot — the regression
        observed in session ``asst-f9826c84c5fd``.
        """

        bulky_ledger = {
            "symbol_to_price": {f"60{i:04d}.SH": "8.96" for i in range(200)},
            "cash": "999.99",
            "positions": [{"symbol": f"60{i:04d}.SH", "qty": 100} for i in range(200)],
        }
        service = _RunBacktestSummaryAwareStub(
            run_response={
                "run_id": "btjob-order-check",
                "task_id": "task-1",
                "status": "completed",
                "starting_equity": 100000.0,
                "ending_equity": 128616.25,
                "return_pct": 28.62,
                "ledger_checkpoint_json": bulky_ledger,
                "config_snapshot_json": {"strategy": "macd", "irrelevant_bulk": "x" * 500},
            },
            summary_response={
                "run": {"run_id": "btjob-order-check", "task_id": "task-1", "status": "completed"},
                "task_id": "task-1",
                "summary": dict(_SUMMARY_PAYLOAD, run_id="btjob-order-check"),
                "summary_state": "ok",
                "latest_summary_run_id": "btjob-order-check",
            },
        )
        tool = RunStrategyBacktestTool(service)

        with _ReportsDirOverride():
            raw = await tool.execute(
                task_id="task-1",
                range_start="2026-04-01",
                range_end="2026-05-15",
                poll_interval_seconds=0,
                timeout_seconds=5,
            )

        decoded = _payload(raw)

        # 1. ``report_path`` is present and points at an absolute path.
        report_path = decoded.get("report_path")
        self.assertIsInstance(report_path, str)
        self.assertTrue(report_path.endswith("btjob-order-check.md"))

        # 2. Ordering: ``report_path`` lands before ``backtest_job`` in dict
        # insertion order, which is what JSON serializers preserve.
        keys = list(decoded.keys())
        self.assertIn("report_path", keys)
        self.assertIn("backtest_job", keys)
        self.assertLess(
            keys.index("report_path"),
            keys.index("backtest_job"),
            msg=(
                "report_path must appear before backtest_job in the envelope "
                "so the bash-tool truncation can't bury it behind bulk fields"
            ),
        )

        # 3. ``backtest_job`` carries identifiers only — KPIs (return_pct /
        # equity / etc.) and bulk fields (ledger / config snapshot) live in
        # the on-disk markdown report referenced by ``report_path``. The
        # agent reads the report for details; the envelope keeps its
        # smallest possible footprint so ``report_path`` always survives
        # the bash-tool truncation boundary.
        job = decoded["backtest_job"]
        self.assertNotIn("ledger_checkpoint_json", job)
        self.assertNotIn("config_snapshot_json", job)
        self.assertNotIn("return_pct", job)
        self.assertNotIn("starting_equity", job)
        self.assertNotIn("ending_equity", job)
        self.assertEqual(job.get("run_id"), "btjob-order-check")
        self.assertEqual(job.get("status"), "completed")

        # 4. Same check on the actual stdout-shape JSON: re-serialize and
        # verify the report_path substring precedes backtest_job substring
        # so even a naive byte-offset truncation works.
        serialized = json.dumps(decoded, ensure_ascii=False)
        self.assertLess(
            serialized.index('"report_path"'),
            serialized.index('"backtest_job"'),
        )


class WarmupInsufficientTests(unittest.IsolatedAsyncioTestCase):
    """Diagnostic ``startup_history`` / ``bars_total`` fields must still
    round-trip through the assistant-tool surface (markdown report and
    JSON envelope), but the dedicated ``warmup_insufficient`` anomaly
    text is gone: its predicate (``bars_total < startup_history``)
    mistook the user's report-window length for a preload failure even
    though the data layer feeds the strategy ``startup_history`` bars
    per cycle. Zero-trade runs now surface the generic "零交易" hint;
    the truthful preload-failure signal is the SDK runner's per-cycle
    ``strategy_base_history_insufficient`` debug event.
    """

    def _warmup_summary(self, run_id: str) -> dict:
        # Mirror the asst-c392d04c94d2 session's shape: startup_history=50,
        # bars_total=19 (30-day range over a sparse trading-day window),
        # zero trades, zero open positions.
        return {
            "schema_version": 1,
            "run_id": run_id,
            "completed_at": "2026-05-15T08:00:00Z",
            "range_start_utc": "2026-04-15T00:00:00Z",
            "range_end_utc": "2026-05-15T00:00:00Z",
            "bar_interval": "1d",
            "startup_history": 50,
            "bars_total": 19,
            "starting_equity": "100000",
            "ending_equity": "100000",
            "return_pct": "0.00",
            "final_cash": "100000",
            "final_market_value": "0",
            "final_positions": [],
            "trade_count_closed": 0,
            "trade_count_open": 0,
            "fills_count": 0,
            "win_rate": "0",
            "win_rate_sample_size": 0,
            "avg_holding_trading_days": "0",
            "avg_holding_sample_size": 0,
            "max_drawdown_pct": "0",
            "max_drawdown_peak_equity": "100000",
            "max_drawdown_trough_equity": "100000",
            "max_drawdown_peak_at": None,
            "max_drawdown_trough_at": None,
            "equity_curve_meta": {"downsampled": False, "raw_length": 19},
            "equity_curve": [],
        }

    async def test_get_summary_markdown_falls_back_to_zero_trade_hint(self) -> None:
        run_id = "btjob-warmup-1"
        service = _SummaryServiceStub(
            responses={
                run_id: {
                    "run": {"run_id": run_id, "task_id": "task-1", "status": "completed"},
                    "task_id": "task-1",
                    "summary": self._warmup_summary(run_id),
                    "summary_state": "ok",
                    "latest_summary_run_id": run_id,
                }
            }
        )
        tool = GetBacktestSummaryTool(service)
        result = await tool.execute(run_id=run_id)

        self.assertFalse(result.is_error)
        self.assertIn("### 异常信号", result.text)
        # The misleading warmup hint is gone — see the WarmupInsufficientTests
        # docstring for the rationale.
        self.assertNotIn("warmup_insufficient", result.text)
        # The generic zero-trade hint is the correct guidance when the
        # strategy ran with sufficient warmup but never crossed a signal
        # threshold.
        self.assertIn("零交易", result.text)

    async def test_get_summary_json_format_keeps_warmup_fields(self) -> None:
        run_id = "btjob-warmup-json"
        service = _SummaryServiceStub(
            responses={
                run_id: {
                    "run": {"run_id": run_id, "task_id": "task-1", "status": "completed"},
                    "task_id": "task-1",
                    "summary": self._warmup_summary(run_id),
                    "summary_state": "ok",
                    "latest_summary_run_id": run_id,
                }
            }
        )
        tool = GetBacktestSummaryTool(service)
        result = await tool.execute(run_id=run_id, format="json")

        self.assertFalse(result.is_error)
        # New diagnostic fields land in the JSON wire so frontend / agent
        # consumers don't have to re-derive them from the markdown body.
        self.assertIn('"startup_history": 50', result.text)
        self.assertIn('"bars_total": 19', result.text)

    async def test_run_strategy_backtest_persists_report_with_zero_trade_hint(self) -> None:
        run_id = "btjob-warmup-run"
        service = _RunBacktestSummaryAwareStub(
            run_response={
                "run_id": run_id,
                "task_id": "task-1",
                "status": "completed",
            },
            summary_response={
                "run": {"run_id": run_id, "task_id": "task-1", "status": "completed"},
                "task_id": "task-1",
                "summary": self._warmup_summary(run_id),
                "summary_state": "ok",
                "latest_summary_run_id": run_id,
            },
        )
        tool = RunStrategyBacktestTool(service)

        with _ReportsDirOverride() as reports_dir:
            result = await tool.execute(
                task_id="task-1",
                range_start="2026-04-15",
                range_end="2026-05-15",
                poll_interval_seconds=0,
                timeout_seconds=5,
            )

            decoded = _payload(result)
            report_path = os.path.join(reports_dir, f"{run_id}.md")
            self.assertEqual(decoded.get("report_path"), report_path)
            with open(report_path, "r", encoding="utf-8") as fh:
                body = fh.read()
            # The misleading warmup hint is gone — the persisted report
            # surfaces the generic zero-trade hint instead. See the
            # WarmupInsufficientTests docstring for the rationale.
            self.assertNotIn("warmup_insufficient", body)
            self.assertIn("零交易", body)


if __name__ == "__main__":
    unittest.main()
