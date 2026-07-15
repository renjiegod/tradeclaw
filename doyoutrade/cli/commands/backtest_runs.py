"""Additional `doyoutrade-cli backtest ...` subcommands: run / summary / suggest-iteration.

These are the heavy lifecycle commands that complement
``backtest watch`` (the streaming poll loop). Registered on the same
``backtest`` group exported from ``commands/backtest.py``.

``backtest run`` is the CLI equivalent of ``run_strategy_backtest``:

* **task mode**: ``--task <task_id>`` for a backtest task that already
  carries its own strategy binding / universe.
* **definition mode**: ``--definition sd-... --universe SYM1,SYM2``
  auto-creates a backtest task bound to the definition, then runs it.

Default ``--timeout 120`` waits for terminal status. ``--timeout 0``
fires and returns immediately — pair with ``backtest watch`` for
follow-up.
"""

from __future__ import annotations

import asyncio
from typing import Any

import click

from doyoutrade.cli._envelope import EXIT_OK, success_envelope
from doyoutrade.cli._format import write_envelope
from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli._kwargs import (
    exit_for_invalid_params,
    parse_params_json,
    split_csv,
)
from doyoutrade.cli._progress import ProgressReporter, should_show_progress
from doyoutrade.cli.commands.backtest import backtest
from doyoutrade.cli.main import run_async_command


# Statuses that stop the client-side progress poll. Mirrors the server's
# ``TERMINAL_BACKTEST_STATUSES`` (the set ``POST /backtest-runs`` waits on)
# and additionally stops promptly on a stopped/cancelled run instead of
# hanging until the wait deadline.
_PROGRESS_TERMINAL_STATUSES = frozenset(
    {"completed", "finished", "failed", "cancelled", "stopped"}
)


def _emit_params_error(err: dict[str, Any]) -> None:
    meta_dict = read_session_meta().to_dict()
    if meta_dict:
        err["meta"] = meta_dict
    fmt = click.get_current_context().find_root().obj.get("fmt", "json")
    write_envelope(err, fmt=fmt)
    click.get_current_context().exit(exit_for_invalid_params(err))


def _backtest_api_timeout_seconds(tool_timeout: float | None) -> float:
    """Keep the HTTP request open at least as long as the tool wait contract."""

    wait_seconds = 120.0 if tool_timeout is None else max(float(tool_timeout), 0.0)
    return max(15.0, wait_seconds + 5.0)


