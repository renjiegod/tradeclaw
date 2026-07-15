"""`doyoutrade-cli analysis ...` subcommands — pattern + indicators + factor analysis."""

from __future__ import annotations

import json
from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command


@click.group()
def analysis() -> None:
    """Analysis commands (pattern recognition, factor analysis)."""


@analysis.command("pattern")
@click.argument("code")
@click.option(
    "--patterns",
    default="all",
    show_default=True,
    help='Comma-separated pattern names or "all".',
)
@click.option(
    "--window",
    type=int,
    default=10,
    show_default=True,
    help="Detection window size.",
)
def analysis_pattern(code: str, patterns: str, window: int) -> None:
    """Detect candlestick / trend patterns in a symbol's OHLCV history.

    Reads from ``ohlcv_<code>.csv`` in the assistant artifacts dir.
    Generate that file first via ``doyoutrade-cli data run <code>``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            "/analysis/pattern",
            json={"code": code, "patterns": patterns, "window": window},
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@analysis.command("indicators")
@click.argument("code")
@click.option(
    "--indicators",
    default="all",
    show_default=True,
    help='Comma-separated indicator names or "all".',
)
@click.option(
    "--params",
    default=None,
    help=(
        'JSON object of per-indicator overrides, '
        'e.g. \'{"rsi": {"period": 21}, "kdj": {"n": 9}}\'.'
    ),
)
@click.option(
    "--tail",
    type=int,
    default=1,
    show_default=True,
    help="Trailing rows to return per indicator (1 = latest snapshot).",
)
def analysis_indicators(
    code: str, indicators: str, params: str | None, tail: int
) -> None:
    """Compute technical indicator values on a symbol's cached OHLCV.

    Reads from ``ohlcv_<code>.csv`` in the assistant artifacts dir; generate
    that file first via ``doyoutrade-cli data run <code>``. Writes the full
    indicator series to ``indicators_<code>.csv`` and returns the latest
    value(s) per indicator.
    """

    body: dict[str, Any] = {"code": code, "indicators": indicators, "tail": tail}
    if params is not None:
        try:
            body["params"] = json.loads(params)
        except json.JSONDecodeError as exc:
            raise click.BadParameter(
                f"--params must be a JSON object: {exc}", param_hint="--params"
            ) from exc

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            "/analysis/indicators",
            json=body,
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@analysis.command("factor")
@click.option(
    "--factor-csv",
    "factor_csv",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to factor value CSV (index=date, columns=codes).",
)
@click.option(
    "--return-csv",
    "return_csv",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Path to forward returns CSV (index=date, columns=codes).",
)
@click.option(
    "--output-dir",
    "output_dir",
    default=None,
    help="Output directory for plots / per-group stats.",
)
@click.option(
    "--n-groups",
    "n_groups",
    type=int,
    default=5,
    show_default=True,
    help="Number of quantile groups.",
)
def analysis_factor(
    factor_csv: str,
    return_csv: str,
    output_dir: str | None,
    n_groups: int,
) -> None:
    """Run IC / IR / quantile-group analysis on a factor."""

    async def _run() -> tuple[dict[str, Any], int]:
        kwargs: dict[str, Any] = {
            "factor_csv": factor_csv,
            "return_csv": return_csv,
            "n_groups": n_groups,
        }
        if output_dir is not None:
            kwargs["output_dir"] = output_dir
        return await invoke_api("POST", "/analysis/factor", json=kwargs, meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["analysis", "analysis_indicators"]
