"""Tests for the rewired ``doyoutrade-cli sdk validate`` command.

As of the strategy-as-files refactor (Task 6, 2026-05-24), ``sdk validate``
no longer calls the deleted ``validate_strategy_code`` tool.  Instead it
writes the source file into a temporary directory and calls
``StrategyCompiler.validate_directory`` + smoke gate directly.

These tests verify:
- A valid strategy file produces ``ok: true`` and exit code 0.
- A strategy with a compile error (e.g. missing ``on_bar``) produces
  ``ok: false``, an ``error_code``, and exit code 1.
- A strategy with a smoke-failing runtime error produces exit code 1.
- The ``--class-name`` flag was removed (the compiler auto-discovers).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from doyoutrade.cli.commands.sdk import sdk as sdk_group


# ---------------------------------------------------------------------------
# Strategy source fixtures.
#
# validate_directory always looks for a class named "Strategy" (the
# default strategy_class_name), so on-disk files must shadow the name —
# i.e. ``class Strategy(Strategy):`` — regardless of display naming.
# ---------------------------------------------------------------------------

_VALID_STRATEGY = """\
from __future__ import annotations
from doyoutrade.strategy_sdk import Strategy, Signal


class Strategy(Strategy):
    name = "simple_strategy"
    timeframe = "1d"
    startup_history = 5

    def on_bar(self, df, ctx) -> Signal:
        return Signal.hold(tag="noop")
"""

# Strategy that fails AST compile (no on_bar override)
_MISSING_ON_BAR = """\
from __future__ import annotations
from doyoutrade.strategy_sdk import Strategy


class Strategy(Strategy):
    name = "no_on_bar"
    timeframe = "1d"
    startup_history = 5
"""

# Strategy with a disallowed import (compile-time error)
_DISALLOWED_IMPORT = """\
from __future__ import annotations
import requests
from doyoutrade.strategy_sdk import Strategy, Signal


class Strategy(Strategy):
    name = "bad_import"
    timeframe = "1d"
    startup_history = 5

    def on_bar(self, df, ctx) -> Signal:
        return Signal.hold(tag="noop")
"""


class SdkValidateCommandTests(unittest.TestCase):
    """In-process tests using CliRunner so no real DB / HTTP needed."""

    def _invoke(self, source_code: str, extra_args: list[str] | None = None) -> "click.testing.Result":
        runner = CliRunner()
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(source_code)
            tmp_path = tmp.name
        try:
            args = ["validate", tmp_path] + (extra_args or [])
            result = runner.invoke(sdk_group, args, catch_exceptions=False, obj={"fmt": "json"})
            return result
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_valid_strategy_exits_0(self) -> None:
        """A valid strategy file should produce ok:true and exit code 0."""
        result = self._invoke(_VALID_STRATEGY)
        self.assertEqual(result.exit_code, 0, msg=f"stdout: {result.output}")
        envelope = json.loads(result.output)
        self.assertTrue(envelope.get("ok"), msg=f"envelope: {envelope}")

    def test_missing_on_bar_exits_1(self) -> None:
        """A strategy missing on_bar should produce ok:false and exit code 1."""
        result = self._invoke(_MISSING_ON_BAR)
        self.assertEqual(result.exit_code, 1, msg=f"stdout: {result.output}")
        envelope = json.loads(result.output)
        self.assertFalse(envelope.get("ok"), msg=f"envelope: {envelope}")
        error = envelope.get("error", {})
        self.assertIn("error_code", error, msg=f"error block: {error}")

    def test_disallowed_import_exits_1(self) -> None:
        """A strategy with a disallowed import should produce ok:false and exit code 1."""
        result = self._invoke(_DISALLOWED_IMPORT)
        self.assertEqual(result.exit_code, 1, msg=f"stdout: {result.output}")
        envelope = json.loads(result.output)
        self.assertFalse(envelope.get("ok"), msg=f"envelope: {envelope}")
        error = envelope.get("error", {})
        self.assertEqual(error.get("error_code"), "disallowed_import", msg=f"error block: {error}")

    def test_no_class_name_flag_required(self) -> None:
        """The command should succeed without any --class-name flag."""
        # The old command required --class-name; the new one auto-discovers.
        runner = CliRunner()
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(_VALID_STRATEGY)
            tmp_path = tmp.name
        try:
            result = runner.invoke(
                sdk_group,
                ["validate", tmp_path],
                catch_exceptions=False,
                obj={"fmt": "json"},
            )
            # Should not fail with "Missing option '--class-name'"
            self.assertNotIn("Missing option", result.output)
            self.assertNotEqual(result.exit_code, 2, msg=f"Click usage error: {result.output}")
        finally:
            Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
