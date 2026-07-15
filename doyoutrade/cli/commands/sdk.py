"""`doyoutrade-cli sdk ...` subcommands — strategy-SDK discovery and validation.

The four SDK tools answer "what can I use when writing a strategy?":

* ``sdk dp-methods``     — DataProvider methods available to strategies
* ``sdk indicators``     — built-in indicators with their parameters
* ``sdk data-requests``  — DataRequest field shapes for ``StrategyDefinition``
* ``sdk validate``       — compile + smoke-test a draft strategy file (no DB
                           writes).  Routes directly through
                           ``StrategyCompiler.validate_directory`` via a
                           temporary directory — the deleted
                           ``validate_strategy_code`` tool is not used.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import click

from doyoutrade.cli._format import write_envelope
from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command


@click.group()
def sdk() -> None:
    """Strategy-SDK discovery and validation commands."""


@sdk.command("dp-methods")
def sdk_dp_methods() -> None:
    """List DataProvider methods exposed to strategies."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("GET", "/sdk/dp-methods", meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@sdk.command("indicators")
def sdk_indicators() -> None:
    """List built-in indicators with their parameter shapes."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("GET", "/sdk/indicators", meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@sdk.command("data-requests")
def sdk_data_requests() -> None:
    """List DataRequest field shapes for StrategyDefinition declarations."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("GET", "/sdk/data-requests", meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@sdk.command("validate")
@click.argument("source_file", type=click.Path(exists=True, dir_okay=False, readable=True))
def sdk_validate(source_file: str) -> None:
    """Compile + smoke-test a strategy file (no DB writes).

    Copies the source file into a temporary directory as ``strategy.py`` and
    runs ``StrategyCompiler.validate_directory`` + smoke gate directly.
    No authoring session or network call is needed.

    The file must define a class named ``Strategy`` (shadowing the SDK base),
    which is the convention used by the authoring lifecycle.  For example::

        class Strategy(Strategy):
            ...

    The ``--class-name`` flag was removed in the strategy-as-files refactor
    (Task 6, 2026-05-24); the compiler looks for a class named ``Strategy``
    by default.
    """
    from doyoutrade.assistant.strategy_tools._smoke_gate import (
        run_directory_smoke_gate,
        smoke_error_payload,
    )

    source_path = Path(source_file)
    source_code = source_path.read_text(encoding="utf-8")

    fmt = click.get_current_context().find_root().obj
    fmt = fmt.get("fmt", "json") if isinstance(fmt, dict) else "json"

    with tempfile.TemporaryDirectory(prefix="doyoutrade_sdk_validate_") as tmpdir:
        tmp_strategy = Path(tmpdir) / "strategy.py"
        tmp_strategy.write_text(source_code, encoding="utf-8")

        smoke = run_directory_smoke_gate(Path(tmpdir))

    if smoke.success:
        result: dict[str, Any] = {
            "ok": True,
            "status": "ok",
            "message": "Compile + smoke passed. No errors found.",
            "source_file": str(source_path),
        }
        write_envelope({"ok": True, "data": result}, fmt=fmt)
        sys.exit(0)
    else:
        error_payload = smoke_error_payload(smoke, class_name="<auto-discovered>")
        error_payload["source_file"] = str(source_path)
        write_envelope({"ok": False, "error": error_payload}, fmt=fmt)
        sys.exit(1)


@sdk.command("validate-recursive")
@click.argument("source_file", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--symbol", required=True, help="Canonical CODE.EXCHANGE to compute indicators against.")
@click.option("--as-of", "as_of", default=None, help="YYYY-MM-DD end of the reference window (default latest).")
@click.option(
    "--ladder",
    default=None,
    help="Comma-separated tail-history lengths to test, e.g. 30,60,120. Omit for auto.",
)
@click.option("--freq", default="1d", help="Bar frequency (default 1d).")
@click.option("--data-source", "data_source", default="auto", help="auto|qmt|akshare|tushare|baostock|mootdx.")
@click.option(
    "--threshold-pct",
    "threshold_pct",
    type=float,
    default=None,
    help="Drift tolerance in percent before a column is flagged unstable (default 1.0).",
)
def sdk_validate_recursive(
    source_file: str,
    symbol: str,
    as_of: str | None,
    ladder: str | None,
    freq: str,
    data_source: str,
    threshold_pct: float | None,
) -> None:
    """Check how much a strategy's indicators drift with ``startup_history``.

    Compiles the strategy file, fetches a long reference window of real
    OHLCV for ``--symbol``, and re-runs ``populate_indicators`` at several
    shorter tail-history lengths to measure how far each indicator's
    last-row value is from its fully-warmed value. Recursive indicators
    (EMA / Wilder-RSI / ADX / MACD …) that haven't converged at the
    declared ``startup_history`` are the ones whose backtest values won't
    reproduce on the live cron path.

    Exits non-zero when ``status == "unstable"`` so it can gate promotion,
    mirroring ``sdk validate``.
    """

    source_code = Path(source_file).read_text(encoding="utf-8")

    payload: dict[str, Any] = {
        "source_code": source_code,
        "symbol": symbol,
        "freq": freq,
        "data_source": data_source,
    }
    if as_of:
        payload["as_of"] = as_of
    if threshold_pct is not None:
        payload["threshold_pct"] = threshold_pct
    if ladder:
        try:
            payload["ladder"] = [int(part.strip()) for part in ladder.split(",") if part.strip()]
        except ValueError:
            raise click.BadParameter(
                f"--ladder must be comma-separated integers, got {ladder!r}",
                param_hint="--ladder",
            )

    async def _run() -> tuple[dict[str, Any], int]:
        envelope, exit_code = await invoke_api(
            "POST", "/sdk/validate-recursive", json=payload, meta=read_session_meta()
        )
        # Gate semantics: a completed-but-unstable analysis is a success
        # envelope (the full per-column table is readable) but exits 1 so a
        # pre-promotion check / CI step treats it as a failed gate.
        if envelope.get("ok"):
            data = envelope.get("data")
            if isinstance(data, dict) and data.get("status") == "unstable":
                exit_code = 1
        return envelope, exit_code

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["sdk"]
