"""`doyoutrade-cli watchlist ...` subcommands.

The watchlist is a single curated pool of A-share symbols, each tagged for
grouping, with an optional display name / note / sort order. Entries carry the
``wl-`` id prefix and are persisted in the ``watchlist_entries`` DB table; all
commands route through the running API server's ``/watchlist`` endpoints (and
``/market/quotes`` for the realtime snapshot used by ``watchlist quotes``).

Like the other CRUD command groups (``account``), this stays a thin
command-line / envelope adapter over the API: shared base-URL resolution lives
in ``doyoutrade/cli/_api.py`` (env ``DOYOUTRADE_API_URL`` → ``cfg.api.base_url`` →
derived from ``cfg.server``). When the server isn't running the CLI emits a
structured ``api_unavailable`` envelope instead of a transport traceback.
"""

from __future__ import annotations

import json
from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._envelope import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_VALIDATION,
    error_envelope,
    success_envelope,
)
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.commands.stock import _read_universe_file
from doyoutrade.cli.main import run_async_command

# Adding a whole universe-file of symbols fans out one POST /watchlist per
# symbol. Mirror data.py's batch budget so a large add doesn't trip the snappy
# 15s control-call default while an agent's execute_bash call waits much longer.
_DATA_FETCH_TIMEOUT_SECONDS = 180.0


@click.group()
def watchlist() -> None:
    """Watchlist (自选股) management via API server."""


def _validation_envelope(message: str) -> tuple[dict[str, Any], int]:
    return (
        error_envelope(
            error_code="validation_error", message=message, meta=read_session_meta()
        ),
        EXIT_VALIDATION,
    )


def _parse_tags(raw: str | None) -> tuple[list[str] | None, str | None]:
    """Parse ``--tags`` into a ``list[str]``.

    Accepts a comma-separated list (``a,b,c``) or a JSON array string
    (``["a","b"]``). Returns ``(None, None)`` when not supplied, or
    ``(None, error_message)`` when the value can't be coerced to a list of
    strings — never silently drops a malformed value (CLAUDE.md 错误可见性).
    """

    if raw is None:
        return None, None
    text = raw.strip()
    if not text:
        return [], None
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"invalid_tags_json: {exc}"
        if not isinstance(parsed, list):
            return None, f"invalid_tags_json: expected a JSON array, got {type(parsed).__name__}"
        tags = [str(item).strip() for item in parsed if str(item).strip()]
        return tags, None
    tags = [part.strip() for part in text.split(",") if part.strip()]
    return tags, None


# ── Read commands ────────────────────────────────────────────────────────────


