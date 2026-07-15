"""Tests for the CLI's structured click-exception envelope.

Background: ``doyoutrade/cli/main.py`` used to catch ``ClickException`` and
just call ``exc.show()`` — that prints ``Error: No such command 'foo'.``
as plain text on stderr, leaving CLI callers (agents and humans) no
structured ``error_code`` token to branch on. After the change in
``doyoutrade/cli/_cli_errors.py`` every well-known click failure mode
produces an :func:`error_envelope` with a stable code, a
``did_you_mean`` hint where possible, and (for typo'd subcommands) a
``suggested_path`` mapping that the model can paste into the next CLI
call.

These tests cover the three failure modes the original incident
report flagged (unknown command, unknown option, missing parameter) plus
the generic-UsageError fallback, and verify both the envelope shape and
the exit code mapping.
"""

from __future__ import annotations

import unittest

import click

from doyoutrade.cli._cli_errors import _structured_click_error_envelope
from doyoutrade.cli._envelope import EXIT_VALIDATION, Meta
from doyoutrade.cli.main import _register_commands, _resolve_fmt_from_argv, cli


def _trigger(args: list[str]) -> click.ClickException:
    """Invoke the wired-up CLI and return the raised click exception.

    Centralised so each test stays focused on the envelope assertion.
    Fails the test if no exception is raised — that would indicate a
    regression in the CLI's command tree, not in this module.
    """

    _register_commands()
    try:
        cli.main(args, standalone_mode=False)
    except click.ClickException as exc:
        return exc
    raise AssertionError(f"expected ClickException for args={args!r}, got success")


class UnknownSubcommandTests(unittest.TestCase):
    def test_unknown_subcommand_emits_unknown_command_envelope(self) -> None:
        exc = _trigger(["strategy", "definition", "nonexistent-verb"])

        envelope, code = _structured_click_error_envelope(exc, meta=Meta())

        self.assertEqual(code, EXIT_VALIDATION)
        self.assertFalse(envelope["ok"])
        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_command")
        self.assertEqual(err["error_type"], "UsageError")
        self.assertEqual(err["unknown_command"], "nonexistent-verb")
        # Available siblings should be the real subcommands of the
        # ``strategy definition`` group — proves we descended into the
        # right context.
        self.assertIn("get", err["available_commands"])
        # ``did_you_mean`` is a list (possibly empty for a totally
        # unrelated typo); we only assert the type contract here.
        self.assertIsInstance(err["did_you_mean"], list)
        # command_path normalised to the documented entry point.
        self.assertTrue(err["command_path"].startswith("doyoutrade-cli"))

    def test_unknown_alias_emits_suggested_path(self) -> None:
        # ``ls`` is one of the documented user-utterance shortcuts; when
        # a group has an ``inspect`` subcommand we should propose it.
        exc = _trigger(["strategy", "ls"])

        envelope, code = _structured_click_error_envelope(exc, meta=Meta())

        self.assertEqual(code, EXIT_VALIDATION)
        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_command")
        self.assertEqual(err["unknown_command"], "ls")
        # ``strategy`` group exposes ``inspect`` (registered by the
        # backtest_runs side-effect module). The suggested-path mapping
        # should target it.
        suggested = err.get("suggested_path") or {}
        self.assertIn("ls", suggested)
        self.assertTrue(suggested["ls"].endswith("inspect"))

    def test_close_match_populates_did_you_mean(self) -> None:
        # Typo a real subcommand by one character so difflib's cutoff is
        # comfortably met.
        exc = _trigger(["strategy", "definition", "gett"])

        envelope, _code = _structured_click_error_envelope(exc, meta=Meta())

        err = envelope["error"]
        self.assertIn("get", err["did_you_mean"])


