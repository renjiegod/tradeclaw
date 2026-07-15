from __future__ import annotations

from typing import Any

from doyoutrade.tools import (
    OperationHandler,
    ToolResult,
    tool_result_from_error_dict,
)
from doyoutrade.tools._identifier_kinds import IdentifierGuard, IdentifierKind
from doyoutrade.tools._prose import (
    append_json_payload,
    format_error_text,
    format_unknown_args,
)
from doyoutrade.assistant.strategy_tools._helpers import build_strategy_authoring_contract
from doyoutrade.backtest.summary import render_summary_markdown, summary_for_agent_view
from doyoutrade.debug import emit_debug_event
from doyoutrade.persistence import (
    SqlAlchemyStrategyDefinitionRepository,
)
from doyoutrade.persistence.errors import RecordNotFoundError
from doyoutrade.strategy_registry.validation import InvalidStrategyDefinitionError
from doyoutrade.strategy_registry import StrategyRegistryService


class GetStrategyDefinitionTool(OperationHandler):
    name = "get_strategy_definition"
    description = (
        "Fetch a strategy definition's metadata. On success returns "
        "``status: ok`` with ``definition`` "
        "(``current_version``, ``parameter_schema``, ``capabilities``, "
        "``code_hash``, ``status``, ...) and ``recommended_next_steps``. "
        "Source code lives on disk — use the authoring lifecycle tools to "
        "read or edit it. "
        "Known ``error_code``: ``wrong_identifier_type`` when "
        "``definition_id`` is not ``sd-...``."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "definition_id": {
                "type": "string",
                "description": "Strategy definition id, shaped ``sd-...``.",
            },
        },
        "required": ["definition_id"],
    }

    identifier_guards = (
        IdentifierGuard(field="definition_id", kind=IdentifierKind.DEFINITION_ID),
    )

    def __init__(self, repository: SqlAlchemyStrategyDefinitionRepository | None):
        self._repository = repository

    async def execute(self, definition_id: str) -> ToolResult:
        guard = self._apply_identifier_guards({"definition_id": definition_id})
        if guard is not None:
            return tool_result_from_error_dict(guard)
        if self._repository is None:
            return ToolResult(
                text=format_error_text(
                    "service_unavailable",
                    "strategy definition repository is not available",
                ),
                is_error=True,
            )
        snapshot = await self._repository.get_definition(definition_id)
        definition = {
            "definition_id": snapshot.definition_id,
            "name": snapshot.name,
            "current_version": snapshot.current_version,
            "parameter_schema": snapshot.parameter_schema_json,
            "capabilities": snapshot.capabilities_json,
            "code_hash": snapshot.code_hash,
            "status": snapshot.status,
        }
        text = (
            f"Strategy definition {snapshot.definition_id} "
            f"({snapshot.name!r}, current_version={snapshot.current_version}, "
            f"status={snapshot.status})."
        )
        payload = {
            "status": "ok",
            "definition": definition,
            "recommended_next_steps": [
                "Use open_strategy_authoring to start an edit session if you need to change the strategy code.",
                "Use compile_strategy_draft after editing to validate and smoke-test the draft.",
                "Use finalize_strategy_authoring to promote a validated draft to a named version.",
            ],
        }
        return ToolResult(text=append_json_payload(text, payload))


