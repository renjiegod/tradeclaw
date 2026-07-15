"""`doyoutrade-cli strategy ...` subcommands.

Talks to the API server's resource-oriented OpenAPI endpoints so the assistant
can manage strategy definitions and bindings through ``execute_bash``. Scope:

* ``strategy definition get <sd-id>``
* ``strategy definition list``
* ``strategy definition create``  — metadata-only; source code goes through
  the authoring lifecycle (``doyoutrade-cli strategy authoring open`` →
  in-process file tools → ``doyoutrade-cli strategy authoring finalize``)
* ``strategy definition update <sd-id>`` — metadata-only patch
* ``strategy bind <task_id> <sd-id>``
* ``strategy promote <task_id> <sd-id>``

Tasks bind directly to a strategy definition (``sd-``); the standalone
strategy-instance (``si-``) concept has been removed.

The ``create`` / ``update`` definition commands are metadata-only.  Strategy
source code is authored via the in-process authoring lifecycle and is never
uploaded through the CLI.
"""

from __future__ import annotations

from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._format import write_envelope
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli._kwargs import (
    exit_for_invalid_params,
    parse_params_json,
)
from doyoutrade.cli.main import run_async_command


@click.group()
def strategy() -> None:
    """Strategy definition / binding / authoring commands."""


# Register the authoring subgroup at import time so it is available to
# any caller that imports ``strategy`` directly (e.g. tests).
def _register_authoring_subgroup() -> None:
    from doyoutrade.cli.commands.strategy_authoring import strategy_authoring  # noqa: PLC0415
    strategy.add_command(strategy_authoring)


_register_authoring_subgroup()


# ---------------------------------------------------------------------------
# Definition (read-only in Phase 2)
# ---------------------------------------------------------------------------


@strategy.group("definition")
def strategy_definition() -> None:
    """Strategy definition commands."""


@strategy_definition.command("get")
@click.argument("definition_id")
def strategy_definition_get(definition_id: str) -> None:
    """Fetch a strategy definition by sd-... id, including its ``code_hash`` and ``current_version``."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("GET", f"/strategy-definitions/{definition_id}", meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@strategy_definition.command("list")
@click.option(
    "--query",
    "query",
    default=None,
    help="Fuzzy search across definition fields (whitespace-separated tokens AND-matched).",
)
@click.option(
    "--limit",
    "limit",
    default=None,
    type=click.IntRange(min=1),
    help="Truncate the returned definitions to the first N rows (after filtering).",
)
def strategy_definition_list(query: str | None, limit: int | None) -> None:
    """List strategy definitions (filtered view of ``strategy inspect``).

    Equivalent to ``doyoutrade-cli strategy inspect [--query ...]``. For a
    single record, use ``strategy definition get <sd-id>``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        params: dict[str, Any] = {}
        envelope, exit_code = await invoke_api("GET", "/strategy-definitions", params=params, meta=read_session_meta())
        if not envelope.get("ok"):
            return envelope, exit_code
        data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
        rows = list(data.get("items") or [])
        total_before_filter = len(rows)
        tokens: list[str] = []
        if query:
            tokens = [token.lower() for token in query.split() if token.strip()]
            rows = [
                row for row in rows
                if all(token in " ".join(str(value).lower() for value in row.values()) for token in tokens)
            ]
        if limit is not None:
            rows = rows[:limit]
        summary = f"Listed {len(rows)} definition(s)"
        if query:
            summary += f" for query='{query}'"
        if limit is not None:
            summary += f" (limit={limit})"
        summary += "."
        envelope["data"] = {
            "definitions": rows,
            "query": query,
            "matched_tokens": tokens,
            "total_definitions": total_before_filter,
            "_summary": summary,
        }
        return envelope, exit_code

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


def _emit_invalid_flag_json(flag_label: str, error_code: str, err: dict[str, Any]) -> None:
    """Retag a parse_params_json error so the agent knows which flag was bad."""

    if isinstance(err.get("error"), dict):
        err["error"]["error_code"] = error_code
        msg = err["error"].get("message", "") or ""
        err["error"]["message"] = msg.replace("--params", flag_label)
    _emit_params_error(err)