@backtest.command("run")
@click.option("--task", "task_id", default=None, help="Existing backtest task_id (uuid). Mutually exclusive with --definition.")
@click.option(
    "--definition",
    "definition_id",
    default=None,
    help="Strategy definition id (sd-...). Auto-creates a backtest task bound to the definition.",
)
@click.option(
    "--params",
    "params_json",
    default=None,
    help='Strategy parameter overrides as JSON, e.g. --params \'{"window": 14}\'. Used with --definition.',
)
@click.option("--range-start", "range_start", required=True, help="Inclusive start date YYYY-MM-DD.")
@click.option("--range-end", "range_end", required=True, help="Inclusive end date YYYY-MM-DD.")
@click.option("--universe", default=None, help="Comma-separated symbols (required in --definition mode).")
@click.option("--name", default=None, help="Optional name for the auto-created task (--definition mode only).")
@click.option(
    "--data-provider",
    "data_provider",
    default=None,
    help="Optional data provider for the auto-created task (auto/qmt/mock/akshare).",
)
@click.option(
    "--config-overrides",
    "config_overrides_json",
    default=None,
    help='Per-run overrides as JSON, e.g. --config-overrides \'{"settings": {...}}\'.',
)
@click.option("--market-profile", "market_profile", default=None, help="Market profile (default cn_a_share).")
@click.option("--bar-interval", "bar_interval", default=None, help="Bar interval (default 1d).")
@click.option("--model-route", "model_route_name", default=None, help="Model route override.")
@click.option(
    "--debug/--no-debug",
    "debug_enabled",
    default=True,
    help=(
        "Capture full debug observability (default --debug). --no-debug runs in "
        "fast mode: skips debug session / spans / per-bar cycle traces / model "
        "invocations for a faster run, keeping status + report + trade fills. "
        "Under --no-debug, `debug get-run-view` has no trace detail by design."
    ),
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=float,
    default=None,
    help="Wait timeout seconds. Default 120. Pass 0 for fire-and-forget.",
)
@click.option(
    "--poll-interval",
    "poll_interval_seconds",
    type=float,
    default=None,
    help="Polling interval while waiting (default 0.2).",
)
@click.option(
    "--progress/--no-progress",
    "progress",
    default=None,
    help=(
        "Render a live progress bar to stderr while waiting. Default: auto "
        "(on when stderr is an interactive TTY, off otherwise). The stdout "
        "JSON envelope is identical either way, so agents are unaffected."
    ),
)
def backtest_run(
    task_id: str | None,
    definition_id: str | None,
    params_json: str | None,
    range_start: str,
    range_end: str,
    universe: str | None,
    name: str | None,
    data_provider: str | None,
    config_overrides_json: str | None,
    market_profile: str | None,
    bar_interval: str | None,
    model_route_name: str | None,
    debug_enabled: bool,
    timeout_seconds: float | None,
    poll_interval_seconds: float | None,
    progress: bool | None,
) -> None:
    """Start a backtest and wait for terminal status (default --timeout 120s).

    Pair with ``backtest watch <run_id>`` when running with --timeout 0.
    """

    config_overrides, err = parse_params_json(config_overrides_json)
    if err is not None:
        _emit_params_error(err)
        return

    parameters, err = parse_params_json(params_json)
    if err is not None:
        _emit_params_error(err)
        return

    universe_list = split_csv(universe)
    kwargs: dict[str, Any] = {
        "range_start": range_start,
        "range_end": range_end,
        "debug_enabled": debug_enabled,
    }
    for key, value in (
        ("task_id", task_id),
        ("definition_id", definition_id),
        ("parameters", parameters),
        ("universe", universe_list),
        ("name", name),
        ("data_provider", data_provider),
        ("config_overrides", config_overrides),
        ("market_profile", market_profile),
        ("bar_interval", bar_interval),
        ("model_route_name", model_route_name),
        ("timeout_seconds", timeout_seconds),
        ("poll_interval_seconds", poll_interval_seconds),
    ):
        if value is not None:
            kwargs[key] = value

    ctx = click.get_current_context()

    if should_show_progress(progress):
        ctx.exit(
            run_async_command(
                lambda: _run_with_progress(
                    kwargs,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                )
            )
        )
        return

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            "/backtest-runs",
            json=kwargs,
            meta=read_session_meta(),
            timeout_seconds=_backtest_api_timeout_seconds(timeout_seconds),
        )

    ctx.exit(run_async_command(_run))