class UpdateStrategyDefinitionTool(OperationHandler):
    name = "update_strategy_definition"
    description = (
        "Update an existing strategy definition's metadata. Patch "
        "semantics: only the fields you supply are written; omit any "
        "field to leave its current value untouched. "
        "Source code changes must go through the authoring lifecycle "
        "(``open_strategy_authoring`` → ``edit/write_strategy_file`` → "
        "``compile_strategy_draft`` → ``finalize_strategy_authoring``). "
        "On success returns ``status: ok`` with the updated "
        "``definition`` and ``next_steps``. "
        "Known ``error_code`` values: ``wrong_identifier_type`` "
        "(``definition_id`` is not ``sd-...``)."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "definition_id": {
                "type": "string",
                "description": "Target definition id, shaped ``sd-...``.",
            },
            "name": {
                "type": "string",
                "description": "Replacement display name; omit to keep.",
            },
            "parameter_schema": {
                "type": "object",
                "description": (
                    "Replacement parameter schema (JSON object mapping "
                    "param name → type/default spec)."
                ),
            },
            "capabilities": {
                "type": "object",
                "description": (
                    "Replacement capabilities/constraints object."
                ),
            },
            "default_parameters": {
                "type": "object",
                "description": (
                    "Replacement default parameter values applied when an "
                    "instance does not override a key."
                ),
            },
            "provenance": {
                "type": "object",
                "description": (
                    "Replacement provenance metadata (e.g. source, "
                    "author)."
                ),
            },
            "status": {
                "type": "string",
                "description": (
                    "New lifecycle status (e.g. ``draft`` / ``ready`` / "
                    "``deprecated``). Omit to keep current."
                ),
            },
        },
        "required": ["definition_id"],
    }

    identifier_guards = (
        IdentifierGuard(field="definition_id", kind=IdentifierKind.DEFINITION_ID),
    )

    def __init__(
        self,
        registry_service: StrategyRegistryService | None,
    ):
        self._registry_service = registry_service

    async def execute(self, definition_id: str, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract({"definition_id": definition_id, **kwargs})
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
            return ToolResult(text=text, is_error=True)
        kwargs = {k: v for k, v in contract.kwargs.items() if k != "definition_id"}
        guard = self._apply_identifier_guards({"definition_id": definition_id})
        if guard is not None:
            return tool_result_from_error_dict(guard)
        if self._registry_service is None:
            return ToolResult(
                text=format_error_text(
                    "service_unavailable",
                    "strategy registry service is not available",
                ),
                is_error=True,
            )

        snapshot = await self._registry_service.update_definition(definition_id, **kwargs)
        text = (
            f"Updated strategy definition {snapshot.definition_id} "
            f"({snapshot.name!r}, current_version={snapshot.current_version}, "
            f"status={snapshot.status})."
        )
        payload = {
            "status": "ok",
            "definition": {
                "definition_id": snapshot.definition_id,
                "name": snapshot.name,
                "current_version": snapshot.current_version,
                "parameter_schema": snapshot.parameter_schema_json,
                "capabilities": snapshot.capabilities_json,
                "code_hash": snapshot.code_hash,
                "status": snapshot.status,
            },
            "next_steps": [
                "If runnable context exists, run strategy backtest next.",
                "Inspect get_run_debug_view after the backtest.",
                "Use suggest_strategy_iteration before deciding whether to adjust parameters or logic.",
            ],
        }
        return ToolResult(text=append_json_payload(text, payload))


class GetRunDebugViewTool(OperationHandler):
    name = "get_run_debug_view"
    description = (
        "Fetch the debug view for a strategy run. Accepts a cycle run id, "
        "backtest job id, or debug session id — the platform resolves "
        "across all three and returns the same payload shape. On success "
        "returns ``status: ok`` with ``debug_view`` containing — in this "
        "order — ``signal_timeline_summary`` (compact, top-of-payload), "
        "``resolved_from``, ``backtest_job``, ``session``, ``cycle_run`` / "
        "``cycle_runs``, ``signal_timeline``, ``spans``, "
        "``model_invocations``. The summary is placed first so it "
        "survives any tool-result truncation — zero-trade diagnosis "
        "starts there, NOT by drilling into ``cycle_runs[i]`` (which "
        "carries no signal info; ``signal_generation`` on the cycle row "
        "is reserved for future per-symbol decision detail and is empty "
        "today).\n"
        "Read order for zero-trade backtests:\n"
        "  1. ``debug_view.signal_timeline_summary`` — counts + "
        "``top_hold_tags`` / ``top_buy_tags`` answer 'what dominated' in "
        "one glance (e.g. ``{'warmup': 19}`` → MACD never warmed up; "
        "``{'no_cross': 19}`` → MACD computed but no golden/dead cross).\n"
        "  2. ``debug_view.signal_timeline`` (top-level, NOT inside "
        "``cycle_runs``) — one row per cycle with ``run_id`` / "
        "``cycle_time`` / ``signals_buy/sell/hold`` / ``per_symbol_tags``. "
        "Use to trace specific bars when the summary shows mixed tags.\n"
        "Both come from the same code path the strategy ran — no need to "
        "reimplement indicators locally. Empty list/zeroed summary means "
        "the strategy ran no cycles; missing keys mean an older backend "
        "payload (treat as empty).\n"
        "Note: an untagged ``Signal.hold()`` collapses to "
        "``<untagged_hold>`` in ``per_symbol_tags`` — fix by tagging the "
        "hold branch in the strategy source (``Signal.hold(tag='...')``) "
        "and re-running.\n"
        "Use this before deciding whether to call "
        "``suggest_strategy_iteration``."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "run_id": {
                "type": "string",
                "description": (
                    "Run identifier — accepts a cycle run id, a backtest "
                    "job id (``btjob-...``), or a debug session id "
                    "(``backtest-...`` / ``debug-...``)."
                ),
            },
            "summary_only": {
                "type": "boolean",
                "description": (
                    "When true, attach a compact ``summary`` block and "
                    "still return the trimmed payload. Default false."
                ),
            },
            "include_spans": {
                "type": "boolean",
                "description": (
                    "When false, drop the ``spans`` array from the "
                    "response (useful to keep payload small). Default "
                    "true."
                ),
            },
            "include_model_invocations": {
                "type": "boolean",
                "description": (
                    "When false, drop ``model_invocations``. Default "
                    "true."
                ),
            },
            "include_cycle_runs_limit": {
                "type": "integer",
                "description": (
                    "If set (>= 0), truncate ``cycle_runs`` to at most "
                    "this many entries. Omit to return all cycle runs."
                ),
            },
        },
        "required": ["run_id"],
    }

    def __init__(self, platform_service: Any | None):
        self._platform_service = platform_service

    async def execute(
        self,
        run_id: str,
        summary_only: bool = False,
        include_spans: bool = True,
        include_model_invocations: bool = True,
        include_cycle_runs_limit: int | None = None,
    ) -> ToolResult:
        if self._platform_service is None:
            return ToolResult(
                text=format_error_text(
                    "service_unavailable", "platform service is not available"
                ),
                is_error=True,
            )
        getter = getattr(self._platform_service, "get_run_debug_view", None)
        if getter is None:
            return ToolResult(
                text=format_error_text(
                    "service_unavailable", "platform service is not available"
                ),
                is_error=True,
            )
        payload = await getter(run_id)
        if isinstance(payload, dict):
            payload = dict(payload)
            cycle_runs = payload.get("cycle_runs")
            if isinstance(cycle_runs, list) and isinstance(include_cycle_runs_limit, int) and include_cycle_runs_limit >= 0:
                payload["cycle_runs"] = cycle_runs[:include_cycle_runs_limit]
            if not include_spans:
                payload.pop("spans", None)
            if not include_model_invocations:
                payload.pop("model_invocations", None)
            if summary_only:
                payload["summary"] = {
                    "cycle_run_status": (
                        payload.get("cycle_run", {}).get("status")
                        if isinstance(payload.get("cycle_run"), dict)
                        else None
                    ),
                    "cycle_run_count": len(payload.get("cycle_runs") or []),
                    "has_spans": bool(payload.get("spans")),
                    "has_model_invocations": bool(payload.get("model_invocations")),
                    "resolved_from": payload.get("resolved_from"),
                }
        # Build a header with key counts so the model can skim before
        # reading the embedded JSON block.
        cycle_run_id: Any = None
        cycle_run_status: Any = None
        resolved_kind: Any = None
        spans_count = 0
        model_invocations_count = 0
        cycle_runs_count = 0
        if isinstance(payload, dict):
            cycle_run = payload.get("cycle_run")
            if isinstance(cycle_run, dict):
                cycle_run_id = cycle_run.get("run_id")
                cycle_run_status = cycle_run.get("status")
            resolved_from = payload.get("resolved_from")
            if isinstance(resolved_from, dict):
                resolved_kind = resolved_from.get("identifier_type")
            spans = payload.get("spans")
            if isinstance(spans, list):
                spans_count = len(spans)
            mis = payload.get("model_invocations")
            if isinstance(mis, list):
                model_invocations_count = len(mis)
            cruns = payload.get("cycle_runs")
            if isinstance(cruns, list):
                cycle_runs_count = len(cruns)
        header_first = (
            f"Debug view for run_id={run_id}"
            f"{f' (resolved as {resolved_kind})' if resolved_kind else ''}."
        )
        lines = [header_first]
        if cycle_run_id is not None:
            lines.append(
                f"- cycle_run {cycle_run_id} [{cycle_run_status or 'unknown'}]"
            )
        lines.append(
            f"- {cycle_runs_count} cycle_run(s); "
            f"{spans_count} span(s); "
            f"{model_invocations_count} model_invocation(s)"
        )
        text = "\n".join(lines)
        return ToolResult(
            text=append_json_payload(text, {"status": "ok", "debug_view": payload}),
        )


