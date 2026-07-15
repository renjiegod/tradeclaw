"""`doyoutrade-cli strategy authoring ...` subcommands.

Exposes the 4 lifecycle operations as shell commands that call
resource-oriented OpenAPI endpoints on the API server.

Command surface::

    doyoutrade-cli strategy authoring open --name "..." [--definition-id sd-...]
    doyoutrade-cli strategy authoring cancel --session-id sess-...
    doyoutrade-cli strategy authoring compile --session-id sess-...
    doyoutrade-cli strategy authoring finalize --session-id sess-...

File primitives (read_file / write_file / edit_file / list_files) are
**in-process agent tools**, not CLI subcommands — the agent calls them
directly without shelling out. The work_dir returned by ``open`` is the
sandbox root the file tools enforce.

All lifecycle commands return a single-line JSON envelope (``ok`` /
``data`` / ``meta``) matching the standard CLI contract. Error
``error_code`` tokens flow through unchanged from the underlying tool so
callers can self-correct.
"""
from __future__ import annotations

from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command


@click.group("authoring")
def strategy_authoring() -> None:
    """Strategy authoring session lifecycle and file commands."""


# ---------------------------------------------------------------------------
# 1. open
# ---------------------------------------------------------------------------


@strategy_authoring.command("open")
@click.option(
    "--name",
    "name",
    default=None,
    help=(
        "Display name for a NEW strategy definition. "
        "Required when --definition-id is omitted."
    ),
)
@click.option(
    "--definition-id",
    "definition_id",
    default=None,
    help=(
        "Existing strategy definition id (sd-...). "
        "When supplied, copies the current version into a new draft."
    ),
)
def authoring_open(name: str | None, definition_id: str | None) -> None:
    """Open a strategy authoring session.

    When --definition-id is supplied, copies the current version into a new
    draft. When only --name is supplied, creates a new definition with the
    scaffold template.

    Returns {definition_id, session_id, work_dir, base_version, status}.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        kwargs: dict[str, Any] = {}
        if definition_id:
            kwargs["definition_id"] = definition_id
        if name:
            kwargs["name"] = name
        return await invoke_api(
            "POST",
            "/strategy-authoring/sessions",
            json=kwargs,
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ---------------------------------------------------------------------------
# 2. cancel
# ---------------------------------------------------------------------------


@strategy_authoring.command("cancel")
@click.option(
    "--session-id",
    "session_id",
    required=True,
    help="Session id returned by `authoring open`.",
)
def authoring_cancel(session_id: str) -> None:
    """Discard the draft for an authoring session.

    Removes the draft directory; the strategy definition record is untouched.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "DELETE",
            f"/strategy-authoring/sessions/{session_id}",
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ---------------------------------------------------------------------------
# 3. compile
# ---------------------------------------------------------------------------


@strategy_authoring.command("compile")
@click.option(
    "--session-id",
    "session_id",
    required=True,
    help="Session id returned by `authoring open`.",
)
def authoring_compile(session_id: str) -> None:
    """Run AST + smoke validation on the draft without persisting.

    Returns status:ok on success, or a full compiler error envelope on
    failure. The draft is preserved regardless of outcome.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            f"/strategy-authoring/sessions/{session_id}/compile",
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ---------------------------------------------------------------------------
# 4. finalize
# ---------------------------------------------------------------------------


@strategy_authoring.command("finalize")
@click.option(
    "--session-id",
    "session_id",
    required=True,
    help="Session id returned by `authoring open`.",
)
def authoring_finalize(session_id: str) -> None:
    """Validate the draft, promote it to a versioned directory, and update DB.

    On success returns status:ok with version_label and definition_id.
    On compile failure the draft is preserved and the error envelope is returned.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            f"/strategy-authoring/sessions/{session_id}/finalize",
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["strategy_authoring"]