async def _run_with_progress(
    kwargs: dict[str, Any],
    *,
    timeout_seconds: float | None,
    poll_interval_seconds: float | None,
) -> tuple[dict[str, Any], int]:
    """Fire the backtest, then client-poll the summary while drawing a bar.

    The blocking ``POST /backtest-runs`` cannot stream progress (it only
    returns once the run is terminal), so the progress path instead:

    1. starts the run fire-and-forget (``timeout_seconds=0``) to learn the
       ``run_id`` immediately;
    2. polls ``GET /backtest-runs/{run_id}/summary`` on ``poll_interval``,
       feeding ``bars_completed`` / ``bars_total`` into the stderr bar;
    3. reconstructs the *same* envelope shape the blocking POST returns
       (``status`` / ``run_id`` / ``task_id`` / ``auto_created_task_id`` /
       ``summary``) so stdout stays byte-for-byte compatible for agents.

    Poll failures are surfaced (not swallowed) per the error-visibility
    rule: a non-ok summary envelope is returned as-is and stops the bar.
    """

    meta = read_session_meta()
    reporter = ProgressReporter(enabled=True)

    # 1. fire-and-forget so we get a run_id without waiting for terminal.
    start_kwargs = dict(kwargs)
    start_kwargs["timeout_seconds"] = 0
    post_env, post_exit = await invoke_api(
        "POST",
        "/backtest-runs",
        json=start_kwargs,
        meta=meta,
        timeout_seconds=_backtest_api_timeout_seconds(0),
    )
    if not post_env.get("ok"):
        return post_env, post_exit

    started = post_env.get("data") or {}
    run_id = started.get("run_id") if isinstance(started, dict) else None
    if not isinstance(run_id, str) or not run_id:
        # Nothing to poll — hand back whatever the server returned.
        return post_env, post_exit
    task_id_started = started.get("task_id") if isinstance(started, dict) else None
    auto_created_task_id = (
        started.get("auto_created_task_id") if isinstance(started, dict) else None
    )

    # 2. poll the summary endpoint, mirroring the server's wait contract.
    wait_seconds = 120.0 if timeout_seconds is None else max(float(timeout_seconds), 0.0)
    interval = 0.2 if poll_interval_seconds is None else max(float(poll_interval_seconds), 0.0)
    interval = max(interval, 0.1)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds if wait_seconds > 0 else None

    last_summary_data: dict[str, Any] = {}
    final_run: dict[str, Any] = {}
    try:
        while True:
            sum_env, sum_exit = await invoke_api(
                "GET",
                f"/backtest-runs/{run_id}/summary",
                params={"format": "json"},
                meta=meta,
                not_found_error_code="backtest_run_not_found",
            )
            if not sum_env.get("ok"):
                # Surface poll failures instead of hiding them behind the bar.
                return sum_env, sum_exit

            sdata = sum_env.get("data")
            sdata = sdata if isinstance(sdata, dict) else {}
            last_summary_data = sdata
            run = sdata.get("run")
            run = run if isinstance(run, dict) else {}
            status = str(run.get("status") or "").strip().lower()
            reporter.update(
                int(run.get("bars_completed") or 0),
                int(run.get("bars_total") or 0),
                status,
            )

            if status in _PROGRESS_TERMINAL_STATUSES:
                final_run = run
                break
            if deadline is not None and loop.time() >= deadline:
                final_run = run
                break
            await asyncio.sleep(interval)
    finally:
        reporter.close()

    # 3. reconstruct the canonical POST envelope shape.
    result: dict[str, Any] = {
        "status": final_run.get("status") or started.get("status"),
        "run_id": run_id,
        "task_id": final_run.get("task_id") or task_id_started,
    }
    if auto_created_task_id is not None:
        result["auto_created_task_id"] = auto_created_task_id
    if last_summary_data.get("summary_state") == "ok":
        summary = last_summary_data.get("summary")
        if isinstance(summary, dict):
            result["summary"] = summary

    return success_envelope(result, "", meta=meta), EXIT_OK


@backtest.command("summary")
@click.argument("run_id")
@click.option(
    "--format",
    "fmt_param",
    type=click.Choice(["markdown", "json"], case_sensitive=False),
    default="json",
    show_default=True,
    help="Response body format. Prefer json; markdown is retained only for manual report rendering.",
)
def backtest_summary(run_id: str, fmt_param: str) -> None:
    """Re-fetch a backtest's persisted summary by run_id."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/backtest-runs/{run_id}/summary",
            params={"format": fmt_param.lower()},
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@backtest.command("suggest-iteration")
@click.argument("run_id")
def backtest_suggest_iteration(run_id: str) -> None:
    """Inspect a run and recommend the next iteration step (definition / parameter / instance change)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            f"/backtest-runs/{run_id}/suggest-iteration",
            meta=read_session_meta(),
            not_found_error_code="backtest_run_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@backtest.command("walk-forward")