class UnknownOptionTests(unittest.TestCase):
    def test_unknown_option_emits_unknown_option_envelope(self) -> None:
        # ``strategy definition get`` takes a positional ``definition_id``
        # only — passing ``--limit`` triggers ``NoSuchOption``.
        exc = _trigger(["strategy", "definition", "get", "--limit", "20"])

        envelope, code = _structured_click_error_envelope(exc, meta=Meta())

        self.assertEqual(code, EXIT_VALIDATION)
        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_option")
        self.assertEqual(err["error_type"], "NoSuchOption")
        self.assertEqual(err["unknown_option"], "--limit")
        self.assertIsInstance(err["did_you_mean"], list)
        # ``command_path`` keeps the full subcommand path so the agent can
        # immediately retry without re-navigating.
        self.assertIn("strategy definition get", err["command_path"])

    def test_unknown_option_falls_back_to_semantic_aliases(self) -> None:
        # ``stock lookup`` has ``--limit`` but no ``--start``. Click's
        # difflib-based ``.possibilities`` won't propose ``--limit``
        # for ``--start`` (similarity is below the cutoff), so the
        # envelope must fall back to ``CLI_OPTION_ALIASES`` to give the
        # agent a meaningful suggestion in a single round-trip.
        # (``data ohlcv`` used to fit this scenario before it natively
        # accepted ``--start`` / ``--range-start``.)
        exc = _trigger(
            ["stock", "lookup", "茅台", "--start", "2026-04-23"]
        )

        envelope, code = _structured_click_error_envelope(exc, meta=Meta())

        self.assertEqual(code, EXIT_VALIDATION)
        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_option")
        self.assertEqual(err["unknown_option"], "--start")
        # Either ``--range-start`` or ``--period`` (or both) should be
        # offered to the agent — the semantic table lists them in order
        # of likelihood.
        self.assertTrue(
            "--range-start" in err["did_you_mean"]
            or "--period" in err["did_you_mean"],
            f"did_you_mean missing semantic alias: {err['did_you_mean']!r}",
        )
        self.assertEqual(err.get("alias_source"), "semantic_table")
        # Hint should point the agent at the suggested flags rather than
        # just `--help`.
        self.assertIn("suggested", err["hint"].lower())

    def test_unknown_option_prefers_click_possibilities_when_available(self) -> None:
        # ``stock lookup`` has ``--limit``. Typo it as ``--limt`` — Click
        # will propose ``--limit`` via its own fuzzy matcher, and that
        # MUST win over the semantic-alias table.
        exc = _trigger(["stock", "lookup", "茅台", "--limt", "5"])

        envelope, _code = _structured_click_error_envelope(exc, meta=Meta())

        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_option")
        self.assertEqual(err["unknown_option"], "--limt")
        self.assertIn("--limit", err["did_you_mean"])
        # When click already supplied possibilities, ``alias_source``
        # must NOT be set — provenance lets callers tell the two
        # mechanisms apart.
        self.assertNotIn("alias_source", err)

    def test_unknown_option_no_alias_returns_empty_did_you_mean(self) -> None:
        # ``--quzzeflump`` doesn't match anything in click's option
        # table nor in the semantic alias map. Envelope should still
        # carry an empty ``did_you_mean`` list (not omit the field) so
        # callers don't have to special-case absence.
        exc = _trigger(
            ["analysis", "pattern", "600522.SH", "--quzzeflump", "x"]
        )

        envelope, _code = _structured_click_error_envelope(exc, meta=Meta())

        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_option")
        self.assertEqual(err["unknown_option"], "--quzzeflump")
        self.assertEqual(err["did_you_mean"], [])
        self.assertNotIn("alias_source", err)

    def test_unknown_option_includes_schema_command_for_contract_lookup(self) -> None:
        # Regression for agents inferring ``--definition-id`` from a
        # ``definition_id`` response field. We do not add a compatibility
        # alias; the error should point at the command contract to prevent
        # another guess. ``backtest run`` takes ``--definition`` (not
        # ``--definition-id``).
        exc = _trigger(
            [
                "backtest",
                "run",
                "--definition-id",
                "sd-a4bdbb1db2ec",
                "--range-start",
                "2026-01-01",
                "--range-end",
                "2026-01-10",
            ]
        )

        envelope, _code = _structured_click_error_envelope(exc, meta=Meta())

        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_option")
        self.assertEqual(err["unknown_option"], "--definition-id")
        self.assertEqual(
            err["schema_command"],
            "doyoutrade-cli schema backtest.run",
        )
        self.assertIn("schema", err["hint"])

    def test_unknown_option_routes_to_positional_argument_hint(self) -> None:
        # ``analysis pattern`` takes a positional ``CODE`` — passing
        # ``--symbol`` (or its synonyms) gets a flag-style ``unknown_option``
        # from click with empty ``possibilities``. Without the positional-hint
        # table the agent has no choice but to ``--help`` (the same trap that
        # earlier burnt 4 tool calls on ``data ohlcv --symbol``; that command
        # has since been removed and the table now points at ``analysis
        # pattern``). The envelope must ship a ``positional_argument`` field
        # plus a ``suggested_invocation`` the agent can paste back as-is.
        exc = _trigger(["analysis", "pattern", "--symbol", "600522.SH"])

        envelope, code = _structured_click_error_envelope(exc, meta=Meta())

        self.assertEqual(code, EXIT_VALIDATION)
        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_option")
        self.assertEqual(err["unknown_option"], "--symbol")
        self.assertEqual(err.get("alias_source"), "positional_arg_hint")
        self.assertEqual(err["positional_argument"], "code")
        # ``suggested_invocation`` must be a complete shell-runnable line
        # starting with the canonical entry point.
        self.assertTrue(err["suggested_invocation"].startswith("doyoutrade-cli "))
        self.assertIn("analysis pattern", err["suggested_invocation"])
        # The first ``did_you_mean`` entry leads with the positional hint
        # so even agents that only look at the head of the list self-correct.
        self.assertTrue(err["did_you_mean"][0].startswith("<positional code>"))
        # Hint text spells out "positional argument" — the failure-mode label
        # the agent should pattern-match on, not just "did you mean a flag?".
        self.assertIn("positional argument", err["hint"])

    def test_unknown_option_positional_hint_covers_command_synonyms(self) -> None:
        # ``--ticker`` should map to the same positional ``code`` hint as
        # ``--symbol`` — the table covers the common synonyms so a single
        # round trip resolves it regardless of how the agent phrased the flag.
        exc = _trigger(["analysis", "pattern", "--ticker", "600522.SH"])

        envelope, _code = _structured_click_error_envelope(exc, meta=Meta())

        err = envelope["error"]
        self.assertEqual(err.get("alias_source"), "positional_arg_hint")
        self.assertEqual(err["positional_argument"], "code")

    def test_unknown_option_positional_hint_does_not_fire_on_unrelated_command(self) -> None:
        # The positional-hint table is per-command (suffix-matched). The
        # ``("analysis pattern", "symbol")`` entry must NOT trigger on
        # ``strategy definition get --symbol X`` (where ``--symbol`` has
        # no positional analogue). That path should fall back to the
        # standard ``did_you_mean`` / alias logic.
        exc = _trigger(["strategy", "definition", "get", "--symbol", "x"])

        envelope, _code = _structured_click_error_envelope(exc, meta=Meta())

        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_option")
        # No positional hint, no suggested_invocation — we don't have
        # ground truth for what ``--symbol`` was supposed to be in this
        # command, so the standard path is the right answer.
        self.assertNotEqual(err.get("alias_source"), "positional_arg_hint")
        self.assertNotIn("positional_argument", err)
        self.assertNotIn("suggested_invocation", err)

    def test_unknown_option_routes_to_wrong_command_hint(self) -> None:
        # ``--min-float-mv`` is a *filter* that lives on ``stock screen``;
        # ``data fundamentals`` only *reports* the value. An agent that puts
        # the filter on the reporting command (tmp/messages.json turn 10)
        # must be redirected to the canonical command in one round trip
        # rather than abandoning the first-party CLI for raw akshare.
        exc = _trigger(
            ["data", "fundamentals", "--universe-file", "/tmp/u.csv", "--min-float-mv", "1e10"]
        )

        envelope, code = _structured_click_error_envelope(exc, meta=Meta())

        self.assertEqual(code, EXIT_VALIDATION)
        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_option")
        self.assertEqual(err["unknown_option"], "--min-float-mv")
        self.assertEqual(err.get("alias_source"), "wrong_command")
        self.assertEqual(err["canonical_command"], "doyoutrade-cli stock screen")
        self.assertTrue(err["suggested_invocation"].startswith("doyoutrade-cli stock screen"))
        self.assertIn("stock screen", err["did_you_mean"][0])
        self.assertIn("stock screen", err["hint"])

    def test_unknown_option_does_not_self_suggest(self) -> None:
        # request1.json line 97 regression: ``strategy inspect --limit 5``
        # used to return ``did_you_mean: ["--limit", "--max-events"]`` —
        # suggesting the same flag the user just tried is a dead-end loop.
        # The envelope must never list the offending flag as its own
        # repair hint, regardless of which suggestion source (click
        # possibilities, semantic table) the entry came from.
        exc = _trigger(
            ["strategy", "inspect", "--query", "MACD", "--limit", "5"]
        )

        envelope, _code = _structured_click_error_envelope(exc, meta=Meta())

        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_option")
        self.assertEqual(err["unknown_option"], "--limit")
        # ``--limit`` itself must NOT appear in did_you_mean.
        self.assertNotIn(
            "--limit",
            err["did_you_mean"],
            f"self-suggested offending flag: {err['did_you_mean']!r}",
        )

    def test_unknown_option_alias_lookup_is_case_insensitive(self) -> None:
        # ``--Start`` / ``--START`` should hit the same alias entry as
        # ``--start``. Click preserves the original casing in
        # ``exc.option_name``; the alias table lookup lowercases it.
        # ``stock lookup`` is used because ``data ohlcv`` now natively
        # accepts ``--start``; pick a sibling command that still rejects
        # any casing variant of it.
        exc = _trigger(
            ["stock", "lookup", "茅台", "--START", "2026-04-23"]
        )

        envelope, _code = _structured_click_error_envelope(exc, meta=Meta())

        err = envelope["error"]
        self.assertEqual(err["error_code"], "unknown_option")
        self.assertTrue(
            "--range-start" in err["did_you_mean"]
            or "--period" in err["did_you_mean"],
        )
        self.assertEqual(err.get("alias_source"), "semantic_table")


