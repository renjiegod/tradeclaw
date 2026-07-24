"""`doyoutrade-cli instruments ...` subcommands.

``instrument_catalog`` (the table backing catalog membership / tradability
checks used by ``backtest run`` and task universe validation) is never
auto-seeded on a fresh deployment — it starts empty and previously could only
be populated via a raw ``POST /instruments/catalog/sync`` call with no CLI
surface, so agents/operators had no discoverable way to fix a "symbols not in
instrument catalog" error. This module exposes that sync operation as a
first-class command.
"""

from __future__ import annotations

from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._envelope import EXIT_VALIDATION, error_envelope
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.commands.stock import _read_universe_file
from doyoutrade.cli.main import run_async_command

# A full catalog sync fans out across the whole exchange listing (akshare) or
# a broker snapshot (qmt); mirror data.py's batch budget rather than the
# snappy 15s control-call default.
_CATALOG_SYNC_TIMEOUT_SECONDS = 180.0


@click.group()
def instruments() -> None:
    """Instrument catalog administration (backs backtest/task universe checks)."""


@instruments.group("catalog")
def catalog() -> None:
    """``instrument_catalog`` table operations."""


@catalog.command("sync")
@click.option(
    "--source",
    "source",
    required=True,
    type=click.Choice(["akshare", "qmt"], case_sensitive=False),
    help="Listing source to sync from. 'qmt' requires a default account with base_url.",
)
@click.option(
    "--mode",
    "mode",
    required=True,
    type=click.Choice(["full", "symbols"], case_sensitive=False),
    help=(
        "'full' pulls the entire exchange listing (stocks/ETFs/index seeds) — "
        "run this once on a new deployment before any backtest/task universe "
        "will validate. 'symbols' upserts only --symbol/--universe-file entries."
    ),
)
@click.option(
    "--symbol",
    "symbols",
    multiple=True,
    help="Symbol to upsert (mode=symbols only); repeat for multiple.",
)
@click.option(
    "--universe-file",
    "universe_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Text file of symbols, one per line (mode=symbols only).",
)
def catalog_sync(source: str, mode: str, symbols: tuple[str, ...], universe_file: str | None) -> None:
    """Sync instrument_catalog from a real data source.

    Examples::

        # First-time deployment bootstrap — populate the whole catalog:
        doyoutrade-cli instruments catalog sync --source akshare --mode full

        # Register a handful of specific symbols only:
        doyoutrade-cli instruments catalog sync --source akshare --mode symbols --symbol 600519.SH --symbol 000001.SH
    """
    mode_norm = mode.strip().lower()

    async def _run() -> tuple[dict[str, Any], int]:
        symbol_list: list[str] | None = None
        if mode_norm == "symbols":
            collected = list(symbols)
            if universe_file:
                collected.extend(_read_universe_file(universe_file))
            if not collected:
                return (
                    error_envelope(
                        error_code="validation_error",
                        message="mode=symbols requires at least one --symbol or --universe-file entry",
                        meta=read_session_meta(),
                    ),
                    EXIT_VALIDATION,
                )
            symbol_list = collected
        return await invoke_api(
            "POST",
            "/instruments/catalog/sync",
            json={"source": source, "mode": mode_norm, "symbols": symbol_list},
            meta=read_session_meta(),
            timeout_seconds=_CATALOG_SYNC_TIMEOUT_SECONDS,
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))