@watchlist.command("list")
@click.option("--tag", "tag", default=None, help="Only entries carrying this tag.")
def watchlist_list(tag: str | None) -> None:
    """List watchlist entries, optionally filtered by --tag."""

    async def _run() -> tuple[dict[str, Any], int]:
        params: dict[str, Any] | None = {"tag": tag} if tag else None
        return await invoke_api(
            "GET", "/watchlist", params=params, meta=read_session_meta()
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@watchlist.command("get")
@click.argument("entry_id")
def watchlist_get(entry_id: str) -> None:
    """Get a watchlist entry by id (wl-...)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/watchlist/{entry_id}",
            meta=read_session_meta(),
            not_found_error_code="watchlist_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@watchlist.command("tags")
def watchlist_tags() -> None:
    """List distinct tags across the watchlist with per-tag counts."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("GET", "/watchlist/tags", meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ── Write commands ───────────────────────────────────────────────────────────


@watchlist.command("add")
@click.argument("symbol", required=False)
@click.option(
    "--universe-file",
    "universe_file",
    default=None,
    type=click.Path(dir_okay=False, readable=True),
    help=(
        "Path to a file with one canonical CODE.EXCHANGE per line "
        "(# comments allowed). Adds every symbol (one POST each). Mutually "
        "exclusive with the positional SYMBOL argument."
    ),
)
@click.option(
    "--tags",
    "tags",
    default=None,
    help="Comma-separated list (a,b,c) or a JSON array string. Applied to all symbols.",
)
@click.option("--note", "note", default=None, help="Free-text note.")
@click.option("--display-name", "display_name", default=None, help="Override display name.")
@click.option("--sort-order", "sort_order", type=int, default=None, help="Manual sort order (lower first).")
def watchlist_add(
    symbol: str | None,
    universe_file: str | None,
    tags: str | None,
    note: str | None,
    display_name: str | None,
    sort_order: int | None,
) -> None:
    """Add one symbol, or many via --universe-file.

    Two symbol-input modes (mutually exclusive):

    * positional ``SYMBOL`` — single canonical CODE.EXCHANGE, e.g. ``600519.SH``.
    * ``--universe-file path.txt`` — one CODE.EXCHANGE per line, ``#`` comments;
      every symbol is POSTed in turn and the results are returned together.

    ``--tags`` / ``--note`` / ``--display-name`` / ``--sort-order`` are applied
    to every symbol added in this invocation.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        if symbol and universe_file:
            return _validation_envelope(
                "pass exactly one of SYMBOL or --universe-file, not both"
            )
        if not symbol and not universe_file:
            return _validation_envelope(
                "a SYMBOL argument or --universe-file is required"
            )

        parsed_tags, tags_err = _parse_tags(tags)
        if tags_err is not None:
            return _validation_envelope(tags_err)

        base_payload: dict[str, Any] = {}
        if parsed_tags is not None:
            base_payload["tags"] = parsed_tags
        if note is not None:
            base_payload["note"] = note
        if display_name is not None:
            base_payload["display_name"] = display_name
        if sort_order is not None:
            base_payload["sort_order"] = sort_order

        meta = read_session_meta()

        if symbol:
            payload = dict(base_payload)
            payload["symbol"] = symbol
            return await invoke_api(
                "POST", "/watchlist", json=payload, meta=meta
            )

        # Batch mode: one POST per symbol; surface each result per symbol so a
        # partial failure (e.g. duplicate_watchlist_symbol) stays visible.
        if not universe_file:
            return _validation_envelope(
                "provide a SYMBOL argument or --universe-file"
            )
        try:
            symbols = _read_universe_file(universe_file)
        except click.BadParameter as exc:
            return _validation_envelope(str(exc))

        results: list[dict[str, Any]] = []
        added = 0
        failed = 0
        for sym in symbols:
            payload = dict(base_payload)
            payload["symbol"] = sym
            envelope, _exit = await invoke_api(
                "POST",
                "/watchlist",
                json=payload,
                meta=meta,
                timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS,
            )
            ok = bool(envelope.get("ok"))
            if ok:
                added += 1
            else:
                failed += 1
            results.append(
                {
                    "symbol": sym,
                    "ok": ok,
                    "entry": envelope.get("data") if ok else None,
                    "error": envelope.get("error") if not ok else None,
                }
            )

        status = "ok" if failed == 0 else ("partial" if added else "failed")
        data = {
            "status": status,
            "added_count": added,
            "failed_count": failed,
            "requested_count": len(symbols),
            "results": results,
        }
        summary = f"watchlist add batch: added={added} failed={failed} requested={len(symbols)}"
        exit_code = EXIT_OK if added else EXIT_FAILURE
        return success_envelope(data, summary, meta=meta), exit_code

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@watchlist.command("update")
@click.argument("entry_id")
@click.option(
    "--tags",
    "tags",
    default=None,
    help="Comma-separated list (a,b,c) or a JSON array string. Replaces existing tags.",
)
@click.option("--note", "note", default=None, help="Free-text note.")
@click.option("--display-name", "display_name", default=None, help="Override display name.")
@click.option("--sort-order", "sort_order", type=int, default=None, help="Manual sort order (lower first).")
def watchlist_update(
    entry_id: str,
    tags: str | None,
    note: str | None,
    display_name: str | None,
    sort_order: int | None,
) -> None:
    """Update a watchlist entry (only supplied fields change; patch semantics)."""

    async def _run() -> tuple[dict[str, Any], int]:
        payload: dict[str, Any] = {}
        if tags is not None:
            parsed_tags, tags_err = _parse_tags(tags)
            if tags_err is not None:
                return _validation_envelope(tags_err)
            payload["tags"] = parsed_tags
        if note is not None:
            payload["note"] = note
        if display_name is not None:
            payload["display_name"] = display_name
        if sort_order is not None:
            payload["sort_order"] = sort_order
        if not payload:
            return _validation_envelope("no fields to update")
        return await invoke_api(
            "PUT",
            f"/watchlist/{entry_id}",
            json=payload,
            meta=read_session_meta(),
            not_found_error_code="watchlist_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@watchlist.command("remove")
@click.argument("entry_id")
def watchlist_remove(entry_id: str) -> None:
    """Remove a watchlist entry by id (wl-...)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "DELETE",
            f"/watchlist/{entry_id}",
            meta=read_session_meta(),
            not_found_error_code="watchlist_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ── Quotes ───────────────────────────────────────────────────────────────────


@watchlist.command("quotes")
@click.option("--tag", "tag", default=None, help="Quote every watchlist symbol carrying this tag.")
@click.option(
    "--symbols",
    "symbols",
    default=None,
    help="Comma-separated list (A.SH,B.SZ) or a JSON array string.",
)
@click.option(
    "--universe-file",
    "universe_file",
    default=None,
    type=click.Path(dir_okay=False, readable=True),
    help="Path to a file with one canonical CODE.EXCHANGE per line (# comments allowed).",
)
def watchlist_quotes(
    tag: str | None,
    symbols: str | None,
    universe_file: str | None,
) -> None:
    """Fetch a one-shot realtime quote snapshot for a set of symbols.

    Three input modes (mutually exclusive):

    * ``--tag T`` — first resolves the watchlist symbols carrying tag ``T``
      (GET /watchlist?tag=T), then quotes them.
    * ``--symbols A.SH,B.SZ`` — explicit list (comma-separated or JSON array).
    * ``--universe-file path.txt`` — one CODE.EXCHANGE per line, ``#`` comments.

    Symbols are passed to ``GET /market/quotes`` as repeated ``symbol`` query
    params. Quotes are sourced from qmt-proxy only; when qmt is not connected
    the snapshot still returns with a ``qmt_disconnected`` status per item.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        provided = [v is not None for v in (tag, symbols, universe_file)]
        if sum(1 for v in provided if v) > 1:
            return _validation_envelope(
                "pass exactly one of --tag / --symbols / --universe-file"
            )
        if not any(provided):
            return _validation_envelope(
                "one of --tag / --symbols / --universe-file is required"
            )

        meta = read_session_meta()

        resolved: list[str]
        if tag is not None:
            entries_env, exit_code = await invoke_api(
                "GET", "/watchlist", params={"tag": tag}, meta=meta
            )
            if not entries_env.get("ok"):
                return entries_env, exit_code
            data = entries_env.get("data") or {}
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return _validation_envelope(
                    f"watchlist tag lookup returned no items for tag={tag!r}"
                )
            resolved = [
                str(item.get("symbol"))
                for item in items
                if isinstance(item, dict) and item.get("symbol")
            ]
            if not resolved:
                return _validation_envelope(
                    f"no watchlist symbols carry tag={tag!r}"
                )
        elif universe_file is not None:
            try:
                resolved = _read_universe_file(universe_file)
            except click.BadParameter as exc:
                return _validation_envelope(str(exc))
        else:
            text = (symbols or "").strip()
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    return _validation_envelope(f"invalid_symbols_json: {exc}")
                if not isinstance(parsed, list):
                    return _validation_envelope(
                        f"invalid_symbols_json: expected a JSON array, got {type(parsed).__name__}"
                    )
                resolved = [str(s).strip() for s in parsed if str(s).strip()]
            else:
                resolved = [part.strip() for part in text.split(",") if part.strip()]
            if not resolved:
                return _validation_envelope("--symbols contained no symbols")

        # /market/quotes takes ``symbol`` as a repeated query param. httpx
        # serializes a list value into repeated params (?symbol=A&symbol=B).
        return await invoke_api(
            "GET",
            "/market/quotes",
            params={"symbol": resolved},
            meta=meta,
            timeout_seconds=_DATA_FETCH_TIMEOUT_SECONDS,
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["watchlist"]
