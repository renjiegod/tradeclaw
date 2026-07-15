"""`doyoutrade-cli monitor ...` subcommands (盯盘: realtime stock monitoring).

A monitor rule is a standalone, stock-scoped entity (``mon-`` id prefix) that the
MonitorDaemon evaluates tick-by-tick against the realtime quote stream — its
declarative AND/OR condition tree (preset detectors + field predicates) fires an
alert to a notification channel when met, independent of any running trading
task.

Like the other CRUD command groups (``watchlist`` / ``account``), this is a thin
command-line / envelope adapter over the running API server's ``/monitors``
endpoints; base-URL resolution + ``api_unavailable`` handling live in
``doyoutrade/cli/_api.py``.
"""

from __future__ import annotations

import json
from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._envelope import EXIT_VALIDATION, error_envelope
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.commands.stock import _read_universe_file
from doyoutrade.cli.main import run_async_command

_DATA_FETCH_TIMEOUT_SECONDS = 180.0

_PRESETS = (
    "limit_up",
    "limit_down",
    "limit_up_seal_shrink",
    "limit_down_seal_shrink",
    "limit_up_open",
    "limit_down_open",
)


@click.group()
def monitor() -> None:
    """Realtime stock monitoring (盯盘规则) management via API server."""


def _validation_envelope(message: str) -> tuple[dict[str, Any], int]:
    return (
        error_envelope(
            error_code="validation_error", message=message, meta=read_session_meta()
        ),
        EXIT_VALIDATION,
    )


def _parse_symbols(raw: str | None) -> tuple[list[str] | None, str | None]:
    """Parse ``--symbols`` (comma list or JSON array) into list[str]."""
    if raw is None:
        return None, None
    text = raw.strip()
    if not text:
        return [], None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"invalid_symbols_json: {exc}"
        if not isinstance(parsed, list):
            return None, f"invalid_symbols_json: expected a JSON array, got {type(parsed).__name__}"
        return [str(s).strip() for s in parsed if str(s).strip()], None
    return [part.strip() for part in text.split(",") if part.strip()], None


# ── Read commands ──────────────────────────────────────────────────────────────


@monitor.command("list")
def monitor_list() -> None:
    """List all monitor rules."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("GET", "/monitors", meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@monitor.command("get")
@click.argument("monitor_id")
def monitor_get(monitor_id: str) -> None:
    """Get a monitor rule by id (mon-...)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/monitors/{monitor_id}",
            meta=read_session_meta(),
            not_found_error_code="monitor_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@monitor.command("alerts")
