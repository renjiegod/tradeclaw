"""`doyoutrade-cli cron ...` subcommands.

All cron commands route through the running API server. The server owns
``AgentCronManager`` and APScheduler state; a CLI subprocess must not
write cron rows directly or build repositories locally.

Per ``MEMORY.md::feedback_cron_vs_bash_sleep``, agents should route
**all** timing intents (sleep / wait N minutes / remind me / every X)
through the in-process ``create_cron_job`` tool when one is available.
This CLI surface is the fallback for operators / shell pipelines.

API base URL resolution (shared with ``doyoutrade/cli/_api.py``): env
``DOYOUTRADE_API_URL`` → ``cfg.api.base_url`` → derived from
``cfg.server``. When the server isn't running the CLI emits a structured
``api_unavailable`` error envelope instead of a transport traceback.
"""

from __future__ import annotations

import json
import os
from typing import Any

import click

from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._envelope import (
    EXIT_OK,
    EXIT_VALIDATION,
    error_envelope,
    success_envelope,
)
from doyoutrade.cli._invoke import read_session_meta
from doyoutrade.cli.main import run_async_command


def _resolve_agent_id(value: str | None) -> str | None:
    """Return the agent_id to use, falling back to ``DOYOUTRADE_AGENT_ID``."""

    if value and value.strip():
        return value.strip()
    env = os.environ.get("DOYOUTRADE_AGENT_ID")
    return env.strip() if env and env.strip() else None


def _missing_agent_id_envelope() -> tuple[dict[str, Any], int]:
    meta = read_session_meta()
    envelope = error_envelope(
        error_code="missing_agent_id",
        message=(
            "agent_id is required for cron write commands. Pass --agent-id <asst_...> "
            "or set DOYOUTRADE_AGENT_ID."
        ),
        meta=meta,
    )
    return envelope, EXIT_VALIDATION


def _parse_pre_action(raw: str | None) -> tuple[Any, str | None]:
    """Parse the ``--pre-action`` JSON string.

    Returns ``(parsed_value, error_message)``. ``parsed_value`` is the
    decoded JSON (must be a dict with a string ``kind`` to satisfy the
    server validator); ``error_message`` is non-None when the input
    failed to parse / shape-check.
    """

    if raw is None:
        return None, None
    text = raw.strip()
    if not text:
        return None, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"--pre-action must be valid JSON: {exc}"
    if not isinstance(parsed, dict) or not isinstance(parsed.get("kind"), str):
        return None, "--pre-action must be a JSON object with a string 'kind' field."
    return parsed, None


def _parse_json_object_option(raw: str | None, *, option: str) -> tuple[dict[str, Any] | None, str | None]:
    if raw is None:
        return None, None
    text = raw.strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{option} must be valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, f"{option} must be a JSON object."
    return parsed, None


@click.group()
def cron() -> None:
    """Cron job management via API server."""


# ── Read commands ────────────────────────────────────────────────────────


