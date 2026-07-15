"""`doyoutrade-cli backtest ...` subcommands — currently only ``watch``.

``backtest watch <run_id>`` is the CLI's first streaming command. It
polls ``GetBacktestSummaryTool`` on a fixed interval and emits one
NDJSON envelope on stdout whenever the snapshot changes, then exits
when one of four contract-stable conditions fires:

* ``terminal`` — the run reached ``completed`` / ``finished`` /
  ``failed`` / ``cancelled`` (the only condition where the default
  ``--until terminal`` stops on its own).
* ``limit`` — ``--max-events N`` reached.
* ``timeout`` — ``--timeout S`` elapsed.
* ``signal`` — ``SIGINT`` / ``SIGTERM`` received OR stdin closed by the
  caller. Agents shutting the subprocess down cleanly should close
  stdin rather than send a signal.

Contract surfaces (do not change without bumping the skill doc):

* stderr ready marker (single line):
    ``[doyoutrade] ready kind=backtest_watch run_id=<id>``
* stderr exit marker (single line):
    ``[doyoutrade] exited — received N event(s) in T (reason: <reason>)``
* stdout: NDJSON, one envelope per line, ``--format ndjson`` is the
  only supported format here. Each envelope mirrors the standard
  ``ok/data/meta`` shape from the main-agent system prompt's "CLI envelope 速读".
* Process exit code: **always 0**. Failure modes are expressed via the
  exit reason on stderr and via ``ok: false`` envelopes inside stdout.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import signal
import sys
from typing import Any

import click

from doyoutrade.cli._envelope import success_envelope
from doyoutrade.cli._format import FORMAT_NDJSON, write_envelope
from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._invoke import read_session_meta


# Terminal statuses that stop ``--until terminal`` watches. Mirrors the
# in-process ``TERMINAL_BACKTEST_STATUSES`` from ``doyoutrade.tools``.
_TERMINAL_STATUSES = frozenset({"completed", "finished", "failed", "cancelled"})


@click.group()
def backtest() -> None:
    """Backtest run inspection commands."""


@backtest.command("watch")
@click.argument("run_id")
@click.option(
    "--interval",
    type=float,
    default=2.0,
    show_default=True,
    help="Poll interval in seconds (lower bound 0.5).",
)
@click.option(
    "--max-events",
    "max_events",
    type=int,
    default=0,
    show_default=True,
    help="Stop after N envelopes emitted. 0 = unlimited.",
)
@click.option(
    "--timeout",
    type=float,
    default=0.0,
    show_default=True,
    help="Stop after T seconds elapsed. 0 = no timeout.",
)
@click.option(
    "--until",
    "until",
    type=click.Choice(["terminal", "none"], case_sensitive=False),
    default="terminal",
    show_default=True,
    help="Stop condition. ``terminal`` exits when status becomes completed/failed/cancelled.",
)
def backtest_watch(
    run_id: str,
    interval: float,
    max_events: int,
    timeout: float,
    until: str,
) -> None:
    """Watch a backtest run and stream status changes as NDJSON.

    Each emitted envelope is a snapshot from ``get_backtest_summary``.
    The CLI de-duplicates consecutive identical snapshots so an idle
    "running" backtest does not flood the stream — you only see lines
    when something actually changes.
    """

    interval = max(0.5, interval)
    exit_code = asyncio.run(
        _run_backtest_watch(
            run_id=run_id,
            interval=interval,
            max_events=max_events,
            timeout=timeout,
            until=until.lower(),
        )
    )
    ctx = click.get_current_context()
    ctx.exit(exit_code)


async def _run_backtest_watch(
    *,
    run_id: str,
    interval: float,
    max_events: int,
    timeout: float,
    until: str,
) -> int:
    """Drive the poll loop. Returns the CLI exit code (always 0 by contract)."""

    meta = read_session_meta()

    stop_state: dict[str, Any] = {"reason": None}
    loop = asyncio.get_running_loop()

    # Signal handlers — Windows / some test harnesses don't support
    # add_signal_handler so we guard each install. SIGINT and SIGTERM
    # both map to reason="signal" (lark-cli precedent).
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: stop_state.update({"reason": "signal"}))
        except (NotImplementedError, RuntimeError):
            # On some harnesses signal handlers can't attach to the loop.
            # We still honour stdin-EOF and timeout for an orderly exit.
            pass

    stdin_task = asyncio.create_task(_watch_stdin_for_eof(stop_state))

    # stderr ready marker — stable contract line, do not reorder fields.
    sys.stderr.write(f"[doyoutrade] ready kind=backtest_watch run_id={run_id}\n")
    sys.stderr.flush()

    started_at = loop.time()
    emitted = 0
    last_hash: str | None = None

    try:
        while True:
            if stop_state["reason"] is not None:
                break
            if timeout > 0 and (loop.time() - started_at) >= timeout:
                stop_state["reason"] = "timeout"
                break
            if max_events > 0 and emitted >= max_events:
                stop_state["reason"] = "limit"
                break

            envelope, _ = await invoke_api(
                "GET",
                f"/backtest-runs/{run_id}/summary",
                params={"format": "json"},
                meta=meta,
                not_found_error_code="backtest_run_not_found",
            )

            snapshot_hash = _envelope_snapshot_hash(envelope)
            if snapshot_hash != last_hash:
                write_envelope(envelope, fmt=FORMAT_NDJSON)
                emitted += 1
                last_hash = snapshot_hash

            # Fast-exit checks — don't sleep an extra interval when we
            # already know the loop is done.
            if until == "terminal" and _is_terminal_envelope(envelope):
                stop_state["reason"] = "terminal"
                break
            if max_events > 0 and emitted >= max_events:
                stop_state["reason"] = "limit"
                break

            # Sleep with cancel awareness so signal / EOF kicks in quickly.
            try:
                await asyncio.wait_for(
                    _stop_event_wait(stop_state),
                    timeout=interval,
                )
            except asyncio.TimeoutError:
                pass
    finally:
        stdin_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stdin_task

    elapsed = loop.time() - started_at
    reason = stop_state["reason"] or "terminal"
    sys.stderr.write(
        f"[doyoutrade] exited — received {emitted} event(s) in {elapsed:.1f}s (reason: {reason})\n"
    )
    sys.stderr.flush()

    # If the loop exited without emitting anything (e.g. fast-path
    # terminal on the very first poll matched last_hash because there
    # was no previous), make sure at least one envelope landed so the
    # caller has something to parse. Otherwise the agent's `jq .data`
    # blows up on an empty stream.
    if emitted == 0:
        write_envelope(
            success_envelope(
                {"status": "no_change", "run_id": run_id, "reason": reason},
                f"backtest watch produced no events for run_id={run_id}",
                meta=meta,
            ),
            fmt=FORMAT_NDJSON,
        )

    return 0  # contract: watch commands always exit 0; reason is in stderr / envelopes


async def _watch_stdin_for_eof(stop_state: dict[str, Any]) -> None:
    """Set ``stop_state['reason'] = 'signal'`` when the caller closes stdin.

    Agents shutting down a watch cleanly should close stdin (e.g. by
    redirecting ``< /dev/null`` or by letting the parent process exit).
    Sending SIGTERM also works but stdin EOF is the gentler signal.
    """

    if sys.stdin.closed:
        stop_state["reason"] = "signal"
        return
    loop = asyncio.get_running_loop()
    try:
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    except (OSError, ValueError):
        # stdin isn't a pollable pipe (e.g. running under a test harness
        # that captured stdin). We can't watch for EOF; fall back to
        # signals and timeout.
        return
    try:
        while True:
            line = await reader.readline()
            if not line:
                stop_state["reason"] = "signal"
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def _stop_event_wait(stop_state: dict[str, Any]) -> None:
    """Suspend until something flips ``stop_state['reason']``.

    Implemented as a polling sleep so we don't have to thread an
    asyncio.Event through every signal/stdin path. The 100 ms tick is
    well below typical poll intervals; the watch loop calls this with
    ``asyncio.wait_for(timeout=interval)`` so the effective resolution
    is bounded by the user's ``--interval``.
    """

    while stop_state["reason"] is None:
        await asyncio.sleep(0.1)


def _envelope_snapshot_hash(envelope: dict[str, Any]) -> str:
    """Hash the meaningful parts of an envelope so we can de-dup repeats.

    We deliberately exclude ``meta`` from the hash (run_id / session_id
    don't change across polls) and ignore floating ``_summary`` text
    that may carry timestamps the model already knows about.
    """

    if not isinstance(envelope, dict):
        return ""
    base = {
        "ok": envelope.get("ok"),
        "data": _strip_summary(envelope.get("data")),
        "error": envelope.get("error"),
    }
    raw = json.dumps(base, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _strip_summary(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k != "_summary"}
    return data


def _is_terminal_envelope(envelope: dict[str, Any]) -> bool:
    """True when the snapshot's ``run.status`` is in TERMINAL_BACKTEST_STATUSES.

    ``get_backtest_summary --format json`` puts the status under
    ``data.run.status`` (per the tool's docstring). When the envelope is
    an error (e.g. ``backtest_summary_not_found``), the run is not
    streamable and we leave the terminal detection to the underlying
    poll loop's other guards.
    """

    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return False
    data = envelope.get("data")
    if not isinstance(data, dict):
        return False
    run = data.get("run")
    if isinstance(run, dict):
        status = run.get("status")
        if isinstance(status, str) and status in _TERMINAL_STATUSES:
            return True
    # Some payloads expose top-level status, future-proof against schema drift.
    status = data.get("status")
    if isinstance(status, str) and status in _TERMINAL_STATUSES:
        return True
    return False


__all__ = ["backtest"]