class MissingParameterTests(unittest.TestCase):
    def test_missing_argument_emits_missing_parameter_envelope(self) -> None:
        # ``strategy definition get`` requires a ``definition_id``
        # positional — omitting it raises ``MissingParameter``.
        exc = _trigger(["strategy", "definition", "get"])

        envelope, code = _structured_click_error_envelope(exc, meta=Meta())

        self.assertEqual(code, EXIT_VALIDATION)
        err = envelope["error"]
        self.assertEqual(err["error_code"], "missing_parameter")
        self.assertEqual(err["error_type"], "MissingParameter")
        self.assertEqual(err["param_name"], "definition_id")
        self.assertEqual(err["param_kind"], "argument")
        # ``message`` must never be the empty string — click's default
        # ``MissingParameter`` message is empty so we synthesise one.
        self.assertTrue(err["message"])


class GenericUsageErrorTests(unittest.TestCase):
    def test_generic_usage_error_falls_back_to_usage_error_code(self) -> None:
        # Construct a UsageError that isn't a NoSuchOption /
        # MissingParameter / "No such command" — e.g. raised manually
        # with no context.
        exc = click.UsageError("Got two values where one was expected.")

        envelope, code = _structured_click_error_envelope(exc, meta=Meta())

        self.assertEqual(code, EXIT_VALIDATION)
        err = envelope["error"]
        self.assertEqual(err["error_code"], "usage_error")
        self.assertIn("two values", err["message"])
        # Fallback ``command_path`` must still be present so the envelope
        # is consistent across failure modes.
        self.assertEqual(err["command_path"], "doyoutrade-cli")


class FmtResolutionTests(unittest.TestCase):
    def test_default_json_when_no_format_flag(self) -> None:
        self.assertEqual(_resolve_fmt_from_argv(["strategy", "definition", "get"]), "json")

    def test_pretty_resolved_from_space_separated_flag(self) -> None:
        self.assertEqual(
            _resolve_fmt_from_argv(["--format", "pretty", "strategy", "definition", "get"]),
            "pretty",
        )

    def test_pretty_resolved_from_equals_form(self) -> None:
        self.assertEqual(
            _resolve_fmt_from_argv(["--format=pretty", "strategy"]),
            "pretty",
        )

    def test_invalid_format_value_falls_back_to_json(self) -> None:
        # We're already inside an error path — refuse to crash on a
        # malformed --format value, just default to json.
        self.assertEqual(
            _resolve_fmt_from_argv(["--format", "xml"]),
            "json",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
