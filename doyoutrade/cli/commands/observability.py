"""`doyoutrade-cli cycle / debug / route` subcommands — observability surface.

All three groups live in one module because each is a thin wrapper over
a single tool (or two), and they share the same observability theme:
* ``cycle list`` / ``cycle get`` — paginate / inspect a task's cycle runs
* ``debug get-run-view`` — fetch the structured debug payload
  (``cycle_run`` / ``spans`` / ``model_invocations``) the in-process UI
  reads from. Accepts cycle-run, backtest-job, or debug-session ids.
* ``route list`` — list available model routes (``route_name`` values to
  pass under ``settings.model_route_name`` in ``task create``).

These do not need streaming — ``debug get-run-view`` returns a single
snapshot; for live event tailing during a backtest, use
``doyoutrade-cli backtest watch``.
"""

from __future__ import annotations

from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command


# ---------------------------------------------------------------------------
# cycle
# ---------------------------------------------------------------------------


@click.group()
def cycle() -> None:
    """Cycle run inspection commands."""


@cycle.command("list")
@click.argument("identifier")
@click.option("--limit", type=int, default=50, show_default=True, help="Max results.")
@click.option("--offset", type=int, default=0, show_default=True, help="Pagination offset.")
@click.option("--status", default=None, help="Exact status filter (running / completed / ...).")
@click.option("--run-kind", "run_kind", default=None, help="Filter by 'scheduled' / 'manual' / 'debug'.")
@click.option("--run-mode", "run_mode", default=None, help="Filter by 'paper' / 'live' / 'backtest'.")
@click.option("--run-id-contains", "run_id_contains", default=None, help="Substring match on run_id.")
@click.option("--started-after", "started_after", default=None, help="ISO datetime lower bound (inclusive).")
@click.option("--started-before", "started_before", default=None, help="ISO datetime upper bound.")
@click.option("--run-id", "run_id", default=None, help="Exact backtest job run_id whose session to query.")
def cycle_list(
    identifier: str,
    limit: int,
    offset: int,
    status: str | None,
    run_kind: str | None,
    run_mode: str | None,
    run_id_contains: str | None,
    started_after: str | None,
    started_before: str | None,
    run_id: str | None,
) -> None:
    """List cycle runs for a task (by task_id UUID or exact name)."""

    async def _run() -> tuple[dict[str, Any], int]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        for key, value in (
            ("status", status),
            ("run_kind", run_kind),
            ("run_mode", run_mode),
            ("q", run_id_contains),
            ("started_after", started_after),
            ("started_before", started_before),
            ("run_id", run_id),
        ):
            if value is not None:
                params[key] = value
        return await invoke_api(
            "GET",
            f"/tasks/{identifier}/cycle-runs",
            params=params,
            meta=read_session_meta(),
            not_found_error_code="task_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@cycle.command("get")
@click.argument("run_id")
def cycle_get(run_id: str) -> None:
    """Get one cycle run by its exact run_id."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/cycle-runs/{run_id}",
            meta=read_session_meta(),
            not_found_error_code="cycle_run_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ---------------------------------------------------------------------------
# debug
# ---------------------------------------------------------------------------


@click.group()
def debug() -> None:
    """Debug-view inspection commands."""


@debug.command("get-run-view")
@click.argument("run_id")
@click.option(
    "--summary-only",
    "summary_only",
    is_flag=True,
    default=False,
    help="Attach a compact summary block; still returns the trimmed payload.",
)
@click.option(
    "--no-spans",
    "no_spans",
    is_flag=True,
    default=False,
    help="Drop the spans array from the response (smaller payload).",
)
@click.option(
    "--no-model-invocations",
    "no_model_invocations",
    is_flag=True,
    default=False,
    help="Drop the model_invocations array.",
)
@click.option(
    "--cycle-runs-limit",
    "cycle_runs_limit",
    type=int,
    default=None,
    help="If set (>=0), truncate cycle_runs to at most this many entries.",
)
def debug_get_run_view(
    run_id: str,
    summary_only: bool,
    no_spans: bool,
    no_model_invocations: bool,
    cycle_runs_limit: int | None,
) -> None:
    """Fetch the debug view for a strategy run, backtest job, or debug session.

    \b
    Read order for zero-trade backtests (the common pain case):
      1. ``debug_view.signal_timeline_summary`` (placed FIRST in payload —
         survives truncation): counts + ``top_hold_tags`` / ``top_buy_tags``
         / ``top_target_exposure_tags`` / ``top_target_quantity_tags`` tell you
         "what dominated" in one glance.
      2. ``debug_view.signal_timeline`` (TOP-LEVEL key, not nested in
         ``cycle_runs``): one row per cycle with ``run_id``,
         ``cycle_time``, ``signals_buy/sell/hold/target_exposure/target_quantity``,
         ``per_symbol_tags``.

    \b
    Common confusion: ``cycle_runs[i].signal_generation`` is ALWAYS empty
    today — signal info lives on ``debug_view.signal_timeline`` instead.

    \b
    An untagged ``Signal.hold()`` shows up as ``<untagged_hold>`` in
    ``per_symbol_tags`` — fix by tagging the hold branch in the strategy
    source (``Signal.hold(tag='no_cross')`` etc.) and re-running.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        params: dict[str, Any] = {
            "summary_only": summary_only,
            "include_spans": not no_spans,
            "include_model_invocations": not no_model_invocations,
        }
        if cycle_runs_limit is not None:
            params["include_cycle_runs_limit"] = cycle_runs_limit
        return await invoke_api(
            "GET",
            f"/cycle-runs/{run_id}/debug-view",
            params=params,
            meta=read_session_meta(),
            not_found_error_code="run_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@debug.command("get-trace-view")
@click.argument("trace_id")
def debug_get_trace_view(trace_id: str) -> None:
    """Fetch the debug view directly by OpenTelemetry trace_id.

    \b
    The trace_id is the 32-char lowercase hex id (the first half of a
    ``traceparent`` header). Use this when you have a trace_id from a log
    line or span and want the full picture — spans, cycle runs, and model
    invocations carrying that trace — without first mapping it to a run_id.

    \b
    Returns the same shape as ``get-run-view`` (``resolved_from.identifier_type``
    is ``"trace"``). For a run / backtest / debug-session id instead, use
    ``debug get-run-view``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/traces/{trace_id}/debug-view",
            meta=read_session_meta(),
            not_found_error_code="trace_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@debug.command("model-invocations")
@click.option("--trace-id", "trace_id", default=None, help="Filter by exact OTel trace_id (32-hex).")
@click.option("--run-id", "run_id", default=None, help="Filter by exact run_id (cycle_run / backtest job).")
@click.option("--span-id", "span_id", default=None, help="Filter by exact span_id.")
@click.option("--limit", type=int, default=20, show_default=True, help="Max results (1-500).")
@click.option("--offset", type=int, default=0, show_default=True, help="Pagination offset.")
def debug_model_invocations(
    trace_id: str | None,
    run_id: str | None,
    span_id: str | None,
    limit: int,
    offset: int,
) -> None:
    """List recorded LLM/model invocations (request + response + tokens + latency).

    \b
    Filter by any combination of --trace-id / --run-id / --span-id (all exact
    match). With no filter, returns the most recent invocations across all
    runs. Each item carries trace_id / span_id / run_id so you can pivot to
    ``debug get-trace-view`` / ``get-run-view`` for the surrounding spans.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        for key, value in (("trace_id", trace_id), ("run_id", run_id), ("span_id", span_id)):
            if value is not None:
                params[key] = value
        return await invoke_api(
            "GET",
            "/model-invocations",
            params=params,
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------


@click.group()
def route() -> None:
    """Model route inspection commands."""


@route.command("list")
def route_list() -> None:
    """List configured model routes (route_name values usable in task settings)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("GET", "/model-routes", meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["cycle", "debug", "route"]
