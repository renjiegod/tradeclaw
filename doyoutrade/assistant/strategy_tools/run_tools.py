from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from doyoutrade.tools import (
    OperationHandler,
    TERMINAL_BACKTEST_STATUSES,
    ToolResult,
    _exception_metadata,
    _safe_lookup_existing_runs,
    tool_result_from_error_dict,
)
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._identifier_kinds import IdentifierGuard, IdentifierKind
from doyoutrade.tools._prose import (
    append_json_payload,
    format_error_text,
    format_unknown_args,
)
from doyoutrade.backtest.summary import render_summary_markdown
from doyoutrade.debug import emit_debug_event
from doyoutrade.persistence import (
    RecordNotFoundError,
    SqlAlchemyStrategyDefinitionRepository,
)
from doyoutrade.strategies.inspect_resources import build_strategy_inspect_payload


logger = logging.getLogger(__name__)


_ATTACHABLE_BACKTEST_STATUSES = frozenset({"running", "queued", "starting", "pending", ""})


# Environment override for the on-disk reports directory. Tests and isolated
# sandboxes can redirect output without touching the user's home directory.
_REPORTS_DIR_ENV_VAR = "DOYOUTRADE_REPORTS_DIR"
_DEFAULT_REPORTS_DIR = "~/.doyoutrade/reports"

# Restrict file stems derived from run_id to a portable, shell-safe charset.
# Anything outside this set falls back to the timestamp-based path so a hostile
# or malformed run_id (e.g. containing ``/``) cannot escape the reports dir.
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _resolve_reports_dir() -> Path:
    """Return the directory where backtest reports should be persisted.

    Honours ``$DOYOUTRADE_REPORTS_DIR`` first, then falls back to
    ``~/.doyoutrade/reports``. The returned path is fully expanded but not yet
    created — callers must ``mkdir(parents=True, exist_ok=True)`` themselves
    so they can surface the underlying ``OSError`` if creation fails.
    """

    raw = os.environ.get(_REPORTS_DIR_ENV_VAR)
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip()).expanduser()
    return Path(_DEFAULT_REPORTS_DIR).expanduser()


async def _write_backtest_report_to_disk(
    *,
    run_id: str | None,
    markdown: str,
) -> str | None:
    """Persist the rendered markdown to disk and return its absolute path.

    Returns ``None`` on any failure — caller MUST treat that as "no
    ``report_path`` to advertise" and fall back to inlining the markdown so
    we never silently lose the report. Every failure path also emits a
    structured ``backtest_run_report_persist_failed`` debug event and logs a
    warning with the exception type, message, and ``run_id`` context so the
    operator can spot truncated / missing reports.
    """

    reports_dir = _resolve_reports_dir()
    safe_run_id = run_id if isinstance(run_id, str) and _SAFE_RUN_ID_RE.match(run_id) else None
    if safe_run_id:
        target = reports_dir / f"{safe_run_id}.md"
    else:
        # Missing or unsafe run_id — fall back to a timestamped name so the
        # report still lands on disk, and emit an event so the gap is visible.
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        target = reports_dir / f"backtest-report-{ts}.md"
        await emit_debug_event(
            "backtest_run_report_persist_failed",
            {
                "run_id": run_id,
                "reason": "unsafe_or_missing_run_id",
                "fallback_path": str(target),
                "hint": (
                    "run_id was missing or contained characters outside "
                    "[A-Za-z0-9._-]; wrote report under a timestamped name "
                    "so it is still retrievable."
                ),
            },
        )
        logger.warning(
            "backtest_run_report_persist: run_id %r is missing or unsafe; "
            "writing report to fallback path %s",
            run_id,
            target,
        )

    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        await emit_debug_event(
            "backtest_run_report_persist_failed",
            {
                "run_id": run_id,
                "report_path": str(target),
                "error_type": type(exc).__name__,
                "message": str(exc),
                "hint": (
                    "could not write backtest report to disk; falling back "
                    "to inlining the markdown into ToolResult.text so the "
                    "agent can still see it."
                ),
            },
        )
        logger.warning(
            "backtest_run_report_persist: failed to write report for run_id=%r "
            "to %s: %s: %s",
            run_id,
            target,
            type(exc).__name__,
            exc,
        )
        return None

    await emit_debug_event(
        "backtest_run_report_persisted",
        {
            "run_id": run_id,
            "report_path": str(target),
            "byte_size": len(markdown.encode("utf-8")),
        },
    )
    return str(target)


# Whitelists for the ``backtest_job`` dict that lands in the assistant
# tool envelope. KPIs and full row state live in the markdown report
# (via ``report_path``) and in ``get_backtest_job`` / ``get_run_debug_view``;
# the envelope only needs the identifiers the agent follows up with plus
# whatever extra context distinguishes the branch:
#
# * ``_BASIC`` — default. The terminal-success and fire-and-forget cases
#   only need ``run_id`` / ``task_id`` / ``status``; KPIs are in the
#   on-disk report.
# * ``_PROGRESS`` — timeout. Adds ``bars_*`` so the agent can judge
#   whether the run is making progress before deciding to wait again.
# * ``_FAILURE`` — failed-run payload. Adds ``error_message`` because
#   the failure cause is what the agent needs first.
#
# Stripped from every variant: ``mode`` / ``market_profile`` /
# ``bar_interval`` / ``range_*_utc`` / ``model_route_name`` (caller
# already passed these in); ``session_id`` / ``stop_requested`` /
# ``reference_starting_equity`` / ``created_at`` / ``started_at`` /
# ``finished_at`` (worker internals); ``ledger_checkpoint_json`` /
# ``config_snapshot_json`` (bulk state snapshots); KPIs like
# ``return_pct`` / ``starting_equity`` / ``ending_equity`` (in report).
#
# Keeping the envelope small matters because the bash tool truncates
# results past a per-call character budget — the regression at session
# ``asst-f9826c84c5fd`` had ``report_path`` pushed past that boundary
# by previously-included bulk fields, and the model fabricated metrics.
_BACKTEST_JOB_BASIC_FIELDS: tuple[str, ...] = ("run_id", "task_id", "status")
_BACKTEST_JOB_PROGRESS_FIELDS: tuple[str, ...] = (
    *_BACKTEST_JOB_BASIC_FIELDS,
    "bars_total",
    "bars_completed",
)
_BACKTEST_JOB_FAILURE_FIELDS: tuple[str, ...] = (
    *_BACKTEST_JOB_BASIC_FIELDS,
    "error_message",
)