@click.option("--definition", "definition_id", required=True, help="Strategy definition id (sd-...).")
@click.option("--universe", required=True, help="Comma-separated symbols for the per-window backtests.")
@click.option(
    "--params",
    "params_json",
    default=None,
    help='Parameter overrides as JSON (same across all windows), e.g. --params \'{"window": 14}\'.',
)
@click.option("--range-start", "range_start", required=True, help="Inclusive start date YYYY-MM-DD of the full range to split.")
@click.option("--range-end", "range_end", required=True, help="Inclusive end date YYYY-MM-DD.")
@click.option(
    "--segments",
    type=int,
    default=None,
    help="Number of consecutive equal windows to split the range into (default 3, range 2-6).",
)
@click.option(
    "--min-trades",
    "min_trades",
    type=int,
    default=None,
    help="Min closed trades for a window to count toward the verdict (default 1).",
)
@click.option(
    "--data-provider",
    "data_provider",
    default=None,
    help="Data provider for the per-window tasks (auto/qmt/mock/akshare).",
)
@click.option(
    "--keep-tasks/--no-keep-tasks",
    "keep_tasks",
    default=False,
    help="Keep the auto-created per-window backtest tasks for drill-in (default: delete after).",
)
@click.option(
    "--timeout",
    "timeout_seconds",
    type=float,
    default=None,
    help="Per-window backtest completion timeout in seconds (default 120).",
)
def backtest_walk_forward(
    definition_id: str,
    universe: str,
    params_json: str | None,
    range_start: str,
    range_end: str,
    segments: int | None,
    min_trades: int | None,
    data_provider: str | None,
    keep_tasks: bool,
    timeout_seconds: float | None,
) -> None:
    """Segmented out-of-sample robustness: run the SAME strategy + params across
    N consecutive windows and check the edge generalises across time.

    ``status=fragile`` (the edge only holds in some windows) exits non-zero so
    it can gate promotion, mirroring ``sdk validate-recursive``. This is
    fixed-parameter multi-window OOS, not re-optimising walk-forward.
    """

    parameters, err = parse_params_json(params_json)
    if err is not None:
        _emit_params_error(err)
        return

    kwargs: dict[str, Any] = {
        "definition_id": definition_id,
        "universe": split_csv(universe),
        "range_start": range_start,
        "range_end": range_end,
        "keep_tasks": keep_tasks,
    }
    for key, value in (
        ("parameters", parameters),
        ("segments", segments),
        ("min_trades", min_trades),
        ("data_provider", data_provider),
        ("timeout_seconds", timeout_seconds),
    ):
        if value is not None:
            kwargs[key] = value

    # The HTTP call blocks for the whole sweep (N sequential window backtests),
    # so size the client timeout to segments * per-window-timeout + buffer.
    seg = segments if isinstance(segments, int) and segments > 0 else 3
    per_window = timeout_seconds if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0 else 120.0
    api_timeout = max(60.0, seg * float(per_window) + 30.0)

    async def _run() -> tuple[dict[str, Any], int]:
        envelope, exit_code = await invoke_api(
            "POST",
            "/backtest/walk-forward",
            json=kwargs,
            meta=read_session_meta(),
            timeout_seconds=api_timeout,
        )
        # Gate: a completed-but-fragile sweep is a success envelope (the full
        # per-window table is readable) but exits 1 so a pre-promotion check
        # treats overfit-across-time as a failed gate. robust / inconclusive
        # exit 0.
        if envelope.get("ok"):
            data = envelope.get("data")
            if isinstance(data, dict) and data.get("status") == "fragile":
                exit_code = 1
        return envelope, exit_code

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ---------------------------------------------------------------------------
# strategy inspect — extends commands/strategy.py's `strategy` group.
# ---------------------------------------------------------------------------


def _register_strategy_inspect() -> None:
    """Add `strategy inspect` to the existing `strategy` group at import time.

    Done as a side-effect-on-import rather than editing strategy.py
    because Phase 2's strategy.py was already complete. This keeps the
    `strategy` group whole without growing that file with an
    inspection command unrelated to definition/instance writes.
    """

    from doyoutrade.cli.commands.strategy import strategy as strategy_group

    @strategy_group.command("inspect")
    @click.option(
        "--query",
        "query",
        default=None,
        help="Fuzzy search across definition fields (whitespace-separated tokens AND-matched).",
    )
    def strategy_inspect(query: str | None) -> None:
        """Inspect strategy definitions, surfacing duplicate code_hash groups."""

        from doyoutrade.strategies.inspect_resources import build_strategy_inspect_payload

        async def _run() -> tuple[dict[str, Any], int]:
            definitions_envelope, definitions_exit = await invoke_api(
                "GET",
                "/strategy-definitions",
                meta=read_session_meta(),
            )
            if not definitions_envelope.get("ok"):
                return definitions_envelope, definitions_exit

            raw_items = list((definitions_envelope.get("data") or {}).get("items") or [])
            definitions = [row for row in raw_items if isinstance(row, dict)]
            return {
                "ok": True,
                "data": build_strategy_inspect_payload(definitions, query=query),
            }, 0

        ctx = click.get_current_context()
        ctx.exit(run_async_command(_run))


_register_strategy_inspect()


__all__: list[str] = []  # commands are registered on `backtest` / `strategy` groups directly
