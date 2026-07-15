"""Tests for ``doyoutrade-cli backtest run`` progress-bar support.

Two layers:

* the pure ``_progress`` helpers (formatting / TTY gating / reporter I/O),
  which need no HTTP at all;
* the command's progress path, which fires the run fire-and-forget and
  client-polls the summary endpoint — asserted via an ``httpx.MockTransport``
  handler so we see the exact request sequence and the reconstructed stdout
  envelope.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from typing import Any, Callable
from unittest.mock import patch

import httpx
from click.testing import CliRunner

from doyoutrade.cli._envelope import EXIT_OK
from doyoutrade.cli._progress import (
    ProgressReporter,
    render_progress_line,
    should_show_progress,
)
from doyoutrade.cli.commands.backtest_runs import backtest as backtest_group


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_async_client(handler: Callable[[httpx.Request], httpx.Response]):
    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return patch("doyoutrade.cli._api.httpx.AsyncClient", factory)


class TestProgressHelpers(unittest.TestCase):
    def test_render_basic_percentage(self) -> None:
        line = render_progress_line(135, 300, "running")
        self.assertIn(" 45% 135/300 bars running", line)
        self.assertTrue(line.startswith("["))

    def test_render_preparing_when_total_unknown(self) -> None:
        line = render_progress_line(0, 0, None)
        self.assertIn("(preparing)", line)
        self.assertIn("running", line)  # default label

    def test_render_clamps_overflow(self) -> None:
        line = render_progress_line(999, 300, "running")
        self.assertIn("100% 300/300", line)

    def test_render_full_bar_completed(self) -> None:
        line = render_progress_line(300, 300, "completed")
        self.assertIn("100% 300/300 bars completed", line)
        self.assertNotIn("░", line)

    def test_should_show_progress_explicit_overrides_tty(self) -> None:
        buf = io.StringIO()  # isatty() -> False
        self.assertTrue(should_show_progress(True, stream=buf))
        self.assertFalse(should_show_progress(False, stream=buf))

    def test_should_show_progress_auto_off_for_non_tty(self) -> None:
        self.assertFalse(should_show_progress(None, stream=io.StringIO()))

    def test_should_show_progress_auto_on_for_tty(self) -> None:
        class _Tty(io.StringIO):
            def isatty(self) -> bool:
                return True

        self.assertTrue(should_show_progress(None, stream=_Tty()))

    def test_reporter_disabled_writes_nothing(self) -> None:
        buf = io.StringIO()
        reporter = ProgressReporter(enabled=False, stream=buf)
        reporter.update(10, 100, "running")
        reporter.close()
        self.assertEqual(buf.getvalue(), "")

    def test_reporter_dedups_and_terminates_with_newline(self) -> None:
        buf = io.StringIO()
        reporter = ProgressReporter(enabled=True, stream=buf)
        reporter.update(10, 100, "running")
        first = buf.getvalue()
        reporter.update(10, 100, "running")  # identical -> no new write
        self.assertEqual(buf.getvalue(), first)
        reporter.update(50, 100, "running")  # changed -> writes again
        self.assertGreater(len(buf.getvalue()), len(first))
        reporter.close()
        self.assertTrue(buf.getvalue().endswith("\n"))

    def test_reporter_close_without_draw_is_silent(self) -> None:
        buf = io.StringIO()
        reporter = ProgressReporter(enabled=True, stream=buf)
        reporter.close()
        self.assertEqual(buf.getvalue(), "")


class TestBacktestRunProgressPath(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_progress_path_fires_forget_then_polls_summary(self) -> None:
        requests: list[httpx.Request] = []
        poll_state = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(
                    201,
                    json={
                        "status": "pending",
                        "run_id": "btjob-1",
                        "task_id": "task-9",
                        "auto_created_task_id": "task-9",
                    },
                )
            # GET summary — running on first poll, completed thereafter.
            poll_state["n"] += 1
            if poll_state["n"] < 2:
                run = {
                    "run_id": "btjob-1",
                    "task_id": "task-9",
                    "status": "running",
                    "bars_completed": 40,
                    "bars_total": 100,
                }
                return httpx.Response(200, json={"run": run, "summary_state": "missing"})
            run = {
                "run_id": "btjob-1",
                "task_id": "task-9",
                "status": "completed",
                "bars_completed": 100,
                "bars_total": 100,
            }
            return httpx.Response(
                200,
                json={
                    "run": run,
                    "summary_state": "ok",
                    "summary": {"open_positions": 0, "return_pct": 1.5},
                },
            )

        with patch.dict(os.environ, {"DOYOUTRADE_API_URL": "http://test.local"}, clear=False):
            with _patch_async_client(handler):
                result = self.runner.invoke(
                    backtest_group,
                    [
                        "run",
                        "--definition",
                        "sd-1",
                        "--range-start",
                        "2026-03-24",
                        "--range-end",
                        "2026-05-24",
                        "--universe",
                        "300058.SZ",
                        "--timeout",
                        "5",
                        "--poll-interval",
                        "0",
                        "--progress",
                    ],
                    obj={"fmt": "json"},
                    catch_exceptions=False,
                )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)

        # First call is the fire-and-forget POST with timeout_seconds forced to 0.
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(str(requests[0].url), "http://test.local/backtest-runs")
        self.assertEqual(json.loads(requests[0].content)["timeout_seconds"], 0)

        # Subsequent calls poll the summary endpoint.
        self.assertGreaterEqual(len(requests), 2)
        self.assertEqual(requests[1].method, "GET")
        self.assertIn("/backtest-runs/btjob-1/summary", str(requests[1].url))

        # stdout envelope mirrors the blocking-POST shape (status/run_id/task_id/summary).
        envelope = json.loads(result.stdout)
        self.assertTrue(envelope["ok"])
        data = envelope["data"]
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["run_id"], "btjob-1")
        self.assertEqual(data["task_id"], "task-9")
        self.assertEqual(data["auto_created_task_id"], "task-9")
        self.assertEqual(data["summary"], {"open_positions": 0, "return_pct": 1.5})

    def test_progress_poll_failure_is_surfaced_not_swallowed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    201,
                    json={"status": "pending", "run_id": "btjob-1", "task_id": "task-9"},
                )
            return httpx.Response(404, json={"detail": "summary gone"})

        with patch.dict(os.environ, {"DOYOUTRADE_API_URL": "http://test.local"}, clear=False):
            with _patch_async_client(handler):
                result = self.runner.invoke(
                    backtest_group,
                    [
                        "run",
                        "--definition",
                        "sd-1",
                        "--range-start",
                        "2026-03-24",
                        "--range-end",
                        "2026-05-24",
                        "--universe",
                        "300058.SZ",
                        "--progress",
                    ],
                    obj={"fmt": "json"},
                    catch_exceptions=False,
                )

        envelope = json.loads(result.stdout)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "backtest_run_not_found")

    def test_no_progress_keeps_single_blocking_post(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                201,
                json={"status": "completed", "run_id": "btjob-1", "task_id": "task-9"},
            )

        with patch.dict(os.environ, {"DOYOUTRADE_API_URL": "http://test.local"}, clear=False):
            with _patch_async_client(handler):
                result = self.runner.invoke(
                    backtest_group,
                    [
                        "run",
                        "--definition",
                        "sd-1",
                        "--range-start",
                        "2026-03-24",
                        "--range-end",
                        "2026-05-24",
                        "--universe",
                        "300058.SZ",
                        "--timeout",
                        "120",
                        "--no-progress",
                    ],
                    obj={"fmt": "json"},
                    catch_exceptions=False,
                )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        # Exactly one blocking POST; no client-side summary polling.
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(json.loads(requests[0].content)["timeout_seconds"], 120.0)


if __name__ == "__main__":
    unittest.main()