def _compact_backtest_job(
    job: dict[str, Any] | None,
    *,
    fields: tuple[str, ...] = _BACKTEST_JOB_BASIC_FIELDS,
) -> dict[str, Any]:
    """Return a minimal snapshot of ``job`` for assistant tool envelopes.

    Only keys in ``fields`` survive; the original ``job`` dict is left
    untouched so persistence / DB contracts stay intact (the e2e
    ``runs.ledger_checkpoint_json`` relationship still holds). Missing
    keys are dropped silently — callers that need a full row should call
    ``get_backtest_job`` directly.
    """

    if not isinstance(job, dict):
        return {}
    return {k: job[k] for k in fields if k in job}


# Forwarded to the user after a successful backtest. Markdown comes from
# ``render_summary_markdown`` so the agent can pass it through verbatim
# instead of re-rendering the dense JSON fields itself.
_BACKTEST_NEXT_STEP_HINT = (
    "After sharing the report with the user, optionally call "
    "suggest_strategy_iteration(run_id=...) to pick the next change."
)


class RunStrategyBacktestTool(OperationHandler):
    name = "run_strategy_backtest"
    description = (
        "One-shot backtest entry: starts a run, waits for terminal status, "
        "returns the final result. Two input modes:\n"
        "  • task mode — pass an existing backtest ``task_id`` (uuid).\n"
        "  • definition mode — pass a ``definition_id`` (``sd-...``) plus "
        "``universe`` and optional ``parameters``; the tool auto-creates a "
        "backtest task bound directly to the definition.\n"
        "Pick task mode when iterating on an existing task; pick definition "
        "mode for the default post-authoring path.\n"
        "Default ``timeout_seconds=120`` (waits for completion). Pass "
        "``timeout_seconds=0`` for fire-and-forget — returns immediately "
        "after the run is queued.\n"
        "If a run already exists for the task this call attaches: running → "
        "waits; completed → returns; failed → ``backtest_run_failed`` (no "
        "auto-clone).\n"
        "Known ``error_code`` values: "
        "``missing_task_or_definition_id`` (caller gave neither "
        "task_id nor definition_id); "
        "``conflicting_backtest_entry_mode`` (caller gave both entry ids); "
        "``missing_universe_for_auto_create_mode`` (auto-create needs "
        "``universe``); "
        "``auto_create_task_failed`` (platform refused to create the auto-"
        "task — payload carries the underlying ``ValueError``); "
        "``invalid_config_overrides_json`` / ``invalid_universe_json`` "
        "(malformed JSON-string fallback for that field); "
        "``wrong_identifier_type`` (id shape mismatch); "
        "``backtest_validation_error`` (date / range validation failed); "
        "``backtest_run_already_exists`` (task has a non-attachable existing "
        "run; payload carries ``existing_run_ids`` / ``existing_run_status``); "
        "``backtest_run_failed`` (existing run terminated in ``failed`` — "
        "inspect via ``get_run_debug_view`` then ``clone_task`` if needed); "
        "``backtest_wait_timeout`` (run still executing past "
        "``timeout_seconds`` — call again to resume waiting); "
        "``backtest_start_failed`` (unhandled error during start)."
    )
    category = "strategy"
    # The markdown report + JSON payload must reach the model intact —
    # ``strategy-authoring`` instructs the agent to forward the body
    # verbatim, which only works if the registry's disk-spill and
    # ``micro_compact_messages`` both leave it alone.
    bypass_result_truncation = True
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task mode entry: existing backtest task id (uuid). "
                    "Mutually exclusive with ``definition_id``."
                ),
            },
            "definition_id": {
                "type": "string",
                "description": (
                    "Definition mode entry: strategy definition id (``sd-...``). "
                    "Tool auto-creates a backtest task with optional "
                    "``parameters`` overrides. Requires ``universe``. "
                    "Mutually exclusive with ``task_id``."
                ),
            },
            "parameters": {
                "type": "object",
                "description": (
                    "Strategy parameter overrides for definition mode. "
                    "Merged with definition defaults at run time."
                ),
            },
            "range_start": {
                "type": "string",
                "description": "Inclusive start date ``YYYY-MM-DD``.",
            },
            "range_end": {
                "type": "string",
                "description": "Inclusive end date ``YYYY-MM-DD``.",
            },
            "universe": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Symbols for the run. Required in definition mode; "
                    "ignored in task mode (the task carries its own universe)."
                ),
            },
            "name": {
                "type": "string",
                "description": (
                    "Optional name for the auto-created task in definition "
                    "mode. Defaults to ``{definition_id}@{range_start}-{range_end}``."
                ),
            },
            "data_provider": {
                "type": "string",
                "description": (
                    "Optional data provider for the auto-created task "
                    "(auto/qmt/mock/akshare). Defaults to ``auto``."
                ),
            },
            "config_overrides": {
                "type": "object",
                "description": (
                    "Optional per-run overrides merged onto the task's "
                    "persisted settings. Allowed top-level keys: ``settings`` "
                    "(deep-merged) and ``universe`` (replaces task universe). "
                    "JSON-string fallback rejected with "
                    "``invalid_config_overrides_json`` if malformed."
                ),
            },
            "market_profile": {
                "type": "string",
                "description": "Optional market profile (default ``cn_a_share``).",
            },
            "bar_interval": {
                "type": "string",
                "description": "Optional bar interval (default ``1d``).",
            },
            "model_route_name": {
                "type": "string",
                "description": "Optional named model route override for this run.",
            },
            "debug_enabled": {
                "type": "boolean",
                "description": (
                    "Whether to capture full debug observability for this run. "
                    "Defaults true (records debug session, OTel spans, per-bar "
                    "cycle traces and model invocations — needed for "
                    "``get_run_debug_view``). Pass false for fast mode: skips all "
                    "that trace persistence so the backtest runs noticeably "
                    "faster, keeping only the run status, report and trade fills. "
                    "When false, ``get_run_debug_view`` returns no spans / cycles "
                    "/ model invocations by design — that is not an error."
                ),
            },
            "timeout_seconds": {
                "type": "number",
                "description": (
                    "How long to wait for terminal status. Default 120. "
                    "Set to 0 for fire-and-forget (returns immediately with "
                    "``status=running``)."
                ),
            },
            "poll_interval_seconds": {
                "type": "number",
                "description": "Polling interval while waiting. Default 0.2.",
            },
        },
        "required": ["range_start", "range_end"],
    }

    coercion_rules = (
        SchemaCoercion(field="config_overrides", declared_type="object"),
        SchemaCoercion(field="parameters", declared_type="object"),
        SchemaCoercion(field="universe", declared_type="array", item_type=str),
    )

    identifier_guards = (
        IdentifierGuard(field="task_id", kind=IdentifierKind.TASK_ID),
        IdentifierGuard(field="definition_id", kind=IdentifierKind.DEFINITION_ID),
    )

    def __init__(self, platform_service: Any | None):
        self._platform_service = platform_service

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
        }
        if contract.error is not None:
            await emit_debug_event(
                f"operation_{self.name}."
                f"{'rejected' if contract.error_kind == 'unknown_arguments' else 'failed'}",
                {**base_payload, "error": contract.error},
            )
            if contract.error_kind == "unknown_arguments":
                text = format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                )
            else:
                text = format_error_text(
                    "validation_error",
                    str(
                        contract.error.get("message")
                        or contract.error.get("error")
                        or "validation failed"
                    ),
                )
            return ToolResult(
                text=text,
                is_error=True,
            )

        kwargs = contract.kwargs
        guard = self._apply_identifier_guards(kwargs)
        if guard is not None:
            await emit_debug_event(
                f"operation_{self.name}.rejected",
                {**base_payload, "error": guard},
            )
            return tool_result_from_error_dict(guard)

        coercion = self._apply_schema_coercion(kwargs)
        if coercion.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error": coercion.error},
            )
            return ToolResult(
                text=format_error_text(
                    str(coercion.error.get("error_code") or "coercion_error"),
                    str(coercion.error.get("error") or "input coercion failed"),
                ),
                is_error=True,
            )
        kwargs = coercion.kwargs

        # Range validation (mirrors BacktestTool legacy contract — empty / whitespace rejected).
        range_start = kwargs.get("range_start")
        range_end = kwargs.get("range_end")
        missing_range = [
            field
            for field, value in (("range_start", range_start), ("range_end", range_end))
            if not isinstance(value, str) or not value.strip()
        ]
        if missing_range:
            err: dict[str, Any] = {
                "status": "error",
                "tool": self.name,
                "error_code": "backtest_validation_error",
                "error_type": "ValueError",
                "error": (
                    "missing required argument(s): " + ", ".join(missing_range)
                ),
                "missing": missing_range,
                "repair_hints": [
                    "run_strategy_backtest is one-shot per call: pass "
                    "range_start and range_end (YYYY-MM-DD) every time, "
                    "even when resuming wait on an existing run.",
                    "to inspect or wait on an existing backtest job without "
                    "restarting it, use get_backtest_job or get_run_debug_view.",
                ],
            }
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "backtest_validation_error"},
            )
            return ToolResult(
                text=format_error_text(
                    "backtest_validation_error",
                    err["error"],
                    "; ".join(err["repair_hints"]),
                ),
                is_error=True,
            )

        # Entry-mode selection — task vs definition (mutually exclusive).
        task_id = kwargs.get("task_id")
        definition_id = kwargs.get("definition_id")
        task_mode = bool(isinstance(task_id, str) and task_id.strip())
        definition_mode = bool(isinstance(definition_id, str) and definition_id.strip())
        active_modes = sum((task_mode, definition_mode))
        if active_modes > 1:
            err = {
                "status": "error",
                "tool": self.name,
                "error_code": "conflicting_backtest_entry_mode",
                "error_type": "ValueError",
                "error": "pass exactly one of task_id or definition_id",
                "repair_hints": [
                    "task_id alone runs the existing task as-is",
                    "definition_id (+ universe + optional parameters) auto-creates from sd-...",
                ],
            }
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "conflicting_backtest_entry_mode"},
            )
            return ToolResult(
                text=format_error_text(
                    "conflicting_backtest_entry_mode",
                    err["error"],
                    "; ".join(err["repair_hints"]),
                ),
                is_error=True,
            )
        if active_modes == 0:
            err = {
                "status": "error",
                "tool": self.name,
                "error_code": "missing_task_or_definition_id",
                "error_type": "ValueError",
                "error": "task_id or definition_id is required",
                "repair_hints": [
                    "pass task_id to run an existing backtest task",
                    "pass definition_id (+ universe + optional parameters) after authoring",
                ],
            }
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "missing_task_or_definition_id"},
            )
            return ToolResult(
                text=format_error_text(
                    "missing_task_or_definition_id",
                    err["error"],
                    "; ".join(err["repair_hints"]),
                ),
                is_error=True,
            )

        if self._platform_service is None or not hasattr(
            self._platform_service, "start_backtest_job"
        ):
            return ToolResult(
                text=format_error_text(
                    "ServiceUnavailable",
                    "doyoutrade-cli backtest service is not available",
                ),
                is_error=True,
            )

        # Auto-create the backtest task when in definition mode.
        auto_created_task_id: str | None = None
        if definition_mode:
            universe = kwargs.get("universe")
            if not isinstance(universe, list) or not universe:
                err = {
                    "status": "error",
                    "tool": self.name,
                    "error_code": "missing_universe_for_auto_create_mode",
                    "error_type": "ValueError",
                    "error": (
                        "auto-create mode requires a non-empty universe (the auto-"
                        "created task needs to know which symbols to trade)"
                    ),
                    "repair_hints": [
                        "pass universe=['SYMBOL1', 'SYMBOL2', ...]",
                        "or use task mode (task_id=...) against a task that "
                        "already carries its own universe",
                    ],
                }
                await emit_debug_event(
                    f"operation_{self.name}.failed",
                    {**base_payload, "error_code": "missing_universe_for_auto_create_mode"},
                )
                return ToolResult(
                    text=format_error_text(
                        "missing_universe_for_auto_create_mode",
                        err["error"],
                        "; ".join(err["repair_hints"]),
                    ),
                    is_error=True,
                )

            name = kwargs.get("name")
            if not isinstance(name, str) or not name.strip():
                name = f"{definition_id}@{range_start}-{range_end}"

            strategy_block: dict[str, Any] = {"definition_id": definition_id}
            parameters = kwargs.get("parameters")
            if isinstance(parameters, dict) and parameters:
                strategy_block["parameter_overrides"] = parameters

            settings_payload = {
                "strategy": strategy_block,
                "universe": list(universe),
            }
            try:
                from doyoutrade.runtime.cycle_task import validate_api_task_settings

                validate_api_task_settings(settings_payload)
                created = await self._platform_service.create_task(
                    name=name,
                    mode="backtest",
                    description="",
                    data_provider=kwargs.get("data_provider") or "auto",
                    settings=settings_payload,
                )
            except Exception as exc:
                metadata = _exception_metadata(exc)
                err = {
                    "status": "error",
                    "tool": self.name,
                    "error_code": "auto_create_task_failed",
                    **metadata,
                    "repair_hints": [
                        "inspect the strategy definition with get_strategy_definition",
                        "verify the universe symbols are valid for the data_provider",
                    ],
                }
                await emit_debug_event(
                    f"operation_{self.name}.failed",
                    {**base_payload, "error_code": "auto_create_task_failed", **metadata},
                )
                return ToolResult(
                    text=format_error_text(
                        "auto_create_task_failed",
                        str(metadata.get("error") or "auto-create task failed"),
                        "; ".join(err["repair_hints"]),
                    ),
                    is_error=True,
                )
            task_id = getattr(created, "task_id", None)
            if not isinstance(task_id, str) or not task_id:
                err = {
                    "status": "error",
                    "tool": self.name,
                    "error_code": "auto_create_task_failed",
                    "error": "platform did not return a task_id for the auto-created backtest task",
                }
                return ToolResult(
                    text=format_error_text(
                        "auto_create_task_failed",
                        err["error"],
                    ),
                    is_error=True,
                )
            auto_created_task_id = task_id
            await emit_debug_event(
                f"operation_{self.name}.created",
                {
                    **base_payload,
                    "auto_created_task": True,
                    "task_id": task_id,
                    "definition_id": definition_id if definition_mode else None,
                    "name": name,
                },
            )

        await emit_debug_event(
            f"operation_{self.name}.validated",
            {
                **base_payload,
                "task_id": task_id,
                "auto_created_task_id": auto_created_task_id,
            },
        )

        timeout_seconds = float(kwargs.get("timeout_seconds", 120.0) or 0.0)
        poll_interval_seconds = float(kwargs.get("poll_interval_seconds", 0.2) or 0.0)

        # After entry-mode dispatch task_id is guaranteed non-empty; the assert
        # narrows the type for pyright (definition-mode set it via create_task).
        assert isinstance(task_id, str) and task_id

        # Start the run; on "already has a run" dispatch to attach handling.
        try:
            row = await self._platform_service.start_backtest_job(
                task_id,
                range_start=range_start,
                range_end=range_end,
                bar_interval=kwargs.get("bar_interval"),
                market_profile=kwargs.get("market_profile"),
                config_overrides=kwargs.get("config_overrides"),
                model_route_name=kwargs.get("model_route_name"),
                debug_enabled=bool(kwargs.get("debug_enabled", True)),
            )
        except Exception as exc:
            metadata = _exception_metadata(exc)
            message = metadata["error"]
            if isinstance(exc, ValueError) and "already has a run" in str(exc):
                return await self._handle_existing_run(
                    task_id=task_id,
                    auto_created_task_id=auto_created_task_id,
                    start_exception_metadata=metadata,
                    start_exception_message=message,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                )
            # Specialise RecordNotFoundError on the strategy lookup so the
            # envelope ships a stable token + repair hint that the agent
            # can self-correct against in one round trip. Pre-fix this fell
            # through to the generic ``backtest_start_failed`` with no
            # repair_hints (hallucinated definition ids).
            repair_hints: list[str] = []
            if isinstance(exc, RecordNotFoundError):
                exc_message = str(exc)
                if "strategy definition not found" in exc_message:
                    error_code = "strategy_definition_not_found"
                    repair_hints = [
                        "do not invent sd-… ids from a strategy name",
                        "run `doyoutrade-cli strategy inspect --query <keyword>` "
                        "(or `strategy definition list --query <keyword>`) and "
                        "use the `definition_id` field from the envelope verbatim",
                    ]
                else:
                    error_code = "backtest_start_failed"
            elif isinstance(exc, ValueError):
                error_code = "backtest_validation_error"
            else:
                error_code = "backtest_start_failed"
            err = {
                "status": "error",
                "tool": self.name,
                "error_code": error_code,
                **metadata,
            }
            if repair_hints:
                err["repair_hints"] = repair_hints
            if auto_created_task_id is not None:
                err["auto_created_task_id"] = auto_created_task_id
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    **base_payload,
                    "error_code": error_code,
                    "error": message,
                    "hint": (
                        "look up the real id via `strategy inspect --query …`"
                        if repair_hints
                        else None
                    ),
                },
            )
            hint_text = "; ".join(err.get("repair_hints") or []) or None
            error_prose = format_error_text(error_code, str(message), hint_text)
            if auto_created_task_id is not None:
                error_prose += f"\nauto_created_task_id: {auto_created_task_id}"
            return ToolResult(
                text=append_json_payload(error_prose, err),
                is_error=True,
            )

        run_id = row.get("run_id") if isinstance(row, dict) else None
        if not isinstance(run_id, str) or not run_id:
            err = {
                "status": "error",
                "error_type": "InvalidBacktestRunId",
                "error": "backtest job did not return a valid run_id",
            }
            return ToolResult(
                text=format_error_text(
                    "InvalidBacktestRunId",
                    err["error"],
                ),
                is_error=True,
            )

        # timeout_seconds <= 0 → fire-and-forget: return the just-queued row.
        if timeout_seconds <= 0:
            payload: dict[str, Any] = {
                "status": "ok",
                "backtest_job": _compact_backtest_job(row),
                "next_steps": [
                    "Inspect the resulting run with get_run_debug_view.",
                    "Use suggest_strategy_iteration to decide whether to change parameters, binding, or definition logic.",
                    "Prefer parameter updates first when the final target produced no allocations.",
                ],
            }
            if auto_created_task_id is not None:
                payload["auto_created_task_id"] = auto_created_task_id
            run_id_str = row.get("run_id") if isinstance(row, dict) else None
            text = (
                f"Backtest queued (fire-and-forget): run_id={run_id_str}. "
                "Use get_run_debug_view to inspect when ready."
                if run_id_str
                else "Backtest queued (fire-and-forget). Use get_run_debug_view to inspect when ready."
            )
            return ToolResult(text=append_json_payload(text, payload))

        terminal, run = await self._poll_until_terminal_or_timeout(
            task_id, run_id, timeout_seconds, poll_interval_seconds
        )
        if terminal:
            terminal_status = str(run.get("status") or "").strip().lower()
            if terminal_status == "failed":
                payload = self._failed_run_payload(
                    task_id, run_id, run, [run_id]
                )
                # fresh-failed runs don't carry attached_to_existing_run
                payload.pop("attached_to_existing_run", None)
                if auto_created_task_id is not None:
                    payload["auto_created_task_id"] = auto_created_task_id
                error_prose = format_error_text(
                    "backtest_run_failed",
                    str(payload.get("error") or "backtest run failed"),
                    "; ".join(payload.get("repair_hints") or []),
                )
                return ToolResult(
                    text=append_json_payload(error_prose, payload),
                    is_error=True,
                )
            # Build payload with headline-first key ordering: status →
            # report_path (added by _attach_summary_if_ready) → next_steps →
            # auto_created_task_id → backtest_job (compact). Python dicts
            # preserve insertion order, so the bash-tool truncation can't
            # bury ``report_path`` behind ``backtest_job``.
            payload: dict[str, Any] = {"status": "ok"}
            markdown, _summary_dict = await self._attach_summary_if_ready(payload, run_id)
            if auto_created_task_id is not None:
                payload["auto_created_task_id"] = auto_created_task_id
            payload["backtest_job"] = _compact_backtest_job(run)
            report_path = payload.get("report_path") if isinstance(payload, dict) else None
            if isinstance(report_path, str) and report_path:
                text = f"Backtest completed. Read {report_path} for the full report."
            elif markdown:
                text = "Backtest completed.\n\n" + markdown
            else:
                text = "Backtest completed."
            return ToolResult(text=append_json_payload(text, payload))
        payload = self._timeout_payload(task_id, run_id, run, attached=False)
        if auto_created_task_id is not None:
            payload["auto_created_task_id"] = auto_created_task_id
        timeout_text = format_error_text(
            "backtest_wait_timeout",
            str(payload.get("error") or "backtest wait timed out"),
            "; ".join(payload.get("repair_hints") or []),
        )
        return ToolResult(
            text=append_json_payload(timeout_text, payload),
            is_error=True,
        )

    # ------------------------------------------------------------------
    # Polling / attach helpers (absorbed from the retired BacktestTool).
    # ------------------------------------------------------------------

    async def _attach_summary_if_ready(
        self,
        payload: dict[str, Any],
        run_id: str,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Best-effort: load the persisted backtest summary for ``run_id`` and
        render it as markdown for the user-facing prose. Mutates ``payload``
        in place to add ``next_steps`` pointers when the summary is stale or
        missing; returns ``(markdown, summary_dict)`` so callers can build a
        KPI prefix line without double-fetching.

        The dense ``backtest_summary`` JSON is **not** inlined into the
        payload — the markdown carries every KPI a user reads, and the
        front-end still fetches the full record (incl. ``equity_curve``)
        via ``GET /tasks/{task_id}`` for chart rendering. Agents that need
        raw fields call ``get_backtest_summary`` explicitly.

        Never raises — failures fall through to the pointer hint so the OK
        path stays OK.
        """

        getter = getattr(self._platform_service, "get_backtest_summary", None)
        if getter is None:
            return None, None
        try:
            result = await getter(run_id)
        except Exception:
            result = None
        summary: dict[str, Any] | None = None
        latest_summary_run_id: str | None = None
        if isinstance(result, dict):
            state = result.get("summary_state")
            if state == "ok" and isinstance(result.get("summary"), dict):
                summary = dict(result["summary"])
            elif state == "stale":
                raw = result.get("latest_summary_run_id")
                if isinstance(raw, str) and raw:
                    latest_summary_run_id = raw

        if summary is not None:
            markdown = render_summary_markdown(summary)
            report_path = await _write_backtest_report_to_disk(
                run_id=run_id,
                markdown=markdown,
            )
            # Only advertise ``report_path`` when the write actually
            # succeeded — otherwise the agent must keep reading the inline
            # markdown (which the caller falls back to including in
            # ``ToolResult.text`` when ``report_path`` is absent).
            if report_path is not None:
                payload["report_path"] = report_path
            existing = payload.get("next_steps")
            if isinstance(existing, list):
                existing.append(_BACKTEST_NEXT_STEP_HINT)
            else:
                payload["next_steps"] = [_BACKTEST_NEXT_STEP_HINT]
            return markdown, summary

        hint = (
            f"call get_backtest_summary(run_id={run_id!r}) to read the "
            "persisted summary fields once the run finalizes."
        )
        if latest_summary_run_id:
            hint = (
                f"a newer run on this task has overwritten the persisted "
                f"summary; call get_backtest_summary(run_id="
                f"{latest_summary_run_id!r}) to read the current summary."
            )
        existing = payload.get("next_steps")
        if isinstance(existing, list):
            existing.append(hint)
        else:
            payload["next_steps"] = [hint]
        return None, None

    async def _poll_until_terminal_or_timeout(
        self,
        task_id: str,
        run_id: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> tuple[bool, dict[str, Any]]:
        assert self._platform_service is not None
        deadline = asyncio.get_running_loop().time() + max(float(timeout_seconds), 0.0)
        sleep_seconds = max(float(poll_interval_seconds), 0.0)
        run: dict[str, Any] = {}
        while True:
            run = await self._platform_service.get_backtest_job(task_id, run_id)
            status = str(run.get("status") or "").strip().lower()
            if status in TERMINAL_BACKTEST_STATUSES:
                return True, run
            if asyncio.get_running_loop().time() >= deadline:
                return False, run
            await asyncio.sleep(sleep_seconds)

    def _timeout_payload(
        self,
        task_id: str,
        run_id: str,
        run: dict[str, Any],
        *,
        attached: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "error",
            "tool": self.name,
            "error_type": "BacktestTimeout",
            "error_code": "backtest_wait_timeout",
            "error": f"backtest job did not finish in time: {run_id}",
            "backtest_job": _compact_backtest_job(run, fields=_BACKTEST_JOB_PROGRESS_FIELDS),
            "repair_hints": [
                (
                    f"the run is still executing; call run_strategy_backtest "
                    f"again with task_id={task_id!r} (same range_start / "
                    "range_end) to resume waiting on this existing run."
                ),
                (
                    f"or poll directly with get_cycle_run(run_id={run_id!r}) / "
                    "get_run_debug_view without restarting."
                ),
                "raise timeout_seconds for the next call if bars_completed/"
                "bars_total shows steady progress.",
            ],
        }
        if attached:
            payload["attached_to_existing_run"] = True
        return payload

    async def _handle_existing_run(
        self,
        *,
        task_id: str,
        auto_created_task_id: str | None,
        start_exception_metadata: dict[str, Any],
        start_exception_message: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> ToolResult:
        assert self._platform_service is not None
        existing = await _safe_lookup_existing_runs(self._platform_service, task_id)
        run_ids = [str(entry["run_id"]) for entry in existing if entry.get("run_id")]

        latest = existing[0] if existing else None
        existing_run_id = (
            str(latest.get("run_id") or "") if isinstance(latest, dict) else ""
        )
        existing_status = (
            str(latest.get("status") or "").strip().lower() if isinstance(latest, dict) else ""
        )

        if existing_run_id and existing_status in _ATTACHABLE_BACKTEST_STATUSES:
            if timeout_seconds <= 0:
                # Fire-and-forget contract: don't poll, just report the
                # attach. Headline-first ordering (status / attached flag
                # before the bulky job dict).
                payload: dict[str, Any] = {
                    "status": "ok",
                    "attached_to_existing_run": True,
                }
                if auto_created_task_id is not None:
                    payload["auto_created_task_id"] = auto_created_task_id
                payload["backtest_job"] = (
                    _compact_backtest_job(dict(latest)) if isinstance(latest, dict) else {}
                )
                latest_run_id = payload["backtest_job"].get("run_id")
                text = (
                    f"Attached to existing backtest run: run_id={latest_run_id}."
                    if latest_run_id
                    else "Attached to existing backtest run."
                )
                return ToolResult(text=append_json_payload(text, payload))
            terminal, run = await self._poll_until_terminal_or_timeout(
                task_id, existing_run_id, timeout_seconds, poll_interval_seconds
            )
            if terminal:
                terminal_status = str(run.get("status") or "").strip().lower()
                if terminal_status == "failed":
                    payload = self._failed_run_payload(task_id, existing_run_id, run, run_ids)
                    if auto_created_task_id is not None:
                        payload["auto_created_task_id"] = auto_created_task_id
                    failed_text = format_error_text(
                        "backtest_run_failed",
                        str(payload.get("error") or "backtest run failed"),
                        "; ".join(payload.get("repair_hints") or []),
                    )
                    return ToolResult(
                        text=append_json_payload(failed_text, payload),
                        is_error=True,
                    )
                # Headline-first key ordering — see the fresh-run branch
                # for rationale. ``attached_to_existing_run`` lands at the
                # head too because the agent needs to know it's not a new
                # run before reading any KPI.
                payload: dict[str, Any] = {
                    "status": "ok",
                    "attached_to_existing_run": True,
                }
                markdown, _summary_dict = await self._attach_summary_if_ready(
                    payload, existing_run_id
                )
                if auto_created_task_id is not None:
                    payload["auto_created_task_id"] = auto_created_task_id
                payload["backtest_job"] = _compact_backtest_job(run)
                report_path = payload.get("report_path") if isinstance(payload, dict) else None
                if isinstance(report_path, str) and report_path:
                    text = (
                        f"Attached to existing backtest run; reached terminal. "
                        f"Read {report_path} for the full report."
                    )
                elif markdown:
                    text = "Attached to existing backtest run; reached terminal.\n\n" + markdown
                else:
                    text = "Attached to existing backtest run; reached terminal status."
                return ToolResult(text=append_json_payload(text, payload))
            payload = self._timeout_payload(task_id, existing_run_id, run, attached=True)
            if auto_created_task_id is not None:
                payload["auto_created_task_id"] = auto_created_task_id
            timeout_text = format_error_text(
                "backtest_wait_timeout",
                str(payload.get("error") or "backtest wait timed out"),
                "; ".join(payload.get("repair_hints") or []),
            )
            return ToolResult(
                text=append_json_payload(timeout_text, payload),
                is_error=True,
            )

        if existing_run_id and existing_status in {"completed", "finished"}:
            try:
                run = await self._platform_service.get_backtest_job(task_id, existing_run_id)
            except Exception:
                run = dict(latest) if isinstance(latest, dict) else {}
            # Headline-first key ordering — see the fresh-run branch
            # for rationale.
            payload: dict[str, Any] = {
                "status": "ok",
                "attached_to_existing_run": True,
            }
            markdown, _summary_dict = await self._attach_summary_if_ready(
                payload, existing_run_id
            )
            if auto_created_task_id is not None:
                payload["auto_created_task_id"] = auto_created_task_id
            payload["backtest_job"] = _compact_backtest_job(run)
            report_path = payload.get("report_path") if isinstance(payload, dict) else None
            if isinstance(report_path, str) and report_path:
                text = (
                    f"Attached to already-completed backtest run. "
                    f"Read {report_path} for the full report."
                )
            elif markdown:
                text = "Attached to already-completed backtest run.\n\n" + markdown
            else:
                text = "Attached to already-completed backtest run."
            return ToolResult(text=append_json_payload(text, payload))

        if existing_run_id and existing_status == "failed":
            try:
                run = await self._platform_service.get_backtest_job(task_id, existing_run_id)
            except Exception:
                run = dict(latest) if isinstance(latest, dict) else {}
            payload = self._failed_run_payload(task_id, existing_run_id, run, run_ids)
            if auto_created_task_id is not None:
                payload["auto_created_task_id"] = auto_created_task_id
            failed_text = format_error_text(
                "backtest_run_failed",
                str(payload.get("error") or "backtest run failed"),
                "; ".join(payload.get("repair_hints") or []),
            )
            return ToolResult(
                text=append_json_payload(failed_text, payload),
                is_error=True,
            )

        payload = {
            "status": "error",
            "tool": self.name,
            "error_code": "backtest_run_already_exists",
            "error_type": start_exception_metadata["error_type"],
            "error": start_exception_message,
            "repair_hints": [
                (
                    "the platform reports an existing run for this task but "
                    "cannot classify it; inspect with get_run_debug_view or "
                    "list_cycle_runs before retrying."
                ),
                (
                    f"if the existing run is unrecoverable, call clone_task "
                    f"source_identifier={task_id!r}, then run_strategy_backtest "
                    "against the cloned task_id."
                ),
            ],
        }
        if run_ids:
            payload["existing_run_ids"] = run_ids
        if auto_created_task_id is not None:
            payload["auto_created_task_id"] = auto_created_task_id
        already_exists_text = format_error_text(
            "backtest_run_already_exists",
            str(start_exception_message or "backtest run already exists"),
            "; ".join(payload["repair_hints"]),
        )
        return ToolResult(
            text=append_json_payload(already_exists_text, payload),
            is_error=True,
        )

    def _failed_run_payload(
        self,
        task_id: str,
        run_id: str,
        run: dict[str, Any],
        run_ids: list[str],
    ) -> dict[str, Any]:
        err_msg = (
            str(run.get("error_message") or "").strip()
            or "no error message reported by the platform"
        )
        payload: dict[str, Any] = {
            "status": "error",
            "tool": self.name,
            "error_code": "backtest_run_failed",
            "error_type": "BacktestRunFailed",
            "error": f"existing backtest run {run_id} failed: {err_msg}",
            "backtest_job": _compact_backtest_job(run, fields=_BACKTEST_JOB_FAILURE_FIELDS),
            "attached_to_existing_run": True,
            "existing_run_status": "failed",
            "existing_run_id": run_id,
            "repair_hints": [
                (
                    f"inspect the failure first with get_run_debug_view(run_id={run_id!r}) "
                    "or get_cycle_run — do not blindly retry."
                ),
                (
                    "fix the underlying cause (strategy code, data provider config, "
                    "universe, etc.) before re-running."
                ),
                (
                    f"only after the cause is addressed, call clone_task "
                    f"source_identifier={task_id!r} then run_strategy_backtest "
                    "against the cloned task_id."
                ),
            ],
        }
        if run_ids:
            payload["existing_run_ids"] = run_ids
        return payload


class InspectStrategyResourcesTool(OperationHandler):
    name = "inspect_strategy_resources"
    description = (
        "Inspect strategy definitions. Definitions sharing the "
        "same source-code fingerprint (``code_hash``) are grouped under "
        "``duplicate_definition_groups`` so the agent can reuse an existing "
        "definition instead of creating another copy. Pass ``query`` "
        "to fuzzy-search by keyword (case-insensitive; whitespace-separated "
        "tokens are AND-matched) across definition "
        "definition_id/name/generation_prompt. "
        "Matched rows surface a ``match_reasons`` field naming the fields that "
        "matched."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Case-insensitive fuzzy keyword search. Whitespace-separated "
                    "tokens are AND-matched (every token must appear in at least "
                    "one searched field). Omit or pass an empty string to list "
                    "everything."
                ),
            },
        },
    }

    def __init__(
        self,
        definition_repository: SqlAlchemyStrategyDefinitionRepository | None,
    ):
        self._definition_repository = definition_repository

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            if contract.error_kind == "unknown_arguments":
                text = format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                )
            else:
                text = format_error_text(
                    "validation_error",
                    str(
                        contract.error.get("message")
                        or contract.error.get("error")
                        or "validation failed"
                    ),
                )
            return ToolResult(
                text=text,
                is_error=True,
            )
        kwargs = contract.kwargs

        raw_query = kwargs.get("query")
        if raw_query is not None and not isinstance(raw_query, str):
            return ToolResult(
                text=format_error_text("validation_error", "query must be a string"),
                is_error=True,
            )
        tokens = [token for token in (raw_query or "").lower().split() if token]

        if self._definition_repository is None:
            return ToolResult(
                text=format_error_text(
                    "repositories_unavailable",
                    "strategy definition repository is not available",
                ),
                is_error=True,
            )
        definitions = await self._definition_repository.list_definitions()
        definition_rows = [
            {
                "definition_id": item.definition_id,
                "name": item.name,
                "status": item.status,
                "code_hash": item.code_hash,
                "generation_prompt": item.generation_prompt,
                "created_at": item.created_at,
            }
            for item in definitions
        ]
        payload = build_strategy_inspect_payload(definition_rows, query=raw_query)

        duplicate_groups = payload.get("duplicate_definition_groups") or []
        definitions_payload = payload.get("definitions") or []
        lines = [
            f"Found {len(definitions_payload)} definition(s)."
        ]
        if tokens:
            lines[0] = (
                f"Query {raw_query!r} matched {len(definitions_payload)} of "
                f"{len(definitions)} definition(s)."
            )
        if duplicate_groups:
            lines.append(
                f"{len(duplicate_groups)} duplicate-definition group(s) detected — "
                "prefer recommended_reuse_id before creating new copies."
            )
        return ToolResult(text=append_json_payload("\n".join(lines), payload))


class SuggestStrategyIterationTool(OperationHandler):
    name = "suggest_strategy_iteration"
    description = (
        "Inspect a run's debug view and recommend the next iteration step. "
        "On success returns ``status: ok`` with the resolved ``run_id`` "
        "and ``suggestion`` (``action_type``, ``reason``, "
        "``recommended_tools``). ``action_type`` is one of: "
        "``definition_change`` (definition risks detected or trace shows "
        "code-level issues), ``parameter_only`` (final target produced "
        "no allocations — tune the task's parameter_overrides before "
        "rewriting code), ``binding_change`` (run produced no useful "
        "spans/model invocations — verify binding/task config first). Use "
        "after a backtest to decide which of update_task / "
        "update_strategy_definition / bind_strategy_definition_to_task to "
        "call next."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "run_id": {
                "type": "string",
                "description": (
                    "Run identifier — accepts cycle run id, backtest job "
                    "id (``btjob-...``), or debug session id "
                    "(``backtest-...`` / ``debug-...``). Same resolution "
                    "rules as ``get_run_debug_view``."
                ),
            },
        },
        "required": ["run_id"],
    }

    def __init__(self, platform_service: Any | None):
        self._platform_service = platform_service

    async def execute(self, run_id: str) -> ToolResult:
        if self._platform_service is None or not hasattr(self._platform_service, "get_run_debug_view"):
            return ToolResult(
                text=format_error_text(
                    "service_unavailable",
                    "platform service is not available",
                ),
                is_error=True,
            )
        debug_view = await self._platform_service.get_run_debug_view(run_id)
        cycle_run = debug_view.get("cycle_run") if isinstance(debug_view, dict) else {}
        details = cycle_run.get("details") if isinstance(cycle_run, dict) else {}
        strategy_trace = details.get("strategy_trace") if isinstance(details, dict) else {}
        final_target_summary = (
            strategy_trace.get("final_target_summary") if isinstance(strategy_trace, dict) else {}
        )
        definition_risks = details.get("definition_risks") if isinstance(details, dict) else []
        allocation_count = 0
        if isinstance(final_target_summary, dict):
            try:
                allocation_count = int(final_target_summary.get("allocation_count") or 0)
            except (TypeError, ValueError):
                allocation_count = 0

        spans = debug_view.get("spans") if isinstance(debug_view, dict) else []
        model_invocations = debug_view.get("model_invocations") if isinstance(debug_view, dict) else []

        if isinstance(definition_risks, list) and definition_risks:
            suggestion = {
                "action_type": "definition_change",
                "reason": "definition risk detected in run evidence; fix strategy code or runtime assumptions before tuning parameters",
                "recommended_tools": ["get_strategy_definition", "update_strategy_definition", "run_strategy_backtest"],
            }
        elif allocation_count <= 0:
            suggestion = {
                "action_type": "parameter_only",
                "reason": "final target produced no allocations; tune the task's parameter_overrides (thresholds or lookback) before rewriting code",
                "recommended_tools": ["get_task", "update_task", "run_strategy_backtest"],
            }
        elif not spans and not model_invocations:
            suggestion = {
                "action_type": "binding_change",
                "reason": "run lacks useful execution trace artifacts; verify definition binding and task configuration",
                "recommended_tools": ["get_task", "bind_strategy_definition_to_task", "run_strategy_backtest"],
            }
        else:
            suggestion = {
                "action_type": "definition_change",
                "reason": "runtime executed and produced allocations; refine strategy logic or diagnostics in the definition",
                "recommended_tools": ["get_strategy_definition", "update_strategy_definition", "run_strategy_backtest"],
            }

        data = {"status": "ok", "run_id": run_id, "suggestion": suggestion}
        text = (
            f"Run {run_id}: next action = {suggestion['action_type']}. "
            f"{suggestion['reason']}"
        )
        return ToolResult(text=append_json_payload(text, data))
