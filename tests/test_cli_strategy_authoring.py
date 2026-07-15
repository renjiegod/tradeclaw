"""Integration tests for ``doyoutrade-cli strategy authoring`` lifecycle subcommands.

These tests drive the 4 ``strategy authoring`` lifecycle commands through
Click's ``CliRunner`` and a stubbed ``invoke_api`` so we do not pay
HTTP / runtime cost.  They verify:

1. The lifecycle commands exist and are wired up correctly.
2. Each command calls the expected OpenAPI path and request shape.
3. The envelope flows through unchanged to stdout.

File primitives (read_file / write_file / edit_file / list_files) are
in-process agent tools — they are NOT CLI subcommands and are tested in
``tests/test_file_tools_sandbox.py``.
"""
from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from doyoutrade.cli.commands.strategy import strategy as strategy_group
from doyoutrade.cli._envelope import EXIT_OK


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _ok_envelope(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    return {"ok": True, "data": data}, EXIT_OK


def _fake_invoke_api_factory(
    expected_method: str,
    expected_path: str,
    expected_json: dict[str, Any] | None,
    response_data: dict[str, Any],
):
    """Return an async mock that asserts OpenAPI method/path/body and returns response."""

    async def _fake(method: str, path: str, *, json=None, params=None, meta=None, **kwargs):
        assert method == expected_method, f"expected {expected_method!r}, got {method!r}"
        assert path == expected_path, f"expected {expected_path!r}, got {path!r}"
        assert json == expected_json, f"expected {expected_json!r}, got {json!r}"
        return _ok_envelope(response_data)

    return _fake


class StrategyAuthoringLifecycleCommandTests(unittest.TestCase):
    """Drive ``strategy authoring open|cancel|compile|finalize`` via Click's CliRunner."""

    def setUp(self) -> None:
        self.runner = CliRunner()

    def _run(self, args: list[str], env: dict[str, str] | None = None):
        return self.runner.invoke(
            strategy_group,
            args,
            obj={"fmt": "json"},
            env=env,
            catch_exceptions=False,
        )

    # ------------------------------------------------------------------
    # open
    # ------------------------------------------------------------------

    def test_open_new_definition_passes_name(self) -> None:
        fake = _fake_invoke_api_factory(
            "POST",
            "/strategy-authoring/sessions",
            {"name": "MyStrategy"},
            {"status": "created", "definition_id": "sd-abc", "session_id": "sess-xyz"},
        )
        with patch("doyoutrade.cli.commands.strategy_authoring.invoke_api", fake):
            result = self._run(["authoring", "open", "--name", "MyStrategy"])

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["session_id"], "sess-xyz")

    def test_open_existing_definition_passes_both_kwargs(self) -> None:
        fake = _fake_invoke_api_factory(
            "POST",
            "/strategy-authoring/sessions",
            {"definition_id": "sd-existing", "name": "Copy"},
            {"status": "ok", "definition_id": "sd-existing", "session_id": "sess-copy"},
        )
        with patch("doyoutrade.cli.commands.strategy_authoring.invoke_api", fake):
            result = self._run([
                "authoring", "open",
                "--definition-id", "sd-existing",
                "--name", "Copy",
            ])

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"])

    # ------------------------------------------------------------------
    # cancel
    # ------------------------------------------------------------------

    def test_cancel_passes_session_id(self) -> None:
        fake = _fake_invoke_api_factory(
            "DELETE",
            "/strategy-authoring/sessions/sess-123",
            None,
            {"status": "ok", "session_id": "sess-123"},
        )
        with patch("doyoutrade.cli.commands.strategy_authoring.invoke_api", fake):
            result = self._run(["authoring", "cancel", "--session-id", "sess-123"])

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)

    # ------------------------------------------------------------------
    # compile
    # ------------------------------------------------------------------

    def test_compile_passes_session_id(self) -> None:
        fake = _fake_invoke_api_factory(
            "POST",
            "/strategy-authoring/sessions/sess-456/compile",
            None,
            {"status": "ok", "session_id": "sess-456", "class_name": "Strategy"},
        )
        with patch("doyoutrade.cli.commands.strategy_authoring.invoke_api", fake):
            result = self._run(["authoring", "compile", "--session-id", "sess-456"])

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)

    # ------------------------------------------------------------------
    # finalize
    # ------------------------------------------------------------------

    def test_finalize_passes_session_id(self) -> None:
        fake = _fake_invoke_api_factory(
            "POST",
            "/strategy-authoring/sessions/sess-789/finalize",
            None,
            {"status": "ok", "version_label": "v0001-abc", "definition_id": "sd-new"},
        )
        with patch("doyoutrade.cli.commands.strategy_authoring.invoke_api", fake):
            result = self._run(["authoring", "finalize", "--session-id", "sess-789"])

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertEqual(envelope["data"]["version_label"], "v0001-abc")

    # ------------------------------------------------------------------
    # Registration / wiring sanity check
    # ------------------------------------------------------------------

    def test_authoring_help_exists(self) -> None:
        """Ensure ``strategy authoring --help`` works (wiring sanity check)."""
        result = self._run(["authoring", "--help"])
        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertIn("authoring", result.output.lower())

    def test_all_lifecycle_subcommands_in_help(self) -> None:
        """All 4 lifecycle subcommands must appear in ``strategy authoring --help``."""
        result = self._run(["authoring", "--help"])
        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        for cmd in ("open", "cancel", "compile", "finalize"):
            self.assertIn(cmd, result.output, msg=f"subcommand {cmd!r} missing from help")

    def test_file_subcommands_not_in_help(self) -> None:
        """File ops (read/write/edit/list) must NOT appear as CLI subcommands."""
        result = self._run(["authoring", "--help"])
        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        # These are now in-process tools, not CLI subcommands.
        for cmd in ("read", "write", "edit", "list"):
            self.assertNotIn(
                f"  {cmd}",
                result.output,
                msg=f"file subcommand {cmd!r} must NOT be in CLI help",
            )


if __name__ == "__main__":
    unittest.main()
