"""doyoutrade-cli click entry point.

Hierarchy mirrors the existing tool domains so skills can map cleanly::

    doyoutrade-cli task get <identifier>
    doyoutrade-cli task list [--q ...] [--status ...] [--mode ...] [--definition sd-...]
    doyoutrade-cli stock lookup <q> [--limit N] [--source ...]
    doyoutrade-cli schema <command>

Global flags (apply to every subcommand)::

    --format json|pretty|ndjson    (default: json)
    --debug-session-id <id>        override DOYOUTRADE_DEBUG_SESSION_ID env
    --no-debug-session             force standalone mode even when env is set

Exit codes — see ``_envelope.py`` for the stable contract:
0 = ok, 1 = failure, 2 = validation, 3 = not found, 10 = CLI internal error.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

import click

from doyoutrade.cli._cli_errors import _structured_click_error_envelope
from doyoutrade.cli._envelope import EXIT_INTERNAL, EXIT_OK, error_envelope
from doyoutrade.cli._format import FORMAT_JSON, SUPPORTED_FORMATS, write_envelope
from doyoutrade.cli._invoke import read_session_meta


# Silence chatty third-party connection logs that otherwise leak into the
# CLI envelope on stdout. lark_oapi's WS client prints
# ``[Lark] ... [INFO] connected to wss://...`` on every connection — that
# line corrupts the single-line JSON envelope contract for agents that
# parse stdout. WARNING / ERROR still pass through so real failures stay
# visible per CLAUDE.md's "错误可见性" rule.
logging.getLogger("Lark").setLevel(logging.WARNING)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--format",
    "fmt",
    type=click.Choice(SUPPORTED_FORMATS),
    default=FORMAT_JSON,
    show_default=True,
    help="Output format. ndjson reserved for streaming commands.",
)
@click.option(
    "--debug-session-id",
    "debug_session_id_override",
    default=None,
    help="Override DOYOUTRADE_DEBUG_SESSION_ID env (rarely needed; agents inherit via execute_bash).",
)
@click.option(
    "--no-debug-session",
    is_flag=True,
    default=False,
    help="Force standalone mode: ignore DOYOUTRADE_DEBUG_SESSION_ID env.",
)
@click.pass_context
def cli(ctx: click.Context, fmt: str, debug_session_id_override: str | None, no_debug_session: bool) -> None:
    """doyoutrade-cli — agent-facing CLI wrapping doyoutrade tools."""

    ctx.ensure_object(dict)
    ctx.obj["fmt"] = fmt

    # Apply the debug-session env overrides early so every subcommand sees
    # the same Meta. Modifying os.environ here is safe — we're in a fresh
    # process, not a long-lived service.
    if no_debug_session:
        os.environ.pop("DOYOUTRADE_DEBUG_SESSION_ID", None)
        os.environ.pop("DOYOUTRADE_RUN_ID", None)
    elif debug_session_id_override:
        os.environ["DOYOUTRADE_DEBUG_SESSION_ID"] = debug_session_id_override


def run_async_command(coro_factory: Any) -> int:
    """Drive an async command function inside its own event loop and shut down.

    ``coro_factory`` is a zero-arg callable returning a coroutine that
    yields ``(envelope, exit_code)``. Centralised here so every command
    gets the same runtime-teardown and unexpected-exception fallback.
    """

    fmt = click.get_current_context().obj.get("fmt", FORMAT_JSON)

    async def _runner() -> tuple[dict[str, Any], int]:
        return await coro_factory()

    try:
        envelope, exit_code = asyncio.run(_runner())
    except click.ClickException:
        raise
    except Exception as exc:
        envelope = error_envelope(
            error_code="cli_internal_error",
            error_type=type(exc).__name__,
            message=str(exc) or f"{type(exc).__name__} (no message)",
            meta=read_session_meta(),
        )
        write_envelope(envelope, fmt=fmt)
        return EXIT_INTERNAL

    write_envelope(envelope, fmt=fmt)
    return exit_code


# Register subcommand groups. Imports live inside ``main`` so a syntax /
# import error in one command group doesn't break ``doyoutrade-cli --help``.
def _register_commands() -> None:
    from doyoutrade.cli.commands import account as account_cmd
    from doyoutrade.cli.commands import analysis as analysis_cmd
    from doyoutrade.cli.commands import assistant as assistant_cmd
    from doyoutrade.cli.commands import backtest as backtest_cmd
    from doyoutrade.cli.commands import cron as cron_cmd
    from doyoutrade.cli.commands import data as data_cmd
    from doyoutrade.cli.commands import decision_signal as decision_signal_cmd
    from doyoutrade.cli.commands import knowledge as knowledge_cmd
    from doyoutrade.cli.commands import monitor as monitor_cmd
    from doyoutrade.cli.commands import observability as obs_cmd
    from doyoutrade.cli.commands import portfolio as portfolio_cmd
    from doyoutrade.cli.commands import schema as schema_cmd
    from doyoutrade.cli.commands import sdk as sdk_cmd
    from doyoutrade.cli.commands import stock as stock_cmd
    from doyoutrade.cli.commands import strategy as strategy_cmd
    from doyoutrade.cli.commands import swarm as swarm_cmd
    from doyoutrade.cli.commands import task as task_cmd
    from doyoutrade.cli.commands import watchlist as watchlist_cmd

    # Side-effect import: registers ``backtest run/summary/suggest-iteration``
    # and ``strategy inspect`` onto their respective groups.
    from doyoutrade.cli.commands import backtest_runs  # noqa: F401

    cli.add_command(assistant_cmd.assistant)
    cli.add_command(account_cmd.account)
    cli.add_command(task_cmd.task)
    cli.add_command(strategy_cmd.strategy)
    cli.add_command(swarm_cmd.swarm)
    cli.add_command(backtest_cmd.backtest)
    cli.add_command(cron_cmd.cron)
    cli.add_command(obs_cmd.cycle)
    cli.add_command(obs_cmd.debug)
    cli.add_command(obs_cmd.route)
    cli.add_command(sdk_cmd.sdk)
    cli.add_command(data_cmd.data)
    cli.add_command(analysis_cmd.analysis)
    cli.add_command(stock_cmd.stock)
    cli.add_command(watchlist_cmd.watchlist)
    cli.add_command(monitor_cmd.monitor)
    cli.add_command(decision_signal_cmd.decision_signal)
    cli.add_command(portfolio_cmd.portfolio)
    cli.add_command(knowledge_cmd.knowledge)
    cli.add_command(schema_cmd.schema)


def _resolve_fmt_from_argv(argv: list[str] | None = None) -> str:
    """Best-effort recover ``--format`` from argv before click parses it.

    When a top-level ``ClickException`` fires (unknown subcommand, missing
    parameter, etc.) the root callback never ran, so ``ctx.obj['fmt']``
    is empty. We still want the structured-error envelope to honour
    ``--format pretty`` if the user (or skill) asked for it. Returns
    :data:`FORMAT_JSON` when the flag is absent or invalid — invalid
    values are treated as absent rather than raising, since we're already
    inside the error path.
    """

    args = argv if argv is not None else sys.argv[1:]
    for idx, token in enumerate(args):
        if token == "--format" and idx + 1 < len(args):
            candidate = args[idx + 1]
            if candidate in SUPPORTED_FORMATS:
                return candidate
            return FORMAT_JSON
        if token.startswith("--format="):
            candidate = token.split("=", 1)[1]
            if candidate in SUPPORTED_FORMATS:
                return candidate
            return FORMAT_JSON
    return FORMAT_JSON


def main() -> None:
    """Console-script entry point — wires subcommands then runs click."""

    _register_commands()
    try:
        exit_code = cli.main(standalone_mode=False)
    except click.exceptions.Exit as exc:
        # ``ctx.exit(N)`` raises this; propagate the requested exit code
        # without rendering a structured envelope (the command already
        # printed whatever it wanted to print).
        sys.exit(exc.exit_code)
    except click.ClickException as exc:
        # Structured handler — replaces ``exc.show(); sys.exit(2)``. Every
        # known click failure mode gets a distinct ``error_code`` token so
        # callers can self-correct from JSON rather than parsing prose.
        envelope, code = _structured_click_error_envelope(exc, meta=read_session_meta())
        write_envelope(envelope, fmt=_resolve_fmt_from_argv())
        sys.exit(code)
    if isinstance(exit_code, int):
        sys.exit(exit_code)
    sys.exit(EXIT_OK)


__all__ = ["cli", "main", "run_async_command"]