@click.argument("monitor_id")
@click.option("--symbol", "symbol", default=None, help="Only alerts for this symbol.")
@click.option("--limit", "limit", type=int, default=100, help="Max alerts to return (≤500).")
def monitor_alerts(monitor_id: str, symbol: str | None, limit: int) -> None:
    """List a rule's fired-alert history (most recent first)."""

    async def _run() -> tuple[dict[str, Any], int]:
        params: dict[str, Any] = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        return await invoke_api(
            "GET",
            f"/monitors/{monitor_id}/alerts",
            params=params,
            meta=read_session_meta(),
            not_found_error_code="monitor_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ── Write commands ───────────────────────────────────────────────────────────


def _build_condition(
    preset: str | None, condition: str | None, condition_file: str | None
) -> tuple[dict | None, str | None]:
    """Resolve exactly one of --preset / --condition / --condition-file → a tree."""
    provided = [v is not None for v in (preset, condition, condition_file)]
    if sum(1 for v in provided if v) != 1:
        return None, "pass exactly one of --preset / --condition / --condition-file"
    if preset is not None:
        return {"preset": preset}, None
    raw = condition
    if condition_file is not None:
        try:
            with open(condition_file, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as exc:
            return None, f"could not read --condition-file: {exc}"
    try:
        tree = json.loads(raw or "")
    except json.JSONDecodeError as exc:
        return None, f"invalid_condition_json: {exc}"
    if not isinstance(tree, dict):
        return None, f"invalid_condition_json: expected a JSON object, got {type(tree).__name__}"
    return tree, None


def _build_scope(
    scope_kind: str, tag: str | None, symbols: str | None, universe_file: str | None
) -> tuple[dict | None, str | None]:
    if scope_kind == "watchlist_tag":
        return ({"tag": tag} if tag else {}), None
    # scope_kind == "symbols"
    if symbols is not None and universe_file is not None:
        return None, "pass only one of --symbols / --universe-file for scope_kind=symbols"
    if symbols is not None:
        parsed, err = _parse_symbols(symbols)
        if err:
            return None, err
        if not parsed:
            return None, "--symbols contained no symbols"
        return {"symbols": parsed}, None
    if universe_file is not None:
        try:
            resolved = _read_universe_file(universe_file)
        except click.BadParameter as exc:
            return None, str(exc)
        return {"symbols": resolved}, None
    return None, "scope_kind=symbols requires --symbols or --universe-file"


@monitor.command("create")
@click.option("--name", "name", required=True, help="Human-readable rule name.")
@click.option(
    "--scope-kind",
    "scope_kind",
    type=click.Choice(["watchlist_tag", "symbols"]),
    required=True,
    help="Which stocks to watch.",
)
@click.option("--tag", "tag", default=None, help="Watchlist tag (scope_kind=watchlist_tag; omit = all).")
@click.option("--symbols", "symbols", default=None, help="Comma list / JSON array (scope_kind=symbols).")
@click.option(
    "--universe-file",
    "universe_file",
    default=None,
    type=click.Path(dir_okay=False, readable=True),
    help="File with one CODE.EXCHANGE per line (scope_kind=symbols).",
)
@click.option("--channel-id", "channel_id", default=None, help="Delivery channel id (chan-...).")
@click.option("--chat-id", "chat_id", default=None, help="Feishu group chat id (oc_...).")
@click.option("--preset", "preset", type=click.Choice(list(_PRESETS)), default=None, help="Single-preset condition shortcut.")
@click.option("--condition", "condition", default=None, help="Condition tree as a JSON string.")
@click.option(
    "--condition-file",
    "condition_file",
    default=None,
    type=click.Path(dir_okay=False, readable=True),
    help="Path to a JSON file with the condition tree.",
)
@click.option("--cooldown", "cooldown", type=int, default=None, help="Min seconds between alerts (default 300).")
@click.option("--disabled", "disabled", is_flag=True, default=False, help="Create paused (enabled=false).")
def monitor_create(
    name: str,
    scope_kind: str,
    tag: str | None,
    symbols: str | None,
    universe_file: str | None,
    channel_id: str | None,
    chat_id: str | None,
    preset: str | None,
    condition: str | None,
    condition_file: str | None,
    cooldown: int | None,
    disabled: bool,
) -> None:
    """Create a monitor rule.

    Condition (exactly one): ``--preset limit_up`` for a single preset, or
    ``--condition '<JSON tree>'`` / ``--condition-file path.json`` for an AND/OR
    composite. Delivery: ``--channel-id chan-... --chat-id oc_...`` pushes to a
    Feishu group.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        scope, scope_err = _build_scope(scope_kind, tag, symbols, universe_file)
        if scope_err:
            return _validation_envelope(scope_err)
        tree, cond_err = _build_condition(preset, condition, condition_file)
        if cond_err:
            return _validation_envelope(cond_err)
        payload: dict[str, Any] = {
            "name": name,
            "scope_kind": scope_kind,
            "scope": scope,
            "condition_json": tree,
            "enabled": not disabled,
        }
        if channel_id is not None:
            payload["channel_id"] = channel_id
        if chat_id is not None:
            payload["chat_id"] = chat_id
        if cooldown is not None:
            payload["cooldown_seconds"] = cooldown
        return await invoke_api("POST", "/monitors", json=payload, meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@monitor.command("update")
@click.argument("monitor_id")
@click.option("--name", "name", default=None)
@click.option("--scope-kind", "scope_kind", type=click.Choice(["watchlist_tag", "symbols"]), default=None)
@click.option("--tag", "tag", default=None)
@click.option("--symbols", "symbols", default=None)
@click.option("--universe-file", "universe_file", default=None, type=click.Path(dir_okay=False, readable=True))
@click.option("--channel-id", "channel_id", default=None)
@click.option("--chat-id", "chat_id", default=None)
@click.option("--preset", "preset", type=click.Choice(list(_PRESETS)), default=None)
@click.option("--condition", "condition", default=None)
@click.option("--condition-file", "condition_file", default=None, type=click.Path(dir_okay=False, readable=True))
@click.option("--cooldown", "cooldown", type=int, default=None)
def monitor_update(
    monitor_id: str,
    name: str | None,
    scope_kind: str | None,
    tag: str | None,
    symbols: str | None,
    universe_file: str | None,
    channel_id: str | None,
    chat_id: str | None,
    preset: str | None,
    condition: str | None,
    condition_file: str | None,
    cooldown: int | None,
) -> None:
    """Update a monitor rule (patch: only supplied fields change)."""

    async def _run() -> tuple[dict[str, Any], int]:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if scope_kind is not None:
            scope, scope_err = _build_scope(scope_kind, tag, symbols, universe_file)
            if scope_err:
                return _validation_envelope(scope_err)
            payload["scope_kind"] = scope_kind
            payload["scope"] = scope
        if any(v is not None for v in (preset, condition, condition_file)):
            tree, cond_err = _build_condition(preset, condition, condition_file)
            if cond_err:
                return _validation_envelope(cond_err)
            payload["condition_json"] = tree
        if channel_id is not None:
            payload["channel_id"] = channel_id
        if chat_id is not None:
            payload["chat_id"] = chat_id
        if cooldown is not None:
            payload["cooldown_seconds"] = cooldown
        if not payload:
            return _validation_envelope("no fields to update")
        return await invoke_api(
            "PUT",
            f"/monitors/{monitor_id}",
            json=payload,
            meta=read_session_meta(),
            not_found_error_code="monitor_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@monitor.command("enable")
@click.argument("monitor_id")
def monitor_enable(monitor_id: str) -> None:
    """Enable (resume) a monitor rule."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "PUT",
            f"/monitors/{monitor_id}",
            json={"enabled": True, "status": "active"},
            meta=read_session_meta(),
            not_found_error_code="monitor_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@monitor.command("disable")
@click.argument("monitor_id")
def monitor_disable(monitor_id: str) -> None:
    """Disable (pause) a monitor rule."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "PUT",
            f"/monitors/{monitor_id}",
            json={"enabled": False, "status": "paused"},
            meta=read_session_meta(),
            not_found_error_code="monitor_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@monitor.command("delete")
@click.argument("monitor_id")
def monitor_delete(monitor_id: str) -> None:
    """Delete a monitor rule (mon-...)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "DELETE",
            f"/monitors/{monitor_id}",
            meta=read_session_meta(),
            not_found_error_code="monitor_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@monitor.command("run-once")
@click.argument("monitor_id")
def monitor_run_once(monitor_id: str) -> None:
    """Dry-run a rule against current snapshots (no alerts persisted)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            f"/monitors/{monitor_id}/run-once",
            meta=read_session_meta(),
            not_found_error_code="monitor_not_found",
            timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS,
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["monitor"]
