"""`doyoutrade-cli account ...` subcommands.

QMT accounts (proxy connection + trading identity + live/mock mode) are
persisted in the ``accounts`` DB table and managed through the running API
server's ``/accounts`` endpoints. A task selects one via ``account_id``; the
account marked default supplies the market-data connection for account-less
paths (backtest / data run / screening).

All commands route through the API server (shared base-URL resolution with
``doyoutrade/cli/_api.py``): env ``DOYOUTRADE_API_URL`` → ``cfg.api.base_url`` →
derived from ``cfg.server``. When the server isn't running the CLI emits a
structured ``api_unavailable`` envelope instead of a transport traceback.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._envelope import (
    EXIT_VALIDATION,
    error_envelope,
)
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command


@click.group()
def account() -> None:
    """QMT account management via API server."""


def _parse_json_opt(raw: str | None, field: str) -> tuple[Any, str | None]:
    if raw is None:
        return None, None
    text = raw.strip()
    if not text:
        return None, None
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"invalid_{field}_json: {exc}"


def _build_payload(
    *,
    name: str | None,
    mode: str | None,
    base_url: str | None,
    token: str | None,
    qmt_account_id: str | None,
    session_id: str | None,
    timeout_seconds: float | None,
    mock_cash: float | None,
    mock_equity: float | None,
    mock_positions: str | None,
    is_default: bool | None,
    enabled: bool | None,
) -> tuple[dict[str, Any], str | None]:
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if mode is not None:
        payload["mode"] = mode
    if base_url is not None:
        payload["base_url"] = base_url
    if token is not None:
        payload["token"] = token
    if qmt_account_id is not None:
        payload["qmt_account_id"] = qmt_account_id
    if session_id is not None:
        payload["session_id"] = session_id
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
    if mock_cash is not None:
        payload["mock_cash"] = mock_cash
    if mock_equity is not None:
        payload["mock_equity"] = mock_equity
    if mock_positions is not None:
        parsed, err = _parse_json_opt(mock_positions, "mock_positions")
        if err is not None:
            return {}, err
        payload["mock_positions"] = parsed
    if is_default is not None:
        payload["is_default"] = is_default
    if enabled is not None:
        payload["enabled"] = enabled
    return payload, None


def _validation_envelope(message: str) -> tuple[dict[str, Any], int]:
    return (
        error_envelope(
            error_code="validation_error", message=message, meta=read_session_meta()
        ),
        EXIT_VALIDATION,
    )


# ── Read commands ──────────────────────────────────────────────────────────


@account.command("list")
def account_list() -> None:
    """List all accounts."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("GET", "/accounts", meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@account.command("get")
@click.argument("account_id")
def account_get(account_id: str) -> None:
    """Get an account by id (acct-...)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/accounts/{account_id}",
            meta=read_session_meta(),
            not_found_error_code="account_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@account.command("statement")
@click.option(
    "--account",
    "account_id",
    default=None,
    help="Account id to query. Omit to use the enabled default account.",
)
@click.option(
    "--asof",
    "asof_raw",
    default=None,
    help="Trading day YYYY-MM-DD. Omit to use today.",
)
def account_statement(account_id: str | None, asof_raw: str | None) -> None:
    """Fetch a QMT-backed account statement (account/asset/positions/trades)."""

    async def _run() -> tuple[dict[str, Any], int]:
        params: dict[str, Any] = {}
        normalized_account_id = (account_id or "").strip()
        if normalized_account_id:
            params["account_id"] = normalized_account_id
        if asof_raw is not None:
            text = asof_raw.strip()
            try:
                params["asof"] = datetime.strptime(text, "%Y-%m-%d").date().isoformat()
            except ValueError:
                return _validation_envelope("--asof must be YYYY-MM-DD")
        return await invoke_api(
            "GET",
            "/accounts/statement",
            params=params,
            meta=read_session_meta(),
            not_found_error_code="account_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ── Write commands ─────────────────────────────────────────────────────────

_WRITE_OPTIONS = [
    click.option("--name", default=None, help="Human-readable account label."),
    click.option(
        "--mode",
        type=click.Choice(["live", "mock"]),
        default=None,
        help="live: real QMT trading terminal; mock: simulated portfolio.",
    ),
    click.option("--base-url", "base_url", default=None, help="QMT proxy base URL."),
    click.option("--token", default=None, help="QMT proxy API token (plaintext)."),
    click.option(
        "--qmt-account-id",
        "qmt_account_id",
        default=None,
        help="Broker trading account id (used for live trading.connect).",
    ),
    click.option("--session-id", "session_id", default=None, help="Trading session id."),
    click.option("--timeout-seconds", "timeout_seconds", type=float, default=None),
    click.option("--mock-cash", "mock_cash", type=float, default=None),
    click.option("--mock-equity", "mock_equity", type=float, default=None),
    click.option(
        "--mock-positions",
        "mock_positions",
        default=None,
        help='JSON list, e.g. \'[{"symbol":"600000.SH","quantity":100,"cost_price":10}]\'.',
    ),
    click.option(
        "--default/--no-default",
        "is_default",
        default=None,
        help="Make this the default account (supplies market-data connection).",
    ),
    click.option("--enabled/--disabled", "enabled", default=None),
]


def _apply_write_options(func):
    for option in reversed(_WRITE_OPTIONS):
        func = option(func)
    return func


@account.command("create")
@_apply_write_options
def account_create(**kwargs: Any) -> None:
    """Create an account. --name and --mode are required."""

    async def _run() -> tuple[dict[str, Any], int]:
        if not kwargs.get("name"):
            return _validation_envelope("--name is required")
        if not kwargs.get("mode"):
            return _validation_envelope("--mode is required (live|mock)")
        payload, err = _build_payload(**kwargs)
        if err is not None:
            return _validation_envelope(err)
        return await invoke_api(
            "POST", "/accounts", json=payload, meta=read_session_meta()
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@account.command("update")
@click.argument("account_id")
@_apply_write_options
def account_update(account_id: str, **kwargs: Any) -> None:
    """Update an account (only supplied fields change)."""

    async def _run() -> tuple[dict[str, Any], int]:
        payload, err = _build_payload(**kwargs)
        if err is not None:
            return _validation_envelope(err)
        if not payload:
            return _validation_envelope("no fields to update")
        return await invoke_api(
            "PUT",
            f"/accounts/{account_id}",
            json=payload,
            meta=read_session_meta(),
            not_found_error_code="account_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@account.command("set-default")
@click.argument("account_id")
def account_set_default(account_id: str) -> None:
    """Make this account the sole default (clears the flag on all others)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            f"/accounts/{account_id}/set-default",
            meta=read_session_meta(),
            not_found_error_code="account_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@account.command("delete")
@click.argument("account_id")
def account_delete(account_id: str) -> None:
    """Delete an account (refused with account_in_use if a task binds it)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "DELETE",
            f"/accounts/{account_id}",
            meta=read_session_meta(),
            not_found_error_code="account_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))
