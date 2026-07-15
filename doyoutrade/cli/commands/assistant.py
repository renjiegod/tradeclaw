"""`doyoutrade-cli assistant ...` commands for chat validation.

The API server owns assistant runtime state. This module is only a thin
HTTP/envelope adapter for programming agents that need to validate a real
assistant conversation without opening the browser.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._envelope import EXIT_FAILURE, EXIT_VALIDATION, error_envelope
from doyoutrade.cli._format import FORMAT_JSON, write_envelope
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command

_DIAGNOSTIC_EXPORT_WAIT_SECONDS = 5.0
_DIAGNOSTIC_EXPORT_POLL_SECONDS = 0.25


@click.group()
def assistant() -> None:
    """Assistant chat validation commands."""


@assistant.group("agent")
def assistant_agent() -> None:
    """Assistant agent management commands."""


@assistant.group("session")
def assistant_session() -> None:
    """Assistant session commands."""


@assistant.group("feishu")
def assistant_feishu() -> None:
    """Feishu helpers (for trigger delivery setup)."""


@assistant_feishu.command("chats")
def assistant_feishu_chats() -> None:
    """List the Feishu groups each running bot belongs to.

    Each row = (channel_id [the bot], chat_id ['oc_…' group id], name). Use the
    chat_id with `task trigger add --target-channel-id <channel_id>
    --target-chat-id <chat_id>` to push a trigger to a fixed group.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            "/assistant/feishu/chats",
            meta=read_session_meta(),
        )

    click.get_current_context().exit(run_async_command(_run))


def _message_from_options(message: str | None, message_file: str | None) -> tuple[str | None, dict[str, Any] | None]:
    if message is not None and message_file is not None:
        return None, error_envelope(
            error_code="conflicting_message_args",
            message="Use exactly one of --message or --message-file.",
            meta=read_session_meta(),
        )
    if message_file is not None:
        path = Path(message_file)
        try:
            return path.read_text(encoding="utf-8"), None
        except (OSError, UnicodeError) as exc:
            return None, error_envelope(
                error_code="message_file_read_failed",
                error_type=type(exc).__name__,
                message=f"Could not read --message-file {message_file}: {exc}",
                meta=read_session_meta(),
            )
    if message is not None:
        return message, None
    return None, error_envelope(
        error_code="missing_message",
        message="Pass --message <text> or --message-file <path>.",
        meta=read_session_meta(),
    )


def _emit_local_error(envelope: dict[str, Any], exit_code: int = EXIT_VALIDATION) -> None:
    root = click.get_current_context().find_root()
    fmt = (root.obj or {}).get("fmt", FORMAT_JSON)
    write_envelope(envelope, fmt=fmt)
    click.get_current_context().exit(exit_code)


def _read_text_option(
    *,
    inline: str | None,
    file_path: str | None,
    option_label: str,
    file_option_label: str,
    conflicting_error_code: str,
    read_failed_error_code: str,
) -> tuple[str | None, dict[str, Any] | None]:
    if inline is not None and file_path is not None:
        return None, error_envelope(
            error_code=conflicting_error_code,
            message=f"Use exactly one of {option_label} or {file_option_label}.",
            meta=read_session_meta(),
        )
    if file_path is not None:
        path = Path(file_path)
        try:
            return path.read_text(encoding="utf-8"), None
        except (OSError, UnicodeError) as exc:
            return None, error_envelope(
                error_code=read_failed_error_code,
                error_type=type(exc).__name__,
                message=f"Could not read {file_option_label} {file_path}: {exc}",
                meta=read_session_meta(),
            )
    return inline, None


def _parse_json_option(
    raw: str | None,
    *,
    option_label: str,
    error_code: str,
    expected_type: type | tuple[type, ...],
    expected_label: str,
) -> tuple[Any | None, dict[str, Any] | None]:
    if raw is None:
        return None, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, error_envelope(
            error_code=error_code,
            error_type=type(exc).__name__,
            message=f"{option_label} must be valid JSON: {exc}",
            meta=read_session_meta(),
        )
    if not isinstance(parsed, expected_type):
        return None, error_envelope(
            error_code=error_code,
            error_type="validation_error",
            message=f"{option_label} must decode to {expected_label}.",
            meta=read_session_meta(),
        )
    return parsed, None