@cron.command("list")
@click.option(
    "--agent-id",
    "agent_id",
    default=None,
    help="Agent id to filter on. Omit to default to the calling agent (DOYOUTRADE_AGENT_ID env).",
)
def cron_list(agent_id: str | None) -> None:
    """List cron jobs for an agent."""

    async def _run() -> tuple[dict[str, Any], int]:
        agent = _resolve_agent_id(agent_id)
        if not agent:
            return _missing_agent_id_envelope()
        return await invoke_api(
            "GET",
            f"/assistant/agents/{agent}/cron/jobs",
            meta=read_session_meta(),
            not_found_error_code="agent_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@cron.command("get")
@click.argument("job_id")
def cron_get(job_id: str) -> None:
    """Get a cron job by id."""

    async def _run() -> tuple[dict[str, Any], int]:
        agent = _resolve_agent_id(None)
        if not agent:
            return _missing_agent_id_envelope()
        return await invoke_api(
            "GET",
            f"/assistant/agents/{agent}/cron/jobs/{job_id}",
            meta=read_session_meta(),
            not_found_error_code="cron_job_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@cron.group("runs")
def cron_runs() -> None:
    """Cron job run history commands."""


@cron_runs.command("list")
@click.argument("job_id")
@click.option("--limit", type=int, default=20, show_default=True, help="Max runs to return.")
def cron_runs_list(job_id: str, limit: int) -> None:
    """List recent runs for a cron job (newest first)."""

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/assistant/cron-jobs/{job_id}/runs",
            params={"limit": limit},
            meta=read_session_meta(),
            not_found_error_code="cron_job_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@cron_runs.command("get")
@click.argument("run_id")
def cron_runs_get(run_id: str) -> None:
    """Get one cron job run by run_id (crun-...).

    The returned run carries ``trace_id`` (the cron.job.fire span's trace),
    ``agent_session_id``, ``pre_run_id`` and ``pre_debug_session_id`` —
    each a handle into a deeper view (``debug get-trace-view``,
    ``assistant session get``, ``debug get-run-view``).
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/assistant/cron-job-runs/{run_id}",
            meta=read_session_meta(),
            not_found_error_code="cron_job_run_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@cron_runs.command("trace")
@click.argument("run_id")
def cron_runs_trace(run_id: str) -> None:
    """Aggregate spans + model_invocations across every session one cron fire touched.

    Walks the fire's agent session, pre-action debug session, and per-instance
    cycle-run sessions, returning a merged ``spans`` / ``model_invocations``
    view for the firing identified by ``run_id`` (crun-...).
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            f"/assistant/cron-job-runs/{run_id}/trace",
            meta=read_session_meta(),
            not_found_error_code="cron_job_run_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@cron_runs.command("by-trace")
@click.argument("trace_id")
@click.option("--limit", type=int, default=50, show_default=True, help="Max runs to return.")
def cron_runs_by_trace(trace_id: str, limit: int) -> None:
    """Reverse-resolve which cron firings carried an OTel trace_id (newest first).

    Use when you have only a trace_id (from a log line / span) and need to find
    the cron run that produced it; then drill in with ``cron runs trace <run_id>``.
    """

    async def _run() -> tuple[dict[str, Any], int]:
        return await invoke_api(
            "GET",
            "/assistant/cron-job-runs",
            params={"trace_id": trace_id, "limit": limit},
            meta=read_session_meta(),
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


# ── Write commands (HTTP to API server) ──────────────────────────────────


@cron.command("create")
@click.option(
    "--agent-id",
    "agent_id",
    default=None,
    help="Agent id that owns the job. Omit to default to DOYOUTRADE_AGENT_ID env.",
)
@click.option("--name", required=True, help="Human-readable job name.")
@click.option(
    "--cron-expression",
    "cron_expression",
    default=None,
    help=(
        "Standard 5-field cron expression for RECURRING schedules. "
        "Mutually exclusive with --at / --in. For one-shot 'fire in N "
        "seconds' intents prefer --in (no TZ math required)."
    ),
)
@click.option(
    "--at",
    "at_iso",
    default=None,
    help=(
        "One-shot fire at this ISO-8601 instant (with offset, e.g. "
        "2026-05-24T10:23:00+08:00). Mutually exclusive with "
        "--cron-expression / --in. The offset eliminates TZ ambiguity."
    ),
)
@click.option(
    "--in",
    "in_duration",
    default=None,
    help=(
        "Relative one-shot delay parsed from a duration string: 60s / "
        "5m / 2h / 1d. Server resolves to --at against the host local "
        "clock — the LLM-safe path that bypasses cron-expression and "
        "timezone entirely."
    ),
)
@click.option(
    "--timezone",
    "tz",
    default=None,
    show_default=False,
    help=(
        "IANA timezone the cron expression is interpreted in. "
        "Defaults to the system local TZ (resolved via tzlocal). "
        "Pass UTC explicitly for portable schedules. Ignored for "
        "--at / --in (those carry / compute the offset directly)."
    ),
)
@click.option(
    "--delete-after-run/--keep-after-run",
    "delete_after_run",
    default=None,
    help=(
        "Whether the job is auto-deleted after a terminal-state fire. "
        "Defaults: true for --at / --in (one-shot), false for "
        "--cron-expression (recurring). Explicit values override."
    ),
)
@click.option(
    "--input-template",
    "input_template",
    default=None,
    help="Jinja2 template rendered with {{now}} / {{job}} / {{pre}} into the agent message.",
)
@click.option("--task-kind", "task_kind", default=None, help="Task-pipeline kind: agent_chat_reply (reminder/Q&A push) or daily_review (每日复盘 — pre-gathers the account statement + KB digest, composes a review, writes it to the knowledge journal, and pushes it). Strategy execution is scheduled via a Task Trigger (doyoutrade-cli task trigger add ...), not a cron task_kind.")
@click.option("--task-params", "task_params_raw", default=None, help="JSON object stored as task_params_json for --task-kind.")
@click.option(
    "--max-concurrency",
    "max_concurrency",
    type=int,
    default=1,
    show_default=True,
)
@click.option(
    "--timeout-seconds",
    "timeout_seconds",
    type=int,
    default=120,
    show_default=True,
)
@click.option(
    "--enabled/--disabled",
    "enabled",
    default=True,
    show_default=True,
    help="Whether the job fires after creation.",
)
@click.option(
    "--pre-action",
    "pre_action_raw",
    default=None,
    help="Optional JSON object for the pre-action step (e.g. '{\"kind\":\"trigger_cycle\"}').",
)
def cron_create(
    agent_id: str | None,
    name: str,
    cron_expression: str | None,
    at_iso: str | None,
    in_duration: str | None,
    tz: str | None,
    delete_after_run: bool | None,
    input_template: str | None,
    task_kind: str | None,
    task_params_raw: str | None,
    max_concurrency: int,
    timeout_seconds: int,
    enabled: bool,
    pre_action_raw: str | None,
) -> None:
    """Create a new cron job on the running API server.

    Three schedule shapes are accepted (mutually exclusive):

    * ``--cron-expression`` — recurring 5-field cron.
    * ``--at <ISO-8601>``    — one-shot at an explicit instant.
    * ``--in <duration>``    — one-shot at ``now + duration`` (host
      local clock; preferred for LLM "fire in N seconds" intents).
    """

    async def _run() -> tuple[dict[str, Any], int]:
        agent = _resolve_agent_id(agent_id)
        if not agent:
            return _missing_agent_id_envelope()
        # Enforce mutual exclusivity at CLI boundary so callers see a
        # clean validation_error before any HTTP round-trip.
        provided = [
            label for label, value in (
                ("--cron-expression", cron_expression),
                ("--at", at_iso),
                ("--in", in_duration),
            ) if value is not None
        ]
        if len(provided) == 0:
            return error_envelope(
                error_code="schedule_required",
                message=(
                    "Exactly one of --cron-expression / --at / --in is "
                    "required. For 'fire in N seconds' use --in 60s."
                ),
                meta=read_session_meta(),
            ), EXIT_VALIDATION
        if len(provided) > 1:
            return error_envelope(
                error_code="schedule_conflict",
                message=(
                    f"Multiple schedule flags passed ({', '.join(provided)}); "
                    "exactly one of --cron-expression / --at / --in is allowed."
                ),
                meta=read_session_meta(),
            ), EXIT_VALIDATION
        has_input_template = bool(input_template and input_template.strip())
        has_task_kind = bool(task_kind and task_kind.strip())
        if has_input_template and has_task_kind:
            return error_envelope(
                error_code="conflicting_execution_form",
                message="--input-template and --task-kind are mutually exclusive.",
                meta=read_session_meta(),
            ), EXIT_VALIDATION
        if not has_input_template and not has_task_kind:
            return error_envelope(
                error_code="missing_execution_form",
                message="Pass --input-template for legacy cron jobs or --task-kind with --task-params for task-pipeline jobs.",
                meta=read_session_meta(),
            ), EXIT_VALIDATION
        pre_action, err = _parse_pre_action(pre_action_raw)
        if err is not None:
            envelope = error_envelope(
                error_code="invalid_pre_action_json",
                message=err,
                meta=read_session_meta(),
            )
            return envelope, EXIT_VALIDATION
        task_params, task_err = _parse_json_object_option(task_params_raw, option="--task-params")
        if task_err is not None:
            return error_envelope(
                error_code="invalid_task_params_json",
                message=task_err,
                meta=read_session_meta(),
            ), EXIT_VALIDATION
        payload: dict[str, Any] = {
            "name": name,
            "max_concurrency": max_concurrency,
            "timeout_seconds": timeout_seconds,
            "enabled": enabled,
        }
        if has_input_template:
            payload["input_template"] = input_template
        if has_task_kind:
            payload["task_kind"] = task_kind.strip()
            payload["task_params_json"] = task_params or {}
        # Resolve TZ to host local when caller omitted it. We do this
        # client-side rather than server-side so the response echoes
        # the value actually stored and the LLM can copy-paste it on
        # follow-up updates.
        resolved_tz = tz or _resolve_local_iana_tz()
        if cron_expression is not None:
            payload["schedule_kind"] = "cron"
            payload["cron_expression"] = cron_expression
            payload["timezone"] = resolved_tz
        elif at_iso is not None:
            payload["schedule_kind"] = "at"
            payload["at_iso"] = at_iso
        else:
            # --in: pass the raw duration string to the API; the
            # server resolves it against its host clock. This keeps
            # the round-trip transparent ("now+60s" really means
            # "60s after the API received the request").
            payload["schedule_kind"] = "at"
            payload["in_duration"] = in_duration
        if delete_after_run is not None:
            payload["delete_after_run"] = delete_after_run
        if pre_action is not None:
            payload["pre_action"] = pre_action
        return await invoke_api(
            "POST",
            f"/assistant/agents/{agent}/cron/jobs",
            json=payload,
            meta=read_session_meta(),
            not_found_error_code="agent_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


def _resolve_local_iana_tz() -> str:
    """Best-effort host-local IANA TZ key. Falls back to UTC if
    tzlocal isn't installed or returns no IANA name. Same convention
    as ``AgentCronManager._resolve_system_iana_tz``."""
    try:
        from tzlocal import get_localzone
    except ImportError:
        return "UTC"
    try:
        zi = get_localzone()
    except Exception:
        return "UTC"
    return getattr(zi, "key", None) or "UTC"


@cron.command("update")
@click.argument("job_id")
@click.option(
    "--agent-id",
    "agent_id",
    default=None,
    help="Owning agent id (path scope). Defaults to DOYOUTRADE_AGENT_ID.",
)
@click.option("--name", default=None)
@click.option("--cron-expression", "cron_expression", default=None)
@click.option("--timezone", "tz", default=None)
@click.option("--input-template", "input_template", default=None)
@click.option("--task-kind", "task_kind", default=None)
@click.option("--task-params", "task_params_raw", default=None)
@click.option("--max-concurrency", "max_concurrency", type=int, default=None)
@click.option("--timeout-seconds", "timeout_seconds", type=int, default=None)
@click.option(
    "--enabled/--disabled",
    "enabled",
    default=None,
    help="Tri-state: pass --enabled or --disabled to change; omit to leave unchanged.",
)
@click.option(
    "--pre-action",
    "pre_action_raw",
    default=None,
    help="Replacement JSON for pre_action.",
)
@click.option(
    "--clear-pre-action",
    "clear_pre_action",
    is_flag=True,
    default=False,
    help="Explicitly set pre_action to null (clears any existing pre-action).",
)
def cron_update(
    job_id: str,
    agent_id: str | None,
    name: str | None,
    cron_expression: str | None,
    tz: str | None,
    input_template: str | None,
    task_kind: str | None,
    task_params_raw: str | None,
    max_concurrency: int | None,
    timeout_seconds: int | None,
    enabled: bool | None,
    pre_action_raw: str | None,
    clear_pre_action: bool,
) -> None:
    """Patch an existing cron job (only sends provided fields)."""

    async def _run() -> tuple[dict[str, Any], int]:
        agent = _resolve_agent_id(agent_id)
        if not agent:
            return _missing_agent_id_envelope()
        if clear_pre_action and pre_action_raw is not None:
            envelope = error_envelope(
                error_code="validation_error",
                message="--clear-pre-action and --pre-action are mutually exclusive.",
                meta=read_session_meta(),
            )
            return envelope, EXIT_VALIDATION
        if input_template is not None and task_kind is not None:
            return error_envelope(
                error_code="conflicting_execution_form",
                message="--input-template and --task-kind are mutually exclusive.",
                meta=read_session_meta(),
            ), EXIT_VALIDATION
        pre_action, err = _parse_pre_action(pre_action_raw)
        if err is not None:
            envelope = error_envelope(
                error_code="invalid_pre_action_json",
                message=err,
                meta=read_session_meta(),
            )
            return envelope, EXIT_VALIDATION
        task_params, task_err = _parse_json_object_option(task_params_raw, option="--task-params")
        if task_err is not None:
            return error_envelope(
                error_code="invalid_task_params_json",
                message=task_err,
                meta=read_session_meta(),
            ), EXIT_VALIDATION
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if cron_expression is not None:
            payload["cron_expression"] = cron_expression
        if tz is not None:
            payload["timezone"] = tz
        if input_template is not None:
            payload["input_template"] = input_template
        if task_kind is not None:
            if not task_kind.strip():
                return error_envelope(
                    error_code="invalid_task_kind",
                    message="--task-kind must be a non-empty string.",
                    meta=read_session_meta(),
                ), EXIT_VALIDATION
            payload["task_kind"] = task_kind.strip()
            payload["task_params_json"] = task_params or {}
        elif task_params is not None:
            # Standalone --task-params (no --task-kind): update the params of an
            # existing task-pipeline cron in place — the common "tweak one param
            # like no_signal_mode" flow. Without this branch the option parsed
            # but was silently dropped (a §错误可见性 silent no-op).
            payload["task_params_json"] = task_params
        if max_concurrency is not None:
            payload["max_concurrency"] = max_concurrency
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        if enabled is not None:
            payload["enabled"] = enabled
        if clear_pre_action:
            payload["pre_action"] = None
        elif pre_action is not None:
            payload["pre_action"] = pre_action
        if not payload:
            envelope = success_envelope(
                {"_summary": "no fields to update; nothing changed."},
                "",
                meta=read_session_meta(),
            )
            return envelope, EXIT_OK
        return await invoke_api(
            "PUT",
            f"/assistant/agents/{agent}/cron/jobs/{job_id}",
            json=payload,
            meta=read_session_meta(),
            not_found_error_code="cron_job_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@cron.command("delete")
@click.argument("job_id")
@click.option(
    "--agent-id",
    "agent_id",
    default=None,
    help="Owning agent id (path scope). Defaults to DOYOUTRADE_AGENT_ID.",
)
def cron_delete(job_id: str, agent_id: str | None) -> None:
    """Delete a cron job from the running server."""

    async def _run() -> tuple[dict[str, Any], int]:
        agent = _resolve_agent_id(agent_id)
        if not agent:
            return _missing_agent_id_envelope()
        return await invoke_api(
            "DELETE",
            f"/assistant/agents/{agent}/cron/jobs/{job_id}",
            meta=read_session_meta(),
            not_found_error_code="cron_job_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@cron.command("pause")
@click.argument("job_id")
@click.option(
    "--agent-id",
    "agent_id",
    default=None,
    help="Owning agent id (path scope). Defaults to DOYOUTRADE_AGENT_ID.",
)
def cron_pause(job_id: str, agent_id: str | None) -> None:
    """Pause a cron job — keeps the row but deregisters from scheduler."""

    async def _run() -> tuple[dict[str, Any], int]:
        agent = _resolve_agent_id(agent_id)
        if not agent:
            return _missing_agent_id_envelope()
        return await invoke_api(
            "POST",
            f"/assistant/agents/{agent}/cron/jobs/{job_id}/pause",
            meta=read_session_meta(),
            not_found_error_code="cron_job_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@cron.command("resume")
@click.argument("job_id")
@click.option(
    "--agent-id",
    "agent_id",
    default=None,
    help="Owning agent id (path scope). Defaults to DOYOUTRADE_AGENT_ID.",
)
def cron_resume(job_id: str, agent_id: str | None) -> None:
    """Resume a paused cron job."""

    async def _run() -> tuple[dict[str, Any], int]:
        agent = _resolve_agent_id(agent_id)
        if not agent:
            return _missing_agent_id_envelope()
        return await invoke_api(
            "POST",
            f"/assistant/agents/{agent}/cron/jobs/{job_id}/resume",
            meta=read_session_meta(),
            not_found_error_code="cron_job_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


@cron.command("trigger")
@click.argument("job_id")
@click.option(
    "--agent-id",
    "agent_id",
    default=None,
    help="Owning agent id (path scope). Defaults to DOYOUTRADE_AGENT_ID.",
)
def cron_trigger(job_id: str, agent_id: str | None) -> None:
    """Fire a cron job once now. Returns the new cron_job_run_id."""

    async def _run() -> tuple[dict[str, Any], int]:
        agent = _resolve_agent_id(agent_id)
        if not agent:
            return _missing_agent_id_envelope()
        return await invoke_api(
            "POST",
            f"/assistant/agents/{agent}/cron/jobs/{job_id}/run",
            meta=read_session_meta(),
            not_found_error_code="cron_job_not_found",
        )

    ctx = click.get_current_context()
    ctx.exit(run_async_command(_run))


__all__ = ["cron"]
