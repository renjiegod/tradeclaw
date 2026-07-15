"""`doyoutrade-cli task ...` subcommands.

Commands route through resource-oriented OpenAPI endpoints. The CLI stays
responsible for parsing flags and shaping request payloads; the API server
owns runtime state and validation.

Common flag pattern for write commands:

* Scalars live on flags: ``--name``, ``--mode``, ``--description``,
  ``--data-provider``.
* Sugar for the most common nested case:
  ``--strategy-definition sd-...`` expands to ``strategy.definition_id``.
* Lists arrive comma-separated: ``--universe SYM1,SYM2``.
* Anything more complex (``agent`` block, ``strategy.execution_profile``,
  ``parameter_overrides``) goes through ``--params '<json>'`` and merges
  with the flat flags. Explicit flags always win over ``--params``.
"""

from __future__ import annotations

from typing import Any

import click

from doyoutrade.cli._envelope import error_envelope, exit_code_for_error
from doyoutrade.cli._format import write_envelope
from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli._kwargs import (
    exit_for_invalid_params,
    merge_flat_over_params,
    parse_params_json,
    split_csv,
)
from doyoutrade.cli.main import run_async_command


@click.group()
def task() -> None:
    """Trading task lifecycle commands."""


def _task_identifier_guard(identifier: str) -> tuple[dict[str, Any], int] | None:
    """Return the legacy task-id shape error without contacting runtime/API."""

    from doyoutrade.tools import wrong_identifier_type_error

    err = wrong_identifier_type_error(identifier)
    if err is None:
        return None
    code = str(err.get("error_code") or "wrong_identifier_type")
    envelope = error_envelope(
        error_code=code,
        error_type=str(err.get("error_type") or "WrongIdentifierType"),
        message=str(err.get("error") or "wrong identifier type"),
        repair_hints=err.get("repair_hints") if isinstance(err.get("repair_hints"), list) else None,
        meta=read_session_meta(),
    )
    return envelope, exit_code_for_error(code)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@task.command("get")