def _dedupe_preserve_order(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _parse_tool_config_entries(
    entries: tuple[str, ...],
    *,
    option_label: str = "--tool-config",
    error_code: str = "invalid_tool_config",
) -> tuple[list[dict[str, str]] | None, dict[str, Any] | None]:
    if not entries:
        return None, None
    ordered_names: list[str] = []
    configs_by_name: dict[str, dict[str, str]] = {}
    for raw_entry in entries:
        entry = str(raw_entry or "").strip()
        if not entry:
            continue
        if "=" in entry:
            name_part, mode_part = entry.rsplit("=", 1)
        else:
            name_part, mode_part = entry, "base"
        name = str(name_part or "").strip()
        load_mode = str(mode_part or "").strip().lower() or "base"
        if not name:
            return None, error_envelope(
                error_code=error_code,
                error_type="validation_error",
                message=f"{option_label} requires NAME or NAME=LOAD_MODE.",
                meta=read_session_meta(),
            )
        if load_mode not in {"base", "deferred"}:
            return None, error_envelope(
                error_code=error_code,
                error_type="validation_error",
                message=f"{option_label} load_mode must be one of: base, deferred.",
                meta=read_session_meta(),
            )
        if name not in configs_by_name:
            ordered_names.append(name)
        configs_by_name[name] = {"name": name, "load_mode": load_mode}
    return [configs_by_name[name] for name in ordered_names], None


def _agent_mutation_conflict(
    *,
    field_name: str,
    reset_requested: bool,
    replace_values: tuple[str, ...],
    add_values: tuple[str, ...],
    remove_values: tuple[str, ...],
) -> dict[str, Any] | None:
    singular = field_name[:-1] if field_name.endswith("s") else field_name
    if replace_values and (reset_requested or add_values or remove_values):
        return error_envelope(
            error_code=f"conflicting_{singular}_args",
            message=(
                f"Use either --{singular} for full replacement or the "
                f"incremental --add-{singular} / --remove-{singular} "
                f"flags (optionally with --clear-{field_name})."
            ),
            meta=read_session_meta(),
        )
    if reset_requested and add_values and remove_values:
        return None
    return None


def _context_compaction_overrides(
    *,
    base: dict[str, Any] | None,
    enabled: bool | None = None,
    mode: str | None = None,
    auto_threshold_tokens: int | None = None,
    warning_threshold_tokens: int | None = None,
    preserve_recent_messages: int | None = None,
    preserve_recent_tool_pairs: int | None = None,
    micro_enabled: bool | None = None,
    tool_result_max_chars: int | None = None,
    full_enabled: bool | None = None,
    summary_model_route_name: str | None = None,
    clear_summary_model_route: bool = False,
    allow_slash_compact: bool | None = None,
) -> dict[str, Any] | None:
    payload = dict(base or {})
    if enabled is not None:
        payload["enabled"] = enabled
    if mode is not None:
        payload["mode"] = str(mode).strip()
    if auto_threshold_tokens is not None:
        payload["auto_threshold_tokens"] = auto_threshold_tokens
    if warning_threshold_tokens is not None:
        payload["warning_threshold_tokens"] = warning_threshold_tokens
    if preserve_recent_messages is not None:
        payload["preserve_recent_messages"] = preserve_recent_messages
    if preserve_recent_tool_pairs is not None:
        payload["preserve_recent_tool_pairs"] = preserve_recent_tool_pairs
    if micro_enabled is not None:
        payload["micro_compaction_enabled"] = micro_enabled
    if tool_result_max_chars is not None:
        payload["tool_result_max_chars"] = tool_result_max_chars
    if full_enabled is not None:
        payload["full_compaction_enabled"] = full_enabled
    if clear_summary_model_route:
        payload["summary_model_route_name"] = ""
    elif summary_model_route_name is not None:
        payload["summary_model_route_name"] = str(summary_model_route_name).strip()
    if allow_slash_compact is not None:
        payload["allow_slash_compact"] = allow_slash_compact
    return payload or None


async def _resolve_updated_name_list(
    *,
    agent_id: str,
    current_field: str,
    replace_values: tuple[str, ...],
    add_values: tuple[str, ...],
    remove_values: tuple[str, ...],
    clear_existing: bool,
    meta: dict[str, Any],
) -> tuple[list[str] | None, dict[str, Any] | None, int | None]:
    if replace_values:
        return _dedupe_preserve_order(replace_values), None, None

    if not clear_existing and not add_values and not remove_values:
        return None, None, None

    base_values: list[str] = []
    if not clear_existing:
        envelope, code = await invoke_api(
            "GET",
            f"/assistant/agents/{agent_id}",
            meta=meta,
            not_found_error_code="agent_not_found",
        )
        if not envelope.get("ok"):
            return None, envelope, code
        base_values = _dedupe_preserve_order(tuple((envelope.get("data") or {}).get(current_field) or ()))

    remove_set = {item for item in _dedupe_preserve_order(remove_values)}
    next_values = [item for item in base_values if item not in remove_set]
    existing = set(next_values)
    for item in _dedupe_preserve_order(add_values):
        if item in remove_set or item in existing:
            continue
        next_values.append(item)
        existing.add(item)
    return next_values, None, None


def _build_agent_payload(
    *,
    name: str | None = None,
    status: str | None = None,
    system_prompt: str | None = None,
    prompt_template_id: str | None = None,
    clear_prompt_template: bool = False,
    model_route_name: str | None = None,
    clear_model_route: bool = False,
    tool_names: list[str] | None = None,
    tool_configs: list[dict[str, Any]] | None = None,
    skill_names: list[str] | None = None,
    max_turns: int | None = None,
    context_compaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if name is not None:
        payload["name"] = name
    if status is not None:
        payload["status"] = status
    if system_prompt is not None:
        payload["system_prompt"] = system_prompt
    if clear_prompt_template:
        payload["prompt_template_id"] = ""
    elif prompt_template_id is not None:
        payload["prompt_template_id"] = prompt_template_id
    if clear_model_route:
        payload["model_route_name"] = ""
    elif model_route_name is not None:
        payload["model_route_name"] = model_route_name
    if tool_names is not None:
        payload["tool_names"] = tool_names
    if tool_configs is not None:
        payload["tool_configs"] = tool_configs
    if skill_names is not None:
        payload["skill_names"] = skill_names
    if max_turns is not None:
        payload["max_turns"] = max_turns
    if context_compaction is not None:
        payload["context_compaction"] = context_compaction
    return payload


def _export_summary(data: dict[str, Any] | None, output: str | None = None) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return data
    summary = {
        "session_id": (data.get("ids") or {}).get("session_id"),
        "format": data.get("format") or "markdown",
        "counts": data.get("counts") or {},
        "ids": data.get("ids") or {},
        "warnings": data.get("warnings") or [],
    }
    if output is not None:
        summary["export_path"] = output
    return summary


def _write_export_if_requested(
    data: dict[str, Any] | None,
    output: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if output is None or not isinstance(data, dict):
        return data, None
    text = data.get("export_text")
    if not isinstance(text, str):
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        Path(output).write_text(text, encoding="utf-8")
    except OSError as exc:
        return None, error_envelope(
            error_code="export_write_failed",
            error_type=type(exc).__name__,
            message=f"Could not write assistant export to {output}: {exc}",
            extra={"diagnostic_export": _export_summary(data), "export_path": output},
            meta=read_session_meta(),
        )
    return _export_summary(data, output), None


def _export_has_chat_diagnostics(data: dict[str, Any] | None, chat_trace_id: str | None) -> bool:
    if not chat_trace_id or not isinstance(data, dict):
        return True
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    ids = data.get("ids") if isinstance(data.get("ids"), dict) else {}
    trace_ids = [str(item) for item in ids.get("trace_ids") or []]
    return (
        str(chat_trace_id) in trace_ids
        and int(counts.get("spans") or 0) > 0
        and int(counts.get("model_invocations") or 0) > 0
    )


async def _invoke_export(
    *,
    session_id: str,
    export_format: str,
    include_traces: bool,
    meta: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    return await invoke_api(
        "GET",
        f"/assistant/sessions/{session_id}/export",
        params={"format": export_format, "include_traces": include_traces},
        meta=meta,
        not_found_error_code="assistant_session_not_found",
        timeout_seconds=60.0,
    )


async def _invoke_export_after_chat_diagnostics(
    *,
    session_id: str,
    export_format: str,
    include_traces: bool,
    meta: dict[str, Any],
    chat_trace_id: str | None,
) -> tuple[dict[str, Any], int]:
    deadline = asyncio.get_running_loop().time() + _DIAGNOSTIC_EXPORT_WAIT_SECONDS
    while True:
        envelope, code = await _invoke_export(
            session_id=session_id,
            export_format=export_format,
            include_traces=include_traces,
            meta=meta,
        )
        if not envelope.get("ok") or not include_traces:
            return envelope, code
        if _export_has_chat_diagnostics(envelope.get("data"), chat_trace_id):
            return envelope, code
        if asyncio.get_running_loop().time() >= deadline:
            return envelope, code
        await asyncio.sleep(_DIAGNOSTIC_EXPORT_POLL_SECONDS)


@assistant_agent.command("list")
@click.option("--include-inactive", is_flag=True, help="Include inactive agents.")
def assistant_agent_list(include_inactive: bool) -> None:
    """List assistant agents from the running API server."""

    async def _run() -> tuple[dict[str, Any], int]:
        params = {"include_inactive": True} if include_inactive else None
        return await invoke_api(
            "GET",
            "/assistant/agents",
            params=params,
            meta=read_session_meta(),
        )

    click.get_current_context().exit(run_async_command(_run))


@assistant_agent.command("get")
@click.argument("agent_id")
def assistant_agent_get(agent_id: str) -> None:
    """Get one assistant agent by id."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/assistant/agents/{agent_id}",
            meta=read_session_meta(),
            not_found_error_code="agent_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


@assistant_agent.command("create")
@click.option("--name", required=True, help="Agent display name.")
@click.option("--status", type=click.Choice(["active", "inactive"]), default=None, help="Initial agent status.")
@click.option("--system-prompt", default=None, help="Raw system prompt text.")
@click.option(
    "--system-prompt-file",
    "system_prompt_file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Read the system prompt from a UTF-8 text file.",
)
@click.option("--prompt-template-id", default=None, help="Built-in prompt template id, e.g. main-agent.")
@click.option("--model-route", "model_route_name", default=None, help="Model route name.")
@click.option("--tool", "tool_names", multiple=True, help="Allowed tool name. Repeatable.")
@click.option(
    "--tool-config",
    "tool_config_entries",
    multiple=True,
    help="Repeat NAME or NAME=LOAD_MODE (base|deferred) to set tool bindings with load mode.",
)
@click.option(
    "--tool-configs-json",
    default=None,
    help="Raw JSON array for tool_configs. Cannot be combined with --tool or --tool-config.",
)
@click.option("--skill", "skill_names", multiple=True, help="Allowed skill name. Repeatable.")
@click.option("--max-turns", type=int, default=None, help="Maximum turns before the agent stops.")
@click.option(
    "--context-compaction-json",
    default=None,
    help="Raw JSON object for context_compaction overrides.",
)
@click.option("--compaction-enabled/--compaction-disabled", default=None, help="Enable or disable context compaction.")
@click.option("--compaction-mode", default=None, help="Context compaction mode, e.g. auto or manual.")
@click.option("--compaction-auto-threshold-tokens", type=int, default=None, help="Context compaction auto threshold.")
@click.option("--compaction-warning-threshold-tokens", type=int, default=None, help="Context compaction warning threshold.")
@click.option("--compaction-preserve-recent-messages", type=int, default=None, help="Context compaction preserve_recent_messages.")
@click.option("--compaction-preserve-recent-tool-pairs", type=int, default=None, help="Context compaction preserve_recent_tool_pairs.")
@click.option("--compaction-micro-enabled/--compaction-micro-disabled", default=None, help="Enable or disable micro compaction.")
@click.option("--compaction-tool-result-max-chars", type=int, default=None, help="Context compaction tool_result_max_chars.")
@click.option("--compaction-full-enabled/--compaction-full-disabled", default=None, help="Enable or disable full compaction.")
@click.option("--compaction-summary-model-route", default=None, help="Context compaction summary_model_route_name.")
@click.option("--compaction-allow-slash-compact/--compaction-disallow-slash-compact", default=None, help="Enable or disable /compact.")
def assistant_agent_create(
    name: str,
    status: str | None,
    system_prompt: str | None,
    system_prompt_file: str | None,
    prompt_template_id: str | None,
    model_route_name: str | None,
    tool_names: tuple[str, ...],
    tool_config_entries: tuple[str, ...],
    tool_configs_json: str | None,
    skill_names: tuple[str, ...],
    max_turns: int | None,
    context_compaction_json: str | None,
    compaction_enabled: bool | None,
    compaction_mode: str | None,
    compaction_auto_threshold_tokens: int | None,
    compaction_warning_threshold_tokens: int | None,
    compaction_preserve_recent_messages: int | None,
    compaction_preserve_recent_tool_pairs: int | None,
    compaction_micro_enabled: bool | None,
    compaction_tool_result_max_chars: int | None,
    compaction_full_enabled: bool | None,
    compaction_summary_model_route: str | None,
    compaction_allow_slash_compact: bool | None,
) -> None:
    """Create an assistant agent."""

    prompt_text, err = _read_text_option(
        inline=system_prompt,
        file_path=system_prompt_file,
        option_label="--system-prompt",
        file_option_label="--system-prompt-file",
        conflicting_error_code="conflicting_system_prompt_args",
        read_failed_error_code="system_prompt_file_read_failed",
    )
    if err is not None:
        _emit_local_error(err)
    if prompt_text is None and not str(prompt_template_id or "").strip():
        _emit_local_error(
            error_envelope(
                error_code="missing_system_prompt",
                message="Pass --system-prompt/--system-prompt-file or --prompt-template-id.",
                meta=read_session_meta(),
            )
        )
    if (tool_names or tool_config_entries) and tool_configs_json is not None:
        _emit_local_error(
            error_envelope(
                error_code="conflicting_tool_args",
                message="Use either --tool/--tool-config or --tool-configs-json, not both.",
                meta=read_session_meta(),
            )
        )
    if tool_names and tool_config_entries:
        _emit_local_error(
            error_envelope(
                error_code="conflicting_tool_args",
                message="Use either --tool or --tool-config, not both.",
                meta=read_session_meta(),
            )
        )

    tool_configs, err = _parse_tool_config_entries(tool_config_entries)
    if err is not None:
        _emit_local_error(err)
    tool_configs_json_value, err = _parse_json_option(
        tool_configs_json,
        option_label="--tool-configs-json",
        error_code="invalid_tool_configs_json",
        expected_type=list,
        expected_label="a JSON array",
    )
    if err is not None:
        _emit_local_error(err)
    if tool_configs is None:
        tool_configs = tool_configs_json_value
    context_compaction_base, err = _parse_json_option(
        context_compaction_json,
        option_label="--context-compaction-json",
        error_code="invalid_context_compaction_json",
        expected_type=dict,
        expected_label="a JSON object",
    )
    if err is not None:
        _emit_local_error(err)
    context_compaction = _context_compaction_overrides(
        base=context_compaction_base,
        enabled=compaction_enabled,
        mode=compaction_mode,
        auto_threshold_tokens=compaction_auto_threshold_tokens,
        warning_threshold_tokens=compaction_warning_threshold_tokens,
        preserve_recent_messages=compaction_preserve_recent_messages,
        preserve_recent_tool_pairs=compaction_preserve_recent_tool_pairs,
        micro_enabled=compaction_micro_enabled,
        tool_result_max_chars=compaction_tool_result_max_chars,
        full_enabled=compaction_full_enabled,
        summary_model_route_name=compaction_summary_model_route,
        allow_slash_compact=compaction_allow_slash_compact,
    )

    async def _run() -> tuple[dict[str, Any], int]:
        payload = _build_agent_payload(
            name=name,
            status=status,
            system_prompt=prompt_text,
            prompt_template_id=str(prompt_template_id or "").strip() or None,
            model_route_name=str(model_route_name or "").strip() or None,
            tool_names=_dedupe_preserve_order(tool_names) or None,
            tool_configs=tool_configs,
            skill_names=_dedupe_preserve_order(skill_names) or None,
            max_turns=max_turns,
            context_compaction=context_compaction,
        )
        return await invoke_api(
            "POST",
            "/assistant/agents",
            json=payload,
            meta=read_session_meta(),
        )

    click.get_current_context().exit(run_async_command(_run))


@assistant_agent.command("update")
@click.argument("agent_id")
@click.option("--name", default=None, help="New agent display name.")
@click.option("--status", type=click.Choice(["active", "inactive"]), default=None, help="Updated agent status.")
@click.option("--system-prompt", default=None, help="Raw system prompt text.")
@click.option(
    "--system-prompt-file",
    "system_prompt_file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Read the system prompt from a UTF-8 text file.",
)
@click.option("--prompt-template-id", default=None, help="Set built-in prompt template id.")
@click.option("--clear-prompt-template", is_flag=True, help="Clear the current prompt template binding.")
@click.option("--model-route", "model_route_name", default=None, help="Model route name.")
@click.option("--clear-model-route", is_flag=True, help="Clear the current model route binding.")
@click.option("--tool", "tool_names", multiple=True, help="Replace tools with this set. Repeatable.")
@click.option("--clear-tools", is_flag=True, help="Clear all tool bindings before applying incremental tool options.")
@click.option("--add-tool", "add_tool_names", multiple=True, help="Add one tool binding. Repeatable.")
@click.option("--remove-tool", "remove_tool_names", multiple=True, help="Remove one tool binding. Repeatable.")
@click.option(
    "--tool-config",
    "tool_config_entries",
    multiple=True,
    help="Repeat NAME or NAME=LOAD_MODE (base|deferred) to replace tool configs with load modes.",
)
@click.option(
    "--tool-configs-json",
    default=None,
    help="Raw JSON array for tool_configs. Cannot be combined with tool mutation flags.",
)
@click.option("--skill", "skill_names", multiple=True, help="Replace skills with this set. Repeatable.")
@click.option("--clear-skills", is_flag=True, help="Clear all skill bindings before applying incremental skill options.")
@click.option("--add-skill", "add_skill_names", multiple=True, help="Add one skill binding. Repeatable.")
@click.option("--remove-skill", "remove_skill_names", multiple=True, help="Remove one skill binding. Repeatable.")
@click.option("--max-turns", type=int, default=None, help="Maximum turns before the agent stops.")
@click.option(
    "--context-compaction-json",
    default=None,
    help="Raw JSON object for context_compaction overrides.",
)
@click.option("--compaction-enabled/--compaction-disabled", default=None, help="Enable or disable context compaction.")
@click.option("--compaction-mode", default=None, help="Context compaction mode, e.g. auto or manual.")
@click.option("--compaction-auto-threshold-tokens", type=int, default=None, help="Context compaction auto threshold.")
@click.option("--compaction-warning-threshold-tokens", type=int, default=None, help="Context compaction warning threshold.")
@click.option("--compaction-preserve-recent-messages", type=int, default=None, help="Context compaction preserve_recent_messages.")
@click.option("--compaction-preserve-recent-tool-pairs", type=int, default=None, help="Context compaction preserve_recent_tool_pairs.")
@click.option("--compaction-micro-enabled/--compaction-micro-disabled", default=None, help="Enable or disable micro compaction.")
@click.option("--compaction-tool-result-max-chars", type=int, default=None, help="Context compaction tool_result_max_chars.")
@click.option("--compaction-full-enabled/--compaction-full-disabled", default=None, help="Enable or disable full compaction.")
@click.option("--compaction-summary-model-route", default=None, help="Context compaction summary_model_route_name.")
@click.option("--clear-compaction-summary-model-route", is_flag=True, help="Clear context compaction summary_model_route_name.")
@click.option("--compaction-allow-slash-compact/--compaction-disallow-slash-compact", default=None, help="Enable or disable /compact.")
def assistant_agent_update(
    agent_id: str,
    name: str | None,
    status: str | None,
    system_prompt: str | None,
    system_prompt_file: str | None,
    prompt_template_id: str | None,
    clear_prompt_template: bool,
    model_route_name: str | None,
    clear_model_route: bool,
    tool_names: tuple[str, ...],
    clear_tools: bool,
    add_tool_names: tuple[str, ...],
    remove_tool_names: tuple[str, ...],
    tool_config_entries: tuple[str, ...],
    tool_configs_json: str | None,
    skill_names: tuple[str, ...],
    clear_skills: bool,
    add_skill_names: tuple[str, ...],
    remove_skill_names: tuple[str, ...],
    max_turns: int | None,
    context_compaction_json: str | None,
    compaction_enabled: bool | None,
    compaction_mode: str | None,
    compaction_auto_threshold_tokens: int | None,
    compaction_warning_threshold_tokens: int | None,
    compaction_preserve_recent_messages: int | None,
    compaction_preserve_recent_tool_pairs: int | None,
    compaction_micro_enabled: bool | None,
    compaction_tool_result_max_chars: int | None,
    compaction_full_enabled: bool | None,
    compaction_summary_model_route: str | None,
    clear_compaction_summary_model_route: bool,
    compaction_allow_slash_compact: bool | None,
) -> None:
    """Update an assistant agent."""

    prompt_text, err = _read_text_option(
        inline=system_prompt,
        file_path=system_prompt_file,
        option_label="--system-prompt",
        file_option_label="--system-prompt-file",
        conflicting_error_code="conflicting_system_prompt_args",
        read_failed_error_code="system_prompt_file_read_failed",
    )
    if err is not None:
        _emit_local_error(err)

    tool_conflict = _agent_mutation_conflict(
        field_name="tools",
        reset_requested=clear_tools,
        replace_values=tool_names,
        add_values=add_tool_names,
        remove_values=remove_tool_names,
    )
    if tool_conflict is not None:
        _emit_local_error(tool_conflict)
    if tool_config_entries and (tool_names or clear_tools or add_tool_names or remove_tool_names):
        _emit_local_error(
            error_envelope(
                error_code="conflicting_tool_args",
                message=(
                    "Use either --tool-config or the tool mutation flags "
                    "(--tool / --clear-tools / --add-tool / --remove-tool)."
                ),
                meta=read_session_meta(),
            )
        )
    skill_conflict = _agent_mutation_conflict(
        field_name="skills",
        reset_requested=clear_skills,
        replace_values=skill_names,
        add_values=add_skill_names,
        remove_values=remove_skill_names,
    )
    if skill_conflict is not None:
        _emit_local_error(skill_conflict)
    if tool_configs_json is not None and (tool_names or clear_tools or add_tool_names or remove_tool_names or tool_config_entries):
        _emit_local_error(
            error_envelope(
                error_code="conflicting_tool_args",
                message=(
                    "Use either --tool-configs-json or the tool mutation flags "
                    "(--tool / --clear-tools / --add-tool / --remove-tool / --tool-config)."
                ),
                meta=read_session_meta(),
            )
        )
    if clear_prompt_template and prompt_template_id is not None:
        _emit_local_error(
            error_envelope(
                error_code="conflicting_prompt_template_args",
                message="Use either --prompt-template-id or --clear-prompt-template.",
                meta=read_session_meta(),
            )
        )
    if clear_model_route and model_route_name is not None:
        _emit_local_error(
            error_envelope(
                error_code="conflicting_model_route_args",
                message="Use either --model-route or --clear-model-route.",
                meta=read_session_meta(),
            )
        )
    if clear_compaction_summary_model_route and compaction_summary_model_route is not None:
        _emit_local_error(
            error_envelope(
                error_code="conflicting_compaction_summary_model_route_args",
                message=(
                    "Use either --compaction-summary-model-route or "
                    "--clear-compaction-summary-model-route."
                ),
                meta=read_session_meta(),
            )
        )

    tool_configs, err = _parse_tool_config_entries(tool_config_entries)
    if err is not None:
        _emit_local_error(err)
    tool_configs_json_value, err = _parse_json_option(
        tool_configs_json,
        option_label="--tool-configs-json",
        error_code="invalid_tool_configs_json",
        expected_type=list,
        expected_label="a JSON array",
    )
    if err is not None:
        _emit_local_error(err)
    if tool_configs is None:
        tool_configs = tool_configs_json_value
    context_compaction_base, err = _parse_json_option(
        context_compaction_json,
        option_label="--context-compaction-json",
        error_code="invalid_context_compaction_json",
        expected_type=dict,
        expected_label="a JSON object",
    )
    if err is not None:
        _emit_local_error(err)
    context_compaction = _context_compaction_overrides(
        base=context_compaction_base,
        enabled=compaction_enabled,
        mode=compaction_mode,
        auto_threshold_tokens=compaction_auto_threshold_tokens,
        warning_threshold_tokens=compaction_warning_threshold_tokens,
        preserve_recent_messages=compaction_preserve_recent_messages,
        preserve_recent_tool_pairs=compaction_preserve_recent_tool_pairs,
        micro_enabled=compaction_micro_enabled,
        tool_result_max_chars=compaction_tool_result_max_chars,
        full_enabled=compaction_full_enabled,
        summary_model_route_name=compaction_summary_model_route,
        clear_summary_model_route=clear_compaction_summary_model_route,
        allow_slash_compact=compaction_allow_slash_compact,
    )

    async def _run() -> tuple[dict[str, Any], int]:
        meta = read_session_meta()
        resolved_tool_names, err_env, err_code = await _resolve_updated_name_list(
            agent_id=agent_id,
            current_field="tool_names",
            replace_values=tool_names,
            add_values=add_tool_names,
            remove_values=remove_tool_names,
            clear_existing=clear_tools,
            meta=meta,
        )
        if err_env is not None:
            return err_env, int(err_code or EXIT_VALIDATION)
        resolved_skill_names, err_env, err_code = await _resolve_updated_name_list(
            agent_id=agent_id,
            current_field="skill_names",
            replace_values=skill_names,
            add_values=add_skill_names,
            remove_values=remove_skill_names,
            clear_existing=clear_skills,
            meta=meta,
        )
        if err_env is not None:
            return err_env, int(err_code or EXIT_VALIDATION)

        payload = _build_agent_payload(
            name=str(name or "").strip() or None,
            status=status,
            system_prompt=prompt_text,
            prompt_template_id=str(prompt_template_id or "").strip() or None,
            clear_prompt_template=clear_prompt_template,
            model_route_name=str(model_route_name or "").strip() or None,
            clear_model_route=clear_model_route,
            tool_names=resolved_tool_names,
            tool_configs=tool_configs,
            skill_names=resolved_skill_names,
            max_turns=max_turns,
            context_compaction=context_compaction,
        )
        if not payload:
            return error_envelope(
                error_code="missing_update_fields",
                message="Pass at least one field to update.",
                meta=meta,
            ), EXIT_VALIDATION
        return await invoke_api(
            "PUT",
            f"/assistant/agents/{agent_id}",
            json=payload,
            meta=meta,
            not_found_error_code="agent_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


@assistant_agent.command("clone")
@click.argument("agent_id")
@click.option("--name", required=True, help="Name for the cloned agent.")
def assistant_agent_clone(agent_id: str, name: str) -> None:
    """Clone an assistant agent."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            f"/assistant/agents/{agent_id}/clone",
            json={"name": name},
            meta=read_session_meta(),
            not_found_error_code="agent_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


@assistant_agent.command("delete")
@click.argument("agent_id")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Also delete the agent's assistant sessions (and their messages/events). "
    "Without this, deleting an agent that still has sessions is rejected.",
)
def assistant_agent_delete(agent_id: str, force: bool) -> None:
    """Delete an assistant agent."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "DELETE",
            f"/assistant/agents/{agent_id}",
            params={"force": "true"} if force else None,
            meta=read_session_meta(),
            not_found_error_code="agent_not_found",
        )

    click.get_current_context().exit(run_async_command(_run))


@assistant_session.command("create")
@click.option("--agent-id", "agent_id", required=True, help="Assistant agent id, e.g. agent_default or agent-...")
@click.option("--title", default="", help="Optional session title.")
def assistant_session_create(agent_id: str, title: str) -> None:
    """Create an assistant session via the running API server."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            "/assistant/sessions",
            json={"agent_id": agent_id, "title": title},
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@assistant_session.command("list")
@click.option("--limit", type=int, default=50, show_default=True, help="Max sessions to return.")
@click.option("--offset", type=int, default=0, show_default=True, help="Pagination offset.")
@click.option("--channel-id", "channel_id", default=None, help="Filter by bound channel id.")
@click.option("--source", default=None, help='Filter by source: "web" or "channel" (mutually exclusive with --channel-id).')
def assistant_session_list(limit: int, offset: int, channel_id: str | None, source: str | None) -> None:
    """List assistant sessions (newest first) to discover a session_id."""

    async def _run() -> tuple[dict[str, Any], int]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if channel_id is not None:
            params["channel_id"] = channel_id
        if source is not None:
            params["source"] = source
        return await invoke_api(
            "GET",
            "/assistant/sessions",
            params=params,
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@assistant_session.command("get")
@click.argument("session_id")
def assistant_session_get(session_id: str) -> None:
    """Get one assistant session's metadata (status / agent / title / timestamps).

    For the full transcript + spans + model invocations use
    ``assistant export --session-id <id>``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/assistant/sessions/{session_id}",
            meta=read_session_meta(),
            not_found_error_code="assistant_session_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@assistant_session.command("events")
@click.argument("session_id")
@click.option("--after-id", "after_id", default=None, help="Only events after this event_id.")
@click.option("--limit", type=int, default=100, show_default=True, help="Max events to return.")
def assistant_session_events(session_id: str, after_id: str | None, limit: int) -> None:
    """List a session's structured events (tool calls, errors, attempts) for triage."""

    async def _run() -> tuple[dict[str, Any], int]:
        params: dict[str, Any] = {"limit": limit}
        if after_id is not None:
            params["after_id"] = after_id
        return await invoke_api(
            "GET",
            f"/assistant/sessions/{session_id}/events",
            params=params,
            meta=read_session_meta(),
            not_found_error_code="assistant_session_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@assistant.command("chat")
@click.option("--session-id", "session_id", required=True, help="Assistant session id.")
@click.option("--message", default=None, help="Message text to send.")
@click.option("--message-file", "message_file", default=None, type=click.Path(dir_okay=False))
def assistant_chat(session_id: str, message: str | None, message_file: str | None) -> None:
    """Send one message to an existing assistant session."""

    content, err = _message_from_options(message, message_file)
    if err is not None:
        _emit_local_error(err)

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "POST",
            f"/assistant/sessions/{session_id}/messages",
            json={"content": content},
            meta=read_session_meta(),
            not_found_error_code="assistant_session_not_found",
            timeout_seconds=120.0,
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@assistant.command("export")
@click.option("--session-id", "session_id", required=True, help="Assistant session id.")
@click.option("--format", "export_format", type=click.Choice(["json", "markdown"]), default="markdown", show_default=True)
@click.option("--output", default=None, help="Optional path for export_text.")
@click.option("--include-traces/--no-include-traces", default=True, show_default=True)
def assistant_export(session_id: str, export_format: str, output: str | None, include_traces: bool) -> None:
    """Export assistant session diagnostics."""

    async def _run() -> tuple[dict[str, Any], int]:
        envelope, code = await _invoke_export(
            session_id=session_id,
            export_format=export_format,
            include_traces=include_traces,
            meta=read_session_meta(),
        )
        if envelope.get("ok"):
            data, err = _write_export_if_requested(envelope.get("data"), output)
            if err is not None:
                return err, EXIT_FAILURE
            envelope["data"] = data
        return envelope, code

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@assistant.command("run")
@click.option("--agent-id", "agent_id", required=True, help="Assistant agent id, e.g. agent_default or agent-...")
@click.option("--title", default="CLI validation", show_default=True, help="Session title.")
@click.option("--message", default=None, help="Message text to send.")
@click.option("--message-file", "message_file", default=None, type=click.Path(dir_okay=False))
@click.option("--format", "export_format", type=click.Choice(["json", "markdown"]), default="markdown", show_default=True)
@click.option("--output", default=None, help="Optional path for export_text.")
@click.option("--include-traces/--no-include-traces", default=True, show_default=True)
def assistant_run(
    agent_id: str,
    title: str,
    message: str | None,
    message_file: str | None,
    export_format: str,
    output: str | None,
    include_traces: bool,
) -> None:
    """Create a session, send one message, then export diagnostics."""

    content, err = _message_from_options(message, message_file)
    if err is not None:
        _emit_local_error(err)

    async def _run() -> tuple[dict[str, Any], int]:
        meta = read_session_meta()
        created, create_code = await invoke_api(
            "POST",
            "/assistant/sessions",
            json={"agent_id": agent_id, "title": title},
            meta=meta,
        )
        if not created.get("ok"):
            return created, create_code

        session_id = str((created.get("data") or {}).get("session_id") or "")
        if not session_id:
            return error_envelope(
                error_code="assistant_session_create_missing_id",
                message="POST /assistant/sessions returned no session_id.",
                meta=meta,
            ), EXIT_FAILURE

        sent, chat_code = await invoke_api(
            "POST",
            f"/assistant/sessions/{session_id}/messages",
            json={"content": content},
            meta=meta,
            not_found_error_code="assistant_session_not_found",
            timeout_seconds=120.0,
        )
        if not sent.get("ok"):
            diagnostic, _ = await _invoke_export(
                session_id=session_id,
                export_format=export_format,
                include_traces=include_traces,
                meta=meta,
            )
            if diagnostic.get("ok"):
                diagnostic_data, diagnostic_err = _write_export_if_requested(diagnostic.get("data"), output)
                if diagnostic_err is not None:
                    sent.setdefault("error", {}).setdefault("extra", {})["diagnostic_export"] = diagnostic_err.get("error")
                else:
                    sent.setdefault("error", {}).setdefault("extra", {})["diagnostic_export"] = diagnostic_data
            return sent, chat_code

        exported, export_code = await _invoke_export_after_chat_diagnostics(
            session_id=session_id,
            export_format=export_format,
            include_traces=include_traces,
            meta=meta,
            chat_trace_id=(sent.get("data") or {}).get("trace_id"),
        )
        if exported.get("ok"):
            data, err = _write_export_if_requested(exported.get("data"), output)
            if err is not None:
                return err, EXIT_FAILURE
            if isinstance(data, dict):
                data["session_id"] = session_id
                data["chat_trace_id"] = (sent.get("data") or {}).get("trace_id")
            exported["data"] = data
        return exported, export_code

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["assistant"]
