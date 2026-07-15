"""Translate click ClickException instances into structured CLI envelopes.

Background: click's default error rendering (``ClickException.show()``)
prints ``Error: No such command 'list'.`` as plain text on stderr. Skill
docs already promise CLI callers a JSON envelope on stdout with a stable
``error_code`` and (for typos) a ``did_you_mean`` / ``suggested_path``
hint. This helper closes the gap between click's exception hierarchy and
the existing :func:`doyoutrade.cli._envelope.error_envelope` contract so
agents (and humans) can self-correct after a single CLI round trip.

The handler is deliberately conservative:

* It does not paper over unknown click exception types — those still
  fall through to a generic ``usage_error`` envelope with the original
  click message preserved so an operator can see what happened.
* It does not invent error codes that aren't in
  ``_VALIDATION_ERROR_CODES`` — every code emitted here is whitelisted
  so ``exit_code_for_error`` maps to ``EXIT_VALIDATION`` (2) and skill
  docs can reference the same token.
* It does not silently swallow ``exc.ctx is None`` (rare). When the
  context is missing we still emit a structured envelope, but with an
  empty ``did_you_mean`` list and ``hint`` pointing at ``--help``.

Reference: CLAUDE.md "错误可见性 / 静默吞 bug 禁令" — every CLI failure
mode must be distinguishable via a structured ``error_code`` field, not
buried in free-form prose.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any

import click

from doyoutrade.cli._envelope import (
    CLI_COMMAND_ALIASES,
    CLI_OPTION_ALIASES,
    CLI_OPTION_WRONG_COMMAND_HINTS,
    CLI_POSITIONAL_ARG_HINTS,
    EXIT_VALIDATION,
    Meta,
    error_envelope,
)

logger = logging.getLogger(__name__)

# click renders unknown subcommands as ``No such command 'foo'.``; we
# round-trip that prose back into a structured field so the envelope's
# ``did_you_mean`` list can be computed against the parent group's actual
# subcommands. Keeping the regex anchored avoids matching legitimate
# prose that happens to contain the literal phrase.
_NO_SUCH_COMMAND_RE = re.compile(r"No such command ['\"]([^'\"]+)['\"]")


def _close_matches(name: str, candidates: list[str], *, limit: int = 3) -> list[str]:
    """Return up to ``limit`` close matches of ``name`` in ``candidates``.

    Thin wrapper around :func:`difflib.get_close_matches` with the
    project's defaults; pulled out so tests can monkeypatch a deterministic
    matcher if needed.
    """

    if not name or not candidates:
        return []
    return difflib.get_close_matches(name, candidates, n=limit, cutoff=0.5)


def _group_command_names(ctx: click.Context | None) -> list[str]:
    """Return the subcommand names available on ``ctx``'s current group.

    Returns an empty list when the context is unavailable or the current
    command is not a :class:`click.Group` (e.g. UsageError raised inside
    a leaf command). Callers treat empty as "no suggestions" — that is
    visible behaviour, not a silent fallback.
    """

    if ctx is None or ctx.command is None:
        return []
    command = ctx.command
    if not isinstance(command, click.Group):
        return []
    return sorted(command.commands.keys())


def _command_path(ctx: click.Context | None) -> str:
    """Return the canonical command path (``doyoutrade-cli ...``) for ``ctx``.

    Falls back to ``doyoutrade-cli`` when no context is available so the
    ``suggested_path`` envelope field still produces an actionable string.
    """

    if ctx is None:
        return "doyoutrade-cli"
    # click stores ``sys.argv[0]`` as ``ctx.info_name``; when invoked via
    # ``cli.main([...], standalone_mode=False)`` this becomes the python
    # binary path. Replace with the documented entry point so the
    # ``suggested_path`` value can be pasted into a shell as-is.
    path = ctx.command_path or "doyoutrade-cli"
    # ``command_path`` for the root may equal ``-c`` (when invoked via
    # ``python -c``) or an absolute path. Normalise the leading token to
    # ``doyoutrade-cli`` so skill docs and tests can match a stable prefix.
    parts = path.split(" ", 1)
    if len(parts) == 1:
        return "doyoutrade-cli"
    return f"doyoutrade-cli {parts[1]}"


def _schema_command_for_path(command_path: str) -> str | None:
    """Return the contract lookup command for a concrete CLI command path."""

    parts = command_path.split()
    if len(parts) <= 1:
        return None
    top_level = {
        "analysis",
        "backtest",
        "cron",
        "cycle",
        "data",
        "debug",
        "monitor",
        "route",
        "sdk",
        "stock",
        "strategy",
        "task",
    }
    start = 1 if parts[0] == "doyoutrade-cli" else 0
    for idx in range(start, len(parts)):
        if parts[idx] in top_level:
            return "doyoutrade-cli schema " + ".".join(parts[idx:])
    return None


def _structured_click_error_envelope(
    exc: click.ClickException,
    *,
    meta: Meta,
) -> tuple[dict[str, Any], int]:
    """Translate a click exception into ``(envelope_dict, exit_code)``.

    Dispatches on the exception class so each click failure mode gets a
    distinct ``error_code`` token. Falls back to a generic
    ``usage_error`` envelope (with a logged warning) when the exception
    type isn't one of the well-known ones — preserving visibility per
    CLAUDE.md's "错误可见性" rule.
    """

    ctx = exc.ctx
    command_path = _command_path(ctx)
    base_message = exc.format_message() if hasattr(exc, "format_message") else str(exc.message)

    # 1. NoSuchOption — typo'd ``--foo`` flag. click already populates
    # ``.possibilities`` from its own fuzzy matcher; we forward those as
    # ``did_you_mean`` so the envelope is as useful as click's own hint.
    # When click's own list is empty (difflib's similarity threshold is
    # strict — ``--start`` vs ``--period`` doesn't match) we fall back
    # to ``CLI_OPTION_ALIASES``: a small semantic table that maps
    # "intuitive but wrong" flag names to the canonical ones they likely
    # meant. ``alias_source`` records the provenance so callers can tell
    # a click-derived suggestion from a semantic-table one.
    if isinstance(exc, click.NoSuchOption):
        # Filter out the offending option itself from click's suggestions —
        # ``Did you mean: --limit?`` when the user just typed ``--limit`` is
        # a dead-end loop (request1.json line 97). click occasionally emits
        # the offending name itself when its difflib threshold matches the
        # canonical option name to itself via a punning case-fold; we strip
        # it unconditionally so suggestions are always *different* from the
        # offending input.
        offending = (exc.option_name or "").strip()
        did_you_mean = [s for s in (exc.possibilities or []) if s != offending]
        option_key = offending.lstrip("-").lower()
        extra: dict[str, Any] = {
            "did_you_mean": did_you_mean,
            "command_path": command_path,
            "unknown_option": exc.option_name,
        }
        schema_command = _schema_command_for_path(command_path)
        if schema_command is not None:
            extra["schema_command"] = schema_command

        # Positional-argument hint takes precedence over flag aliases:
        # when the user passed ``--symbol`` but the canonical entry is a
        # positional ``CODE``, ``--option-name`` suggestions just send
        # them around the same loop. The hint includes a concrete
        # corrected invocation so they can self-correct in one round trip.
        # Match the command path suffix (e.g. ``analysis pattern``) so the same
        # global table covers all entry shapes that lead here.
        positional_hint_key: tuple[str, str] | None = None
        for (suffix, opt), _value in CLI_POSITIONAL_ARG_HINTS.items():
            if opt == option_key and command_path.endswith(suffix):
                positional_hint_key = (suffix, opt)
                break

        if positional_hint_key is not None:
            arg_name, example_value = CLI_POSITIONAL_ARG_HINTS[positional_hint_key]
            suffix = positional_hint_key[0]
            corrected = f"doyoutrade-cli {suffix} {example_value}"
            extra["positional_argument"] = arg_name
            extra["alias_source"] = "positional_arg_hint"
            extra["suggested_invocation"] = corrected
            # Preserve any click-derived did_you_mean entries; agents may
            # still want flags too. But the positional hint is the primary
            # repair so list it first via ``did_you_mean``-shaped sugar.
            extra["did_you_mean"] = [
                f"<positional {arg_name}> — try: {corrected}",
                *did_you_mean,
            ]
            hint = (
                f"`{arg_name}` is a positional argument on `{command_path}`, "
                f"not a flag. Example: `{corrected}`. "
                f"Run `{schema_command or command_path + ' --help'}` for the full signature."
            )
            envelope = error_envelope(
                error_code="unknown_option",
                error_type="NoSuchOption",
                message=base_message,
                hint=hint,
                extra=extra,
                meta=meta,
            )
            return envelope, EXIT_VALIDATION

        # Wrong-sibling-command hint takes precedence over same-command flag
        # aliases: the flag exists, just on a different command. Point the
        # agent at the canonical command + a corrected invocation so it does
        # not abandon the first-party CLI (tmp/messages.json turn 10).
        wrong_command_key: tuple[str, str] | None = None
        for (suffix, opt), _value in CLI_OPTION_WRONG_COMMAND_HINTS.items():
            if opt == option_key and command_path.endswith(suffix):
                wrong_command_key = (suffix, opt)
                break
        if wrong_command_key is not None:
            canonical_command, corrected = CLI_OPTION_WRONG_COMMAND_HINTS[wrong_command_key]
            extra["alias_source"] = "wrong_command"
            extra["canonical_command"] = f"doyoutrade-cli {canonical_command}"
            extra["suggested_invocation"] = corrected
            extra["did_you_mean"] = [
                f"{offending} lives on `doyoutrade-cli {canonical_command}` — try: {corrected}",
                *did_you_mean,
            ]
            hint = (
                f"`{offending}` is not an option of `{command_path}`; it lives on "
                f"`doyoutrade-cli {canonical_command}`. Example: `{corrected}`."
            )
            envelope = error_envelope(
                error_code="unknown_option",
                error_type="NoSuchOption",
                message=base_message,
                hint=hint,
                extra=extra,
                meta=meta,
            )
            return envelope, EXIT_VALIDATION

        if not did_you_mean:
            aliases = CLI_OPTION_ALIASES.get(option_key, [])
            # CLI_OPTION_ALIASES historically contained self-aliases (e.g.
            # ``"limit": ["--limit", "--max-events"]``) — suggesting the
            # same flag the user just tried is a dead-end loop, so drop any
            # alias matching the offending option byte-for-byte.
            aliases = [a for a in aliases if a != offending]
            if aliases:
                did_you_mean = list(aliases)
                extra["did_you_mean"] = did_you_mean
                extra["alias_source"] = "semantic_table"
        if did_you_mean and extra.get("alias_source") == "semantic_table":
            hint = (
                "Try one of the suggested flags. "
                f"`{schema_command or command_path + ' --help'}` shows the full option list for this command."
            )
        elif did_you_mean:
            hint = (
                f"Did you mean: {', '.join(did_you_mean)}? "
                f"Run `{schema_command}` to inspect the command contract."
                if schema_command is not None
                else f"Did you mean: {', '.join(did_you_mean)}?"
            )
        else:
            hint = f"Run `{schema_command or command_path + ' --help'}` to inspect the command contract."
        envelope = error_envelope(
            error_code="unknown_option",
            error_type="NoSuchOption",
            message=base_message,
            hint=hint,
            extra=extra,
            meta=meta,
        )
        return envelope, EXIT_VALIDATION

    # 2. MissingParameter — required argument / option absent. click's
    # ``exc.param`` carries the offending parameter; we surface its name
    # and kind so the agent knows whether to add a positional or a flag.
    if isinstance(exc, click.MissingParameter):
        param = exc.param
        param_name = param.name if param is not None else None
        param_kind = param.param_type_name if param is not None else None
        # MissingParameter sometimes has an empty ``.message``; build a
        # human-readable one from ``param`` rather than passing through
        # the empty string.
        message = base_message or (
            f"Missing {param_kind or 'parameter'}: {param_name!r}"
            if param_name
            else "Missing required parameter."
        )
        extra = {
            "command_path": command_path,
        }
        if param_name:
            extra["param_name"] = param_name
        if param_kind:
            extra["param_kind"] = param_kind
        envelope = error_envelope(
            error_code="missing_parameter",
            error_type="MissingParameter",
            message=message,
            hint=f"Run `{command_path} --help` to see required arguments.",
            extra=extra,
            meta=meta,
        )
        return envelope, EXIT_VALIDATION

    # 3. UsageError → unknown subcommand. click renders these as
    # ``UsageError("No such command 'foo'.")`` so we sniff the message to
    # decide whether it's a typo'd subcommand vs. some other usage error.
    if isinstance(exc, click.UsageError):
        match = _NO_SUCH_COMMAND_RE.search(exc.message or "")
        if match is not None:
            unknown_name = match.group(1)
            siblings = _group_command_names(ctx)
            did_you_mean = _close_matches(unknown_name, siblings)
            suggested_path: dict[str, str] = {}
            alias = CLI_COMMAND_ALIASES.get(unknown_name)
            if alias and alias in siblings:
                suggested_path[unknown_name] = f"{command_path} {alias}"
            extra = {
                "did_you_mean": did_you_mean,
                "command_path": command_path,
                "unknown_command": unknown_name,
                "available_commands": siblings,
            }
            if suggested_path:
                extra["suggested_path"] = suggested_path
            envelope = error_envelope(
                error_code="unknown_command",
                error_type="UsageError",
                message=base_message,
                hint=(
                    f"Did you mean: {', '.join(did_you_mean)}?"
                    if did_you_mean
                    else f"Run `{command_path} --help` to see available commands."
                ),
                extra=extra,
                meta=meta,
            )
            return envelope, EXIT_VALIDATION

        # Generic UsageError (BadArgumentUsage, BadOptionUsage, plain
        # UsageError). Keep the click message intact so operators can
        # see what failed; do not silently re-classify.
        extra = {"command_path": command_path}
        envelope = error_envelope(
            error_code="usage_error",
            error_type=type(exc).__name__,
            message=base_message,
            hint=f"Run `{command_path} --help` for usage.",
            extra=extra,
            meta=meta,
        )
        return envelope, EXIT_VALIDATION

    # 4. Fallback for unknown ClickException subclasses. Log a warning so
    # the operator can grow the dispatch table — silent fallback would
    # violate CLAUDE.md's "错误可见性" rule.
    logger.warning(
        "Unhandled click exception class %s in main(): %s",
        type(exc).__name__,
        exc.message,
    )
    envelope = error_envelope(
        error_code="usage_error",
        error_type=type(exc).__name__,
        message=base_message,
        hint=f"Run `{command_path} --help` for usage.",
        extra={"command_path": command_path},
        meta=meta,
    )
    return envelope, EXIT_VALIDATION


__all__ = ["_structured_click_error_envelope"]