@click.argument("identifier")
def task_get(identifier: str) -> None:
    """Get a task by task_id (UUID) or exact task name."""

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(identifier)
        if guard is not None:
            return guard
        return await invoke_api(
            "GET",
            f"/tasks/{identifier}",
            meta=read_session_meta(),
            not_found_error_code="task_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@task.command("list")
@click.option("--q", "q", default=None, help="Substring filter on task name.")
@click.option("--status", "status", default=None, help="Exact status filter.")
@click.option("--mode", "mode", default=None, help="Run mode filter.")
@click.option(
    "--definition",
    "definition_id",
    default=None,
    help="Strategy definition id filter (sd-...).",
)
@click.option("--limit", "limit", type=int, default=20, show_default=True, help="Page size (1-200).")
@click.option("--offset", "offset", type=int, default=0, show_default=True, help="Page offset.")
def task_list(
    q: str | None,
    status: str | None,
    mode: str | None,
    definition_id: str | None,
    limit: int,
    offset: int,
) -> None:
    """List trading tasks with optional filters and pagination."""

    async def _run() -> tuple[dict[str, Any], int]:
        kwargs: dict[str, Any] = {"limit": limit, "offset": offset}
        if q is not None:
            kwargs["q"] = q
        if status is not None:
            kwargs["status"] = status
        if mode is not None:
            kwargs["mode"] = mode
        if definition_id is not None:
            kwargs["definition_id"] = definition_id
        return await invoke_api("GET", "/tasks/page", params=kwargs, meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _strategy_block(
    params: dict[str, Any],
    definition_id: str | None = None,
    flat_param_overrides: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Merge CLI strategy binding flags into ``settings.strategy``.

    ``flat_param_overrides`` captures top-level keys from ``--params`` that
    are not known settings keys — when ``--definition`` is used, those become
    ``parameter_overrides`` so callers can pass ``--params '{"window": 14}'``.
    """

    existing = params.get("strategy")
    if (
        existing is None
        and not definition_id
        and not flat_param_overrides
    ):
        return None
    if existing is None:
        block: dict[str, Any] = {}
    elif not isinstance(existing, dict):
        return existing
    else:
        block = dict(existing)
    if definition_id:
        block["definition_id"] = definition_id
    overrides: dict[str, Any] = {}
    nested = block.get("parameter_overrides")
    if isinstance(nested, dict):
        overrides.update(nested)
    if flat_param_overrides:
        overrides.update(flat_param_overrides)
    if overrides:
        block["parameter_overrides"] = overrides
    return block or None


_FLAT_STRATEGY_PARAM_KEYS = frozenset({
    "agent",
    "strategy",
    "universe",
    "strategy_preferences",
    "position_constraints",
    "context_compaction",
    "model_route_name",
    "execution_strategy",
    "account_id",
})


def _extract_flat_strategy_param_overrides(params: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in params.items()
        if key not in _FLAT_STRATEGY_PARAM_KEYS
    }


_SETTINGS_KEYS = (
    "agent",
    "strategy",
    "strategy_preferences",
    "universe",
    "position_constraints",
    "context_compaction",
    "account_id",
)


def _task_settings_from_kwargs(kwargs: dict[str, Any]) -> dict[str, Any] | Any:
    settings = kwargs.get("settings")
    if settings is None:
        out: dict[str, Any] = {}
    elif isinstance(settings, dict):
        out = dict(settings)
    else:
        return settings
    for key in _SETTINGS_KEYS:
        if key in kwargs and kwargs[key] is not None:
            out[key] = kwargs[key]
    return out or None


def _task_payload_from_kwargs(kwargs: dict[str, Any], *, require_settings: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("name", "mode", "description", "data_provider"):
        value = kwargs.get(key)
        if value is not None:
            payload[key] = value
    settings = _task_settings_from_kwargs(kwargs)
    if settings is not None or require_settings:
        payload["settings"] = settings or {}
    return payload


@task.command("create")
@click.option("--name", required=True, help="Human-readable task name.")
@click.option(
    "--definition",
    "strategy_definition",
    default=None,
    help="Strategy definition id (sd-...). Bind code directly with optional --params overrides.",
)
@click.option(
    "--mode",
    default=None,
    help="Run mode (paper / backtest / live / signal_only). Default 'paper'.",
)
@click.option("--description", default=None, help="Free-text description.")
@click.option("--data-provider", "data_provider", default=None, help="Data provider (auto / qmt / mock / akshare).")
@click.option(
    "--account",
    "account_id",
    default=None,
    help="Account id (acct-...) this task runs against. Omit to use the default "
    "account. The account record carries the live/mock mode and QMT connection.",
)
@click.option(
    "--universe",
    default=None,
    help="Comma-separated symbols, e.g. 600519.SH,000001.SZ.",
)
@click.option(
    "--params",
    "params_json",
    default=None,
    help=(
        "JSON merged into the request. With --definition, a flat object like "
        '\'{"window": 14}\' is treated as strategy parameter_overrides.'
    ),
)
def task_create(
    name: str,
    strategy_definition: str | None,
    mode: str | None,
    description: str | None,
    data_provider: str | None,
    account_id: str | None,
    universe: str | None,
    params_json: str | None,
) -> None:
    """Create a new trading task."""

    params, err = parse_params_json(params_json)
    if err is not None:
        meta_dict = read_session_meta().to_dict()
        if meta_dict:
            err["meta"] = meta_dict
        fmt = click.get_current_context().find_root().obj.get("fmt", "json")
        write_envelope(err, fmt=fmt)
        click.get_current_context().exit(exit_for_invalid_params(err))
        return

    params_dict = dict(params or {})
    flat_overrides = (
        _extract_flat_strategy_param_overrides(params_dict)
        if strategy_definition
        else None
    )
    if flat_overrides:
        for key in flat_overrides:
            params_dict.pop(key, None)

    universe_list = split_csv(universe)
    strategy_block = _strategy_block(
        params_dict,
        strategy_definition,
        flat_overrides,
    )

    flat: dict[str, Any] = {
        "name": name,
        "mode": mode,
        "description": description,
        "data_provider": data_provider,
        "account_id": account_id,
        "universe": universe_list,
        "strategy": strategy_block,
    }
    kwargs = merge_flat_over_params(params_dict, flat)

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            "/tasks",
            json=_task_payload_from_kwargs(kwargs, require_settings=True),
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@task.command("update")
@click.argument("identifier")
@click.option("--name", default=None, help="Updated task name.")
@click.option(
    "--mode",
    default=None,
    help="Updated run mode (paper / backtest / live / signal_only).",
)
@click.option("--description", default=None, help="Updated description.")
@click.option("--data-provider", "data_provider", default=None, help="Updated data provider.")
@click.option(
    "--account",
    "account_id",
    default=None,
    help="Rebind this task to account id (acct-...). Omit to leave unchanged.",
)
@click.option(
    "--definition",
    "strategy_definition",
    default=None,
    help="Rebind to this strategy definition (sd-...), with optional --params overrides.",
)
@click.option(
    "--universe",
    default=None,
    help="Replace the universe (comma-separated symbols).",
)
@click.option(
    "--params",
    "params_json",
    default=None,
    help="JSON object merged into the patch (e.g. agent block).",
)
def task_update(
    identifier: str,
    name: str | None,
    mode: str | None,
    description: str | None,
    data_provider: str | None,
    account_id: str | None,
    strategy_definition: str | None,
    universe: str | None,
    params_json: str | None,
) -> None:
    """Update a trading task by task_id or exact name (patch semantics)."""

    params, err = parse_params_json(params_json)
    if err is not None:
        meta_dict = read_session_meta().to_dict()
        if meta_dict:
            err["meta"] = meta_dict
        fmt = click.get_current_context().find_root().obj.get("fmt", "json")
        write_envelope(err, fmt=fmt)
        click.get_current_context().exit(exit_for_invalid_params(err))
        return

    universe_list = split_csv(universe)
    strategy_block = _strategy_block(params or {}, strategy_definition)

    flat: dict[str, Any] = {
        "identifier": identifier,
        "name": name,
        "mode": mode,
        "description": description,
        "data_provider": data_provider,
        "account_id": account_id,
        "universe": universe_list,
        "strategy": strategy_block,
    }
    kwargs = merge_flat_over_params(params, flat)

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(identifier)
        if guard is not None:
            return guard
        return await invoke_api(
            "PUT",
            f"/tasks/{identifier}",
            json=_task_payload_from_kwargs(kwargs, require_settings=False),
            meta=read_session_meta(),
            not_found_error_code="task_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@task.command("start")
@click.argument("identifier")
def task_start(identifier: str) -> None:
    """Start a task by task_id."""

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(identifier)
        if guard is not None:
            return guard
        return await invoke_api(
            "POST",
            f"/tasks/{identifier}/start",
            meta=read_session_meta(),
            not_found_error_code="task_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@task.command("pause")
@click.argument("identifier")
def task_pause(identifier: str) -> None:
    """Pause a task by task_id."""

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(identifier)
        if guard is not None:
            return guard
        return await invoke_api(
            "POST",
            f"/tasks/{identifier}/pause",
            meta=read_session_meta(),
            not_found_error_code="task_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@task.command("stop")
@click.argument("identifier")
def task_stop(identifier: str) -> None:
    """Stop a task by task_id."""

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(identifier)
        if guard is not None:
            return guard
        return await invoke_api(
            "POST",
            f"/tasks/{identifier}/stop",
            meta=read_session_meta(),
            not_found_error_code="task_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@task.command("delete")
@click.argument("identifier")
def task_delete(identifier: str) -> None:
    """Delete a trading task by task_id or exact name."""

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(identifier)
        if guard is not None:
            return guard
        return await invoke_api(
            "DELETE",
            f"/tasks/{identifier}",
            meta=read_session_meta(),
            not_found_error_code="task_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@task.command("clone")
@click.argument("source_identifier")
@click.option("--name", default=None, help="Optional new task name (default: '<source>_copy').")
@click.option("--description", default=None, help="Optional override for the clone's description.")
def task_clone(source_identifier: str, name: str | None, description: str | None) -> None:
    """Clone an existing trading task (useful for one-shot backtest re-runs)."""

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(source_identifier)
        if guard is not None:
            return guard
        envelope, exit_code = await invoke_api(
            "GET",
            f"/tasks/{source_identifier}/duplicate-preset",
            meta=read_session_meta(),
            not_found_error_code="task_not_found",
        )
        if not envelope.get("ok"):
            return envelope, exit_code
        preset = envelope.get("data")
        if not isinstance(preset, dict):
            return envelope, exit_code
        if name is not None:
            preset["name"] = name
        if description is not None:
            preset["description"] = description
        settings: dict[str, Any] = {
            "strategy": preset.get("strategy"),
            "universe": preset.get("universe_symbols") or preset.get("universe") or [],
        }
        enabled_skills = preset.get("enabled_skills")
        if enabled_skills is not None:
            settings["agent"] = {"enabled_skills": enabled_skills}
        payload = {
            "name": preset.get("name"),
            "mode": preset.get("mode"),
            "description": preset.get("description"),
            "data_provider": preset.get("data_provider"),
            "settings": settings,
        }
        return await invoke_api("POST", "/tasks", json=payload, meta=read_session_meta())

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ---------------------------------------------------------------------------
# Triggers (Task-owned schedule + execution intent + delivery)
# ---------------------------------------------------------------------------


@task.group("trigger")
def task_trigger() -> None:
    """Manage a Task's triggers (schedule + execution intent + delivery).

    A trigger fires the task's strategy on a schedule and optionally pushes the
    result. One task may own many triggers (e.g. an intraday trade trigger plus a
    14:50 signal-push trigger), replacing the old signal_only-task + cron-job combo.
    """


def _trigger_payload(
    *,
    name: str | None,
    cron: str | None,
    every: int | None,
    at: str | None,
    timezone: str | None,
    trading_session: str | None,
    intent: str,
    deliver: str,
    target_channel_id: str | None,
    target_chat_id: str | None,
    no_signal_mode: str,
    composer_agent_id: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Build the create/update body, or return (None, error_envelope)."""

    chosen = [k for k, v in (("cron", cron), ("every", every), ("at", at)) if v is not None]
    if len(chosen) != 1:
        err = error_envelope(
            error_code="schedule_required" if not chosen else "schedule_conflict",
            error_type="ValidationError",
            message="exactly one of --cron / --every / --at is required",
            meta=read_session_meta(),
        )
        return None, err
    payload: dict[str, Any] = {"execution_intent": intent}
    if name is not None:
        payload["name"] = name
    if cron is not None:
        payload["schedule_kind"] = "cron"
        payload["cron_expression"] = cron
        if timezone:
            payload["timezone"] = timezone
    elif every is not None:
        payload["schedule_kind"] = "interval"
        payload["interval_seconds"] = every
    else:
        payload["schedule_kind"] = "at"
        payload["at_iso"] = at
    if trading_session:
        payload["trading_session"] = trading_session
    if deliver == "none":
        payload["delivery_json"] = None
    else:
        # A Feishu-group push needs BOTH a bot (--target-channel-id, the registered
        # channel record id) and a group (--target-chat-id, the 'oc_…' chat id).
        # Get the chat id from the UI picker or `assistant feishu chats`. Default
        # (neither set) pushes back to the originating session.
        if target_chat_id:
            target: dict[str, Any] = {
                "kind": "channel",
                "channel_id": target_channel_id or "",
                "chat_id": target_chat_id,
                "channel_type": "feishu",
            }
        else:
            target = {"kind": "session", "origin": True}
        payload["delivery_json"] = {
            "mode": deliver,
            "target": target,
            "no_signal_mode": no_signal_mode,
        }
        # Only a prose push runs a composer Agent; card/none never reads this.
        if deliver == "prose" and composer_agent_id:
            payload["delivery_json"]["composer_agent_id"] = composer_agent_id
    return payload, None


_SCHEDULE_OPTS = [
    click.option("--cron", "cron", default=None, help="5-field cron expression, e.g. '50 14 * * mon-fri'."),
    click.option("--every", "every", type=int, default=None, help="Interval seconds (e.g. 300)."),
    click.option("--at", "at", default=None, help="One-shot ISO-8601 instant w/ offset, e.g. 2026-06-12T09:25:00+08:00."),
    click.option("--timezone", "timezone", default=None, help="Timezone for --cron (e.g. Asia/Shanghai)."),
    click.option("--trading-session", "trading_session", default=None, help="Trading-session gate, e.g. ashare."),
    click.option("--intent", "intent", type=click.Choice(["signal_only", "trade"]), default="signal_only", help="Execution intent for the fire."),
    click.option("--deliver", "deliver", type=click.Choice(["none", "card", "prose"]), default="card", help="Push format (none = run silently)."),
    click.option("--target-channel-id", "target_channel_id", default=None, help="Registered Feishu channel record id (the bot) for a channel push."),
    click.option("--target-chat-id", "target_chat_id", default=None, help="Feishu group chat id 'oc_…' to push to (with --target-channel-id). Default: the current session."),
    click.option("--no-signal-mode", "no_signal_mode", type=click.Choice(["silent", "brief", "full"]), default="brief", help="Behaviour when a fire produces no signal."),
    click.option("--composer-agent-id", "composer_agent_id", default=None, help="Agent that composes the prose push (prose mode only; default: first active agent)."),
]


def _with_schedule_opts(fn):
    for opt in reversed(_SCHEDULE_OPTS):
        fn = opt(fn)
    return fn


@task_trigger.command("add")
@click.argument("task_identifier")
@click.option("--name", default=None, help="Human-readable trigger name.")
@_with_schedule_opts
def task_trigger_add(task_identifier: str, name: str | None, cron, every, at, timezone, trading_session, intent, deliver, target_channel_id, target_chat_id, no_signal_mode, composer_agent_id) -> None:
    """Add a trigger to a task."""

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(task_identifier)
        if guard is not None:
            return guard
        payload, err = _trigger_payload(
            name=name, cron=cron, every=every, at=at, timezone=timezone,
            trading_session=trading_session, intent=intent, deliver=deliver,
            target_channel_id=target_channel_id, target_chat_id=target_chat_id,
            no_signal_mode=no_signal_mode, composer_agent_id=composer_agent_id,
        )
        if err is not None:
            return err, exit_code_for_error(str(err.get("error", {}).get("error_code", "validation_error")))
        return await invoke_api(
            "POST", f"/tasks/{task_identifier}/triggers", json=payload,
            meta=read_session_meta(), not_found_error_code="task_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


@task_trigger.command("list")
@click.argument("task_identifier")
def task_trigger_list(task_identifier: str) -> None:
    """List a task's triggers."""

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(task_identifier)
        if guard is not None:
            return guard
        return await invoke_api(
            "GET", f"/tasks/{task_identifier}/triggers",
            meta=read_session_meta(), not_found_error_code="task_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


@task_trigger.command("get")
@click.argument("task_identifier")
@click.argument("trigger_id")
def task_trigger_get(task_identifier: str, trigger_id: str) -> None:
    """Get one trigger."""

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(task_identifier)
        if guard is not None:
            return guard
        return await invoke_api(
            "GET", f"/tasks/{task_identifier}/triggers/{trigger_id}",
            meta=read_session_meta(), not_found_error_code="trigger_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


@task_trigger.command("update")
@click.argument("task_identifier")
@click.argument("trigger_id")
@click.option("--name", default=None, help="New name.")
@_with_schedule_opts
def task_trigger_update(task_identifier: str, trigger_id: str, name, cron, every, at, timezone, trading_session, intent, deliver, target_channel_id, target_chat_id, no_signal_mode, composer_agent_id) -> None:
    """Update a trigger (re-specify the full schedule + intent + delivery)."""

    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(task_identifier)
        if guard is not None:
            return guard
        payload, err = _trigger_payload(
            name=name, cron=cron, every=every, at=at, timezone=timezone,
            trading_session=trading_session, intent=intent, deliver=deliver,
            target_channel_id=target_channel_id, target_chat_id=target_chat_id,
            no_signal_mode=no_signal_mode, composer_agent_id=composer_agent_id,
        )
        if err is not None:
            return err, exit_code_for_error(str(err.get("error", {}).get("error_code", "validation_error")))
        return await invoke_api(
            "PUT", f"/tasks/{task_identifier}/triggers/{trigger_id}", json=payload,
            meta=read_session_meta(), not_found_error_code="trigger_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


def _trigger_action(task_identifier: str, trigger_id: str, action: str, method: str = "POST"):
    async def _run() -> tuple[dict[str, Any], int]:
        guard = _task_identifier_guard(task_identifier)
        if guard is not None:
            return guard
        suffix = f"/{action}" if action else ""
        return await invoke_api(
            method, f"/tasks/{task_identifier}/triggers/{trigger_id}{suffix}",
            meta=read_session_meta(), not_found_error_code="trigger_not_found",
        )

    return _run


@task_trigger.command("pause")
@click.argument("task_identifier")
@click.argument("trigger_id")
def task_trigger_pause(task_identifier: str, trigger_id: str) -> None:
    """Pause a trigger (stops firing until resumed)."""
    click.get_current_context().exit(run_async_command(_trigger_action(task_identifier, trigger_id, "pause")))


@task_trigger.command("resume")
@click.argument("task_identifier")
@click.argument("trigger_id")
def task_trigger_resume(task_identifier: str, trigger_id: str) -> None:
    """Resume a paused trigger."""
    click.get_current_context().exit(run_async_command(_trigger_action(task_identifier, trigger_id, "resume")))


@task_trigger.command("run")
@click.argument("task_identifier")
@click.argument("trigger_id")
def task_trigger_run(task_identifier: str, trigger_id: str) -> None:
    """Fire a trigger once now (out-of-band; ungated by trading session)."""
    click.get_current_context().exit(run_async_command(_trigger_action(task_identifier, trigger_id, "run")))


@task_trigger.command("delete")
@click.argument("task_identifier")
@click.argument("trigger_id")
def task_trigger_delete(task_identifier: str, trigger_id: str) -> None:
    """Delete a trigger."""
    click.get_current_context().exit(run_async_command(_trigger_action(task_identifier, trigger_id, "", method="DELETE")))


__all__ = ["task"]
