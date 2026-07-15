"""End-to-end subprocess tests for ``doyoutrade-cli``.

These tests spawn the real CLI binary so they exercise the same path
the assistant agent hits via ``execute_bash`` — including click
argument parsing, envelope writing to stdout, exit code propagation,
and the stderr ready/exit markers for streaming commands.

Heavy operations (DB bootstrap, alembic migration check) make each
test ~1-2s. The suite is scoped tight; broader coverage lives in the
in-process unit tests.

Skipped automatically when ``doyoutrade-cli`` isn't on ``$PATH`` — keeps
``make test`` green on a fresh checkout that hasn't run ``uv sync`` yet.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest


_CLI = shutil.which("doyoutrade-cli")


@unittest.skipIf(_CLI is None, "doyoutrade-cli not on PATH; skipping subprocess suite")
class CliSubprocessTests(unittest.TestCase):
    def _run_cli(
        self,
        *args: str,
        env_overrides: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> "subprocess.CompletedProcess[str]":
        assert _CLI is not None  # skip-decorator narrows this for the test runner
        env = os.environ.copy()
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [_CLI, *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )

    # ------------------------------------------------------------------
    # schema — no bootstrap; fastest path
    # ------------------------------------------------------------------

    def test_schema_task_get_returns_tool_metadata(self) -> None:
        result = self._run_cli("schema", "task.get")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["tool_name"], "get_task")
        self.assertIn("parameters", payload["data"])

    def test_schema_unknown_command_returns_exit_2(self) -> None:
        result = self._run_cli("schema", "task.nonexistent")

        self.assertEqual(result.returncode, 2, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "unknown_command")

    def test_assistant_help_is_registered_without_server(self) -> None:
        result = self._run_cli("assistant", "--help")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Assistant chat validation commands", result.stdout)
        self.assertIn("run", result.stdout)
        self.assertIn("export", result.stdout)

    # ------------------------------------------------------------------
    # task get — bootstrap + identifier-guard rejection
    # ------------------------------------------------------------------

    def test_task_get_with_sd_prefix_returns_wrong_identifier_type(self) -> None:
        result = self._run_cli("task", "get", "sd-not-a-task")

        self.assertEqual(result.returncode, 2, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "wrong_identifier_type")

    # ------------------------------------------------------------------
    # env propagation — agent_id / session_id surface in envelope.meta
    # ------------------------------------------------------------------

    def test_env_vars_surface_in_envelope_meta(self) -> None:
        result = self._run_cli(
            "task",
            "get",
            "sd-not-a-task",
            env_overrides={
                "DOYOUTRADE_AGENT_ID": "asst-integration",
                "DOYOUTRADE_SESSION_ID": "sess-integration",
                "DOYOUTRADE_DEBUG_SESSION_ID": "sess-integration",
            },
        )

        # Exit code is the identifier-guard rejection; meta should still be present.
        self.assertEqual(result.returncode, 2, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["meta"]["agent_id"], "asst-integration")
        self.assertEqual(payload["meta"]["session_id"], "sess-integration")
        self.assertEqual(payload["meta"]["debug_session_id"], "sess-integration")

    def test_no_debug_session_flag_drops_meta_block(self) -> None:
        result = self._run_cli(
            "--no-debug-session",
            "task",
            "get",
            "sd-not-a-task",
            env_overrides={
                "DOYOUTRADE_AGENT_ID": "asst-x",
                "DOYOUTRADE_SESSION_ID": "sess-x",
                "DOYOUTRADE_DEBUG_SESSION_ID": "sess-x",
            },
        )

        self.assertEqual(result.returncode, 2, msg=result.stderr)
        payload = json.loads(result.stdout)
        # agent_id / session_id remain (we only drop debug_session_id and run_id).
        self.assertEqual(payload["meta"]["agent_id"], "asst-x")
        self.assertNotIn("debug_session_id", payload["meta"])

    # ------------------------------------------------------------------
    # backtest watch — streaming contract
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # stock screen — CLI parses flags + reads universe file + surfaces
    # api_unavailable when the server is down (matches every other
    # invoke_api-backed command's behaviour). Doesn't verify match logic
    # — that's covered by tests.test_stock_screen.
    # ------------------------------------------------------------------

    def test_stock_screen_help_lists_condition_flags(self) -> None:
        result = self._run_cli("stock", "screen", "--help")

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # Each condition family must surface in --help so an agent
        # generating CLI invocations can self-discover what's available.
        for flag in (
            "--universe-file",
            "--asof",
            "--rsi-max",
            "--patterns",
            "--ma-cross",
            "--pct-change-lookback",
            "--volume-ratio-lookback",
            "--bollinger",
            "--adx-min",
            "--macd",
            "--kdj",
            "--cci-min",
            "--williams-min",
            "--keltner",
            "--donchian",
            "--cmf-min",
            "--roc-min",
            "--top-k",
            "--sort-by",
        ):
            self.assertIn(flag, result.stdout, msg=f"missing flag in --help: {flag}")

    def test_stock_screen_missing_universe_file_is_validation_error(self) -> None:
        # No --universe-file flag → click ``missing_parameter``, exit 2.
        result = self._run_cli(
            "stock",
            "screen",
            "--rsi-max",
            "30",
        )
        self.assertEqual(result.returncode, 2, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "missing_parameter")

    def test_stock_screen_against_dead_api_returns_api_unavailable(self) -> None:
        # Write a one-symbol universe file in a temp dir; the body never
        # hits the operation because the (dead) API rejects first.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            uni = os.path.join(tmp, "u.txt")
            with open(uni, "w", encoding="utf-8") as fh:
                fh.write("600000.SH\n")
            result = self._run_cli(
                "stock",
                "screen",
                "--universe-file",
                uni,
                "--rsi-max",
                "30",
                env_overrides={"DOYOUTRADE_API_URL": "http://127.0.0.1:9"},
            )

        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "api_unavailable")

    def test_backtest_watch_emits_ready_and_exit_markers(self) -> None:
        result = self._run_cli(
            "backtest",
            "watch",
            "run-nonexistent-zzz",
            "--max-events",
            "1",
            "--timeout",
            "5",
            env_overrides={"DOYOUTRADE_API_URL": "http://127.0.0.1:9"},
            timeout=20.0,
        )

        # Watch commands always exit 0 by contract.
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # Ready marker on stderr — stable contract line.
        self.assertIn("[doyoutrade] ready kind=backtest_watch run_id=run-nonexistent-zzz", result.stderr)
        # Exit marker on stderr — includes reason.
        self.assertIn("[doyoutrade] exited", result.stderr)
        self.assertRegex(result.stderr, r"reason: (limit|terminal|signal|timeout)")
        # At least one NDJSON envelope on stdout.
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 1)
        envelope = json.loads(lines[0])
        # Without a running API, business commands surface a structured
        # transport error rather than bootstrapping local runtime.
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "api_unavailable")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
