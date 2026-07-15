"""`doyoutrade-cli decision-signal ...` subcommands (决策信号 → 回测验证闭环).

A decision signal is a persisted, attributable trading decision (``dsig-`` id
prefix) recorded from a backtest run, a live strategy, or an assistant
conversation, and later verified against subsequent market data
(``decision_signal_outcomes``: hit / miss / neutral per horizon).

Like the other CRUD command groups (``monitor`` / ``watchlist``), this is a
thin command-line / envelope adapter over the running API server's
``/decision-signals`` endpoints; base-URL resolution + ``api_unavailable``
handling live in ``doyoutrade/cli/_api.py``. Signal creation is not a CLI
write — signals are produced by the backtest hook and the
``record_decision_signal`` assistant tool.
"""

from __future__ import annotations

from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command


@click.group(name="decision-signal")
def decision_signal() -> None:
    """Decision signal (决策信号) inspection and re-evaluation via API server."""


@decision_signal.command("list")
@click.option("--task-id", "task_id", default=None, help="Filter by owning task (task-...).")
@click.option("--run-id", "run_id", default=None, help="Filter by producing run id.")
@click.option("--symbol", "symbol", default=None, help="Filter by canonical symbol (e.g. 600519.SH).")
@click.option(
    "--status",
    "status",
    type=click.Choice(["active", "expired", "invalidated", "evaluated"]),
    default=None,
    help="Filter by lifecycle status.",
)
@click.option("--limit", "limit", type=int, default=50, help="Max rows (<=500).")
@click.option("--offset", "offset", type=int, default=0, help="Pagination offset.")
def decision_signal_list(
    task_id: str | None,
    run_id: str | None,
    symbol: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> None:
    """List decision signals (lazily expires overdue active signals first)."""

    async def _run() -> tuple[dict[str, Any], int]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if task_id:
            params["task_id"] = task_id
        if run_id:
            params["run_id"] = run_id
        if symbol:
            params["symbol"] = symbol
        if status:
            params["status"] = status
        return await invoke_api(
            "GET", "/decision-signals", params=params, meta=read_session_meta()
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@decision_signal.command("get")
@click.argument("signal_id")
def decision_signal_get(signal_id: str) -> None:
    """Get one decision signal (dsig-...) with its evaluation outcomes."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/decision-signals/{signal_id}",
            meta=read_session_meta(),
            not_found_error_code="decision_signal_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@decision_signal.command("evaluate")
@click.argument("signal_id")
@click.option(
    "--horizon",
    "horizon",
    default=None,
    help="Evaluation window like '5d' (default: the signal's own horizon).",
)
@click.option(
    "--provider",
    "provider",
    default=None,
    help="Cached-bars provider to read from (default: resolved from the signal's task).",
)
def decision_signal_evaluate(
    signal_id: str, horizon: str | None, provider: str | None
) -> None:
    """Re-evaluate a signal against cached bars (upserts the outcome row).

    Data insufficiency is a structured success: ``data.status == "skipped"``
    with ``reason=data_insufficient`` — backfill bars, then re-run.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        payload: dict[str, Any] = {}
        if horizon:
            payload["horizon"] = horizon
        if provider:
            payload["provider"] = provider
        return await invoke_api(
            "POST",
            f"/decision-signals/{signal_id}/evaluate",
            json=payload,
            meta=read_session_meta(),
            not_found_error_code="decision_signal_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["decision_signal"]