@strategy_definition.command("create")
@click.option("--name", "definition_name", required=True, help="Display name for the new definition.")
@click.option(
    "--definition-id",
    "definition_id",
    default=None,
    help="Optional pre-chosen sd-... id (auto-generated when omitted).",
)
@click.option(
    "--params-schema",
    "params_schema_json",
    default=None,
    help="JSON object describing the parameter schema. Overrides the descriptor-derived schema.",
)
@click.option(
    "--default-params",
    "default_params_json",
    default=None,
    help="JSON object of default parameter values applied when a task binding does not override a key.",
)
@click.option(
    "--capabilities",
    "capabilities_json",
    default=None,
    help="JSON object stored on the definition as capabilities/constraints.",
)
@click.option(
    "--provenance",
    "provenance_json",
    default=None,
    help="JSON object stored as provenance metadata (defaults to {'source':'assistant'}).",
)
@click.option("--status", "status", default=None, help="Initial lifecycle status (default: active).")
def strategy_definition_create(
    definition_name: str,
    definition_id: str | None,
    params_schema_json: str | None,
    default_params_json: str | None,
    capabilities_json: str | None,
    provenance_json: str | None,
    status: str | None,
) -> None:
    """Create a metadata-only strategy definition.

    The strategy code itself is authored via the assistant authoring lifecycle
    (``doyoutrade-cli strategy authoring open`` → in-process file tools →
    ``doyoutrade-cli strategy authoring finalize``) which materializes code on disk and bumps
    ``current_version``.  The ``--source-file`` / ``--class-name`` flags were
    removed in the strategy-as-files refactor (Task 6, 2026-05-24).
    """

    parameter_schema, err = parse_params_json(params_schema_json)
    if err is not None:
        _emit_invalid_flag_json("--params-schema", "invalid_params_schema_json", err)
        return
    default_parameters, err = parse_params_json(default_params_json)
    if err is not None:
        _emit_invalid_flag_json("--default-params", "invalid_default_params_json", err)
        return
    capabilities, err = parse_params_json(capabilities_json)
    if err is not None:
        _emit_invalid_flag_json("--capabilities", "invalid_capabilities_json", err)
        return
    provenance, err = parse_params_json(provenance_json)
    if err is not None:
        _emit_invalid_flag_json("--provenance", "invalid_provenance_json", err)
        return

    kwargs: dict[str, Any] = {"name": definition_name}
    if definition_id:
        kwargs["definition_id"] = definition_id
    if parameter_schema is not None:
        kwargs["parameter_schema"] = parameter_schema
    if default_parameters is not None:
        kwargs["default_parameters"] = default_parameters
    if capabilities is not None:
        kwargs["capabilities"] = capabilities
    if provenance is not None:
        kwargs["provenance"] = provenance
    if status is not None:
        kwargs["status"] = status

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("POST", "/strategy-definitions", json=kwargs, meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@strategy_definition.command("update")
@click.argument("definition_id")
@click.option("--name", "definition_name", default=None, help="New display name.")
@click.option(
    "--params-schema",
    "params_schema_json",
    default=None,
    help="Replacement parameter schema JSON object.",
)
@click.option(
    "--default-params",
    "default_params_json",
    default=None,
    help="Replacement default-parameters JSON object.",
)
@click.option(
    "--capabilities",
    "capabilities_json",
    default=None,
    help="Replacement capabilities JSON object.",
)
@click.option(
    "--provenance",
    "provenance_json",
    default=None,
    help="Replacement provenance JSON object.",
)
@click.option("--status", "status", default=None, help="New lifecycle status (e.g. draft / active / deprecated).")
def strategy_definition_update(
    definition_id: str,
    definition_name: str | None,
    params_schema_json: str | None,
    default_params_json: str | None,
    capabilities_json: str | None,
    provenance_json: str | None,
    status: str | None,
) -> None:
    """Update an existing strategy definition (metadata patch semantics).

    Only supplied flags are written.  Source-code changes go through the
    authoring lifecycle (``doyoutrade-cli strategy authoring open`` → in-process file tools →
    ``doyoutrade-cli strategy authoring finalize``).  The ``--source-file``
    and ``--class-name`` flags were removed in the strategy-as-files refactor
    (Task 6, 2026-05-24).
    """

    parameter_schema, err = parse_params_json(params_schema_json)
    if err is not None:
        _emit_invalid_flag_json("--params-schema", "invalid_params_schema_json", err)
        return
    default_parameters, err = parse_params_json(default_params_json)
    if err is not None:
        _emit_invalid_flag_json("--default-params", "invalid_default_params_json", err)
        return
    capabilities, err = parse_params_json(capabilities_json)
    if err is not None:
        _emit_invalid_flag_json("--capabilities", "invalid_capabilities_json", err)
        return
    provenance, err = parse_params_json(provenance_json)
    if err is not None:
        _emit_invalid_flag_json("--provenance", "invalid_provenance_json", err)
        return

    kwargs: dict[str, Any] = {"definition_id": definition_id}
    if definition_name is not None:
        kwargs["name"] = definition_name
    if parameter_schema is not None:
        kwargs["parameter_schema"] = parameter_schema
    if default_parameters is not None:
        kwargs["default_parameters"] = default_parameters
    if capabilities is not None:
        kwargs["capabilities"] = capabilities
    if provenance is not None:
        kwargs["provenance"] = provenance
    if status is not None:
        kwargs["status"] = status

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api("PATCH", f"/strategy-definitions/{definition_id}", json=kwargs, meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


def _emit_params_error(err: dict[str, Any]) -> None:
    """Inject session meta and emit the invalid_params_json envelope, then exit."""

    meta_dict = read_session_meta().to_dict()
    if meta_dict:
        err["meta"] = meta_dict
    fmt = click.get_current_context().find_root().obj.get("fmt", "json")
    write_envelope(err, fmt=fmt)
    click.get_current_context().exit(exit_for_invalid_params(err))


# ---------------------------------------------------------------------------
# Binding / promotion
# ---------------------------------------------------------------------------


@strategy.command("bind")
@click.argument("task_id")
@click.argument("definition_id")
def strategy_bind(task_id: str, definition_id: str) -> None:
    """Bind a strategy definition (sd-...) to a task (writes settings.strategy.definition_id)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "PUT",
            f"/tasks/{task_id}",
            json={"settings": {"strategy": {"definition_id": definition_id}}},
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@strategy.command("promote")
@click.argument("task_id")
@click.argument("definition_id")
@click.option(
    "--approval-policy",
    "approval_policy_json",
    default=None,
    help="JSON object written under settings.strategy.approval_policy.",
)
@click.option(
    "--risk-overrides",
    "risk_overrides_json",
    default=None,
    help="JSON object written under settings.strategy.risk_overrides.",
)
def strategy_promote(
    task_id: str,
    definition_id: str,
    approval_policy_json: str | None,
    risk_overrides_json: str | None,
) -> None:
    """Promote a strategy definition (sd-...) binding to a live task with optional policy patches.

    Writes ``settings.strategy.definition_id`` and, when supplied, patches the
    ``approval_policy`` / ``risk_overrides`` blocks. Only the explicitly
    provided flags are written (patch semantics); omitted flags leave the
    existing values untouched.
    """

    approval_policy, err = parse_params_json(approval_policy_json)
    if err is not None:
        # Re-tag the error code so the skill docs distinguish which flag failed.
        if isinstance(err.get("error"), dict):
            err["error"]["error_code"] = "invalid_approval_policy_json"
            err["error"]["message"] = (
                err["error"].get("message", "") .replace("--params", "--approval-policy")
            )
        _emit_params_error(err)
        return
    risk_overrides, err = parse_params_json(risk_overrides_json)
    if err is not None:
        if isinstance(err.get("error"), dict):
            err["error"]["error_code"] = "invalid_risk_overrides_json"
            err["error"]["message"] = (
                err["error"].get("message", "").replace("--params", "--risk-overrides")
            )
        _emit_params_error(err)
        return

    async def _run() -> tuple[dict[str, Any], int]:
        strategy_patch: dict[str, Any] = {"definition_id": definition_id}
        if approval_policy is not None:
            strategy_patch["approval_policy"] = approval_policy
        if risk_overrides is not None:
            strategy_patch["risk_overrides"] = risk_overrides
        return await invoke_api(
            "PUT",
            f"/tasks/{task_id}",
            json={"settings": {"strategy": strategy_patch}},
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["strategy"]