class GetBacktestSummaryTool(OperationHandler):
    name = "get_backtest_summary"
    description = (
        "Fetch a finished backtest's persisted summary. ``run_strategy_backtest`` "
        "already returns a markdown report in its happy path; use this tool to "
        "re-fetch the report for an earlier run, read the raw JSON fields for "
        "programmatic analysis, or recover from a stale summary.\n"
        "``format='markdown'`` (default) returns the same markdown body the "
        "agent forwards to the user. ``format='json'`` returns the dense field "
        "pack (``starting_equity``, ``ending_equity``, ``return_pct``, "
        "``max_drawdown_pct``, ``win_rate``, ``trade_count_closed`` / "
        "``trade_count_open`` / ``fills_count``, ``avg_holding_trading_days``, "
        "``final_positions``, ...) plus the ``run`` header (status, range, "
        "starting/ending equity, error_message); the bulky ``equity_curve`` "
        "array is stripped — front-ends fetch it via ``GET /tasks/{task_id}``.\n"
        "Known ``error_code`` values: "
        "``backtest_summary_not_found`` (no run row for the given "
        "``run_id``); "
        "``backtest_summary_not_ready`` (the run exists but no summary "
        "has been persisted yet — the run is in flight, was aborted "
        "before finalize, or the compute step raised; payload carries "
        "the ``run`` header so the caller can inspect ``status`` and "
        "``error_message``); "
        "``backtest_summary_stale`` (a newer backtest on the same task "
        "overwrote the persisted summary; payload carries "
        "``latest_summary_run_id`` so the caller can refresh with "
        "``get_backtest_summary(latest_summary_run_id)`` or rerun)."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "run_id": {
                "type": "string",
                "description": (
                    "Backtest job run_id (the ``run_id`` returned by "
                    "``run_strategy_backtest`` inside ``backtest_job``)."
                ),
            },
            "format": {
                "type": "string",
                "enum": ["markdown", "json"],
                "description": (
                    "Output shape. ``markdown`` (default) returns the "
                    "human-facing report — same body emitted by "
                    "``run_strategy_backtest`` — for forwarding to the user. "
                    "``json`` returns the dense field pack for programmatic "
                    "inspection or stale-summary recovery."
                ),
            },
        },
        "required": ["run_id"],
    }

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
            return ToolResult(text=text, is_error=True)
        kwargs = contract.kwargs

        run_id = kwargs.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "validation_error"},
            )
            return ToolResult(
                text=format_error_text("validation_error", "run_id is required"),
                is_error=True,
            )
        run_id = run_id.strip()

        fmt_raw = kwargs.get("format", "markdown")
        if not isinstance(fmt_raw, str) or fmt_raw not in {"markdown", "json"}:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "validation_error"},
            )
            return ToolResult(
                text=format_error_text(
                    "validation_error",
                    "format must be 'markdown' or 'json'",
                ),
                is_error=True,
            )
        fmt = fmt_raw

        if self._platform_service is None or not hasattr(
            self._platform_service, "get_backtest_summary"
        ):
            return ToolResult(
                text=format_error_text(
                    "ServiceUnavailable",
                    "doyoutrade-cli backtest summary service is not available",
                ),
                is_error=True,
            )

        try:
            result = await self._platform_service.get_backtest_summary(run_id)
        except RecordNotFoundError as exc:
            repair_hint = (
                "verify the run_id with list_cycle_runs(identifier=<task>) "
                "or pass the run_id reported by run_strategy_backtest."
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "backtest_summary_not_found"},
            )
            return ToolResult(
                text=format_error_text(
                    "backtest_summary_not_found",
                    str(exc),
                    repair_hint,
                ),
                is_error=True,
            )

        run_header = result.get("run") if isinstance(result, dict) else None
        summary = result.get("summary") if isinstance(result, dict) else None
        state = result.get("summary_state") if isinstance(result, dict) else None
        latest_summary_run_id = (
            result.get("latest_summary_run_id") if isinstance(result, dict) else None
        )

        if state == "ok" and isinstance(summary, dict):
            await emit_debug_event(
                f"operation_{self.name}.validated",
                {**base_payload, "run_id": run_id, "format": fmt},
            )
            run_status = (
                str(run_header.get("status") or "")
                if isinstance(run_header, dict)
                else ""
            )
            return_pct = summary.get("return_pct")
            kpi_line = (
                f"Backtest summary for run_id={run_id} (status={run_status!r}); "
                f"return_pct={return_pct}."
            )
            if fmt == "markdown":
                # Mirror run_strategy_backtest's happy path: render the
                # markdown into the prose head, keep the JSON payload thin
                # (run header only) so the markdown survives any per-agent
                # truncation.
                markdown = render_summary_markdown(summary)
                payload = {
                    "status": "ok",
                    "run_id": run_id,
                    "run": run_header,
                }
                text = (
                    f"{kpi_line}\n\n{markdown}" if markdown else kpi_line
                )
                return ToolResult(text=append_json_payload(text, payload))
            # ``summary_for_agent_view`` strips ``equity_curve`` so the
            # tool result fits under the per-agent
            # ``tool_result_max_chars`` budget — the full curve is still
            # available to front-ends via ``GET /tasks/{task_id}``.
            payload = {
                "status": "ok",
                "run_id": run_id,
                "run": run_header,
                "backtest_summary": summary_for_agent_view(summary),
            }
            return ToolResult(text=append_json_payload(kpi_line, payload))

        if state == "missing":
            run_status = (
                str(run_header.get("status") or "")
                if isinstance(run_header, dict)
                else ""
            )
            message = (
                f"no persisted summary for run_id={run_id!r} "
                f"(run status={run_status!r}). Either the run is still "
                "in flight, was aborted before finalize, or the "
                "summary compute step raised — inspect the run header "
                "or get_run_debug_view for the underlying cause."
            )
            repair_hint = (
                "wait for run_strategy_backtest to return a terminal "
                "status (completed/failed) before reading the summary."
            )
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "backtest_summary_not_ready"},
            )
            return ToolResult(
                text=format_error_text(
                    "backtest_summary_not_ready", message, repair_hint
                ),
                is_error=True,
            )

        # state == "stale"
        message = (
            f"a newer backtest on the same task overwrote the persisted "
            f"summary; the stored summary is for run_id="
            f"{latest_summary_run_id!r}, not {run_id!r}."
        )
        repair_hint = (
            "call get_backtest_summary again with "
            f"run_id={latest_summary_run_id!r} to read the "
            "currently-stored summary."
        )
        await emit_debug_event(
            f"operation_{self.name}.failed",
            {**base_payload, "error_code": "backtest_summary_stale"},
        )
        return ToolResult(
            text=format_error_text(
                "backtest_summary_stale", message, repair_hint
            ),
            is_error=True,
        )
