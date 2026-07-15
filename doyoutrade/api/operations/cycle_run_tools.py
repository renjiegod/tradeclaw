from __future__ import annotations

from typing import Any

from doyoutrade.tools import (
    OperationHandler,
    ToolResult,
    call_with_task_name_fallback,
    tool_result_from_error_dict,
)
from doyoutrade.tools._pagination import append_pagination_hint
from doyoutrade.tools._prose import (
    append_json_payload,
    format_error_text,
    format_unknown_args,
)


_TASK_IDENTIFIER_DESCRIPTION = (
    "Task id (UUID). Exact task name is also accepted; if multiple tasks "
    "share the same name the call returns ambiguous_task_name with "
    "candidates so you can pick the right task_id. Use list_tasks(q=...) "
    "to discover the task_id."
)


def _contract_error_to_result(self_: OperationHandler, contract_error: dict[str, Any], error_kind: str | None) -> ToolResult:
    """Render a contract failure as a ToolResult — matches task_tools usage."""

    if error_kind == "unknown_arguments":
        text = format_unknown_args(
            list(contract_error.get("unknown", [])),
            sorted(self_._allowed_top_level_kwargs()),
            dict(contract_error.get("suggested_path") or {}),
        )
    else:
        text = format_error_text(
            "validation_error",
            str(contract_error.get("message") or contract_error.get("error") or "validation failed"),
        )
    return ToolResult(
        text=text,        is_error=True,
    )


class ListCycleRunsTool(OperationHandler):
    name = "list_cycle_runs"
    description = (
        "List cycle runs for a task with optional filters. "
        "Use run_id_contains for substring match, status for exact match, "
        "run_kind for 'scheduled'/'manual'/'debug', run_mode for 'paper'/'live'/'backtest'."
    )
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {
            "identifier": {"type": "string", "description": _TASK_IDENTIFIER_DESCRIPTION},
            "limit": {"type": "integer", "description": "Max results", "default": 50},
            "offset": {"type": "integer", "description": "Pagination offset", "default": 0},
            "run_id_contains": {"type": "string", "description": "Substring match on run_id"},
            "status": {"type": "string", "description": "Exact status match: 'running', 'completed', etc."},
            "run_kind": {"type": "string", "description": "'scheduled', 'manual', or 'debug'"},
            "run_mode": {"type": "string", "description": "'paper', 'live', or 'backtest'"},
            "started_after": {"type": "string", "description": "ISO datetime string — only runs started after this time"},
            "started_before": {"type": "string", "description": "ISO datetime string — only runs started before this time"},
            "run_id": {"type": "string", "description": "Exact backtest job run_id to filter by its session"},
        },
        "required": ["identifier"],
        "additionalProperties": False,
    }

    def __init__(self, platform_service: Any):
        self._svc = platform_service

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            return _contract_error_to_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs
        identifier = kwargs.get("identifier")
        if not isinstance(identifier, str) or not identifier.strip():
            return ToolResult(
                text=format_error_text("validation_error", "identifier is required"),                is_error=True,
            )

        limit = int(kwargs.get("limit", 50))
        offset = int(kwargs.get("offset", 0))

        async def _action(tid: str) -> dict[str, Any]:
            return await self._svc.list_cycle_runs_summary(
                tid,
                limit=limit,
                offset=offset,
                run_id_contains=kwargs.get("run_id_contains"),
                status=kwargs.get("status"),
                run_kind=kwargs.get("run_kind"),
                run_mode=kwargs.get("run_mode"),
                started_after=kwargs.get("started_after"),
                started_before=kwargs.get("started_before"),
                run_id=kwargs.get("run_id"),
            )

        result, err, resolved_from = await call_with_task_name_fallback(
            self._svc, identifier, _action
        )
        if err is not None:
            return tool_result_from_error_dict(err)

        items = result.get("items") if isinstance(result, dict) else []
        if not isinstance(items, list):
            items = []
        total = int(result.get("total", len(items))) if isinstance(result, dict) else len(items)
        applied_limit = int(result.get("limit", limit)) if isinstance(result, dict) else limit
        applied_offset = int(result.get("offset", offset)) if isinstance(result, dict) else offset

        if not items:
            text = (
                f"No cycle runs found for task {identifier} "
                f"(total={total}, limit={applied_limit}, offset={applied_offset})."
            )
        else:
            lines = [
                f"Found {len(items)} cycle run(s) for task {identifier} "
                f"of {total} total (limit={applied_limit}, offset={applied_offset}):"
            ]
            for item in items:
                rid = item.get("run_id", "?")
                st = item.get("status", "")
                kind = item.get("run_kind", "")
                mode = item.get("run_mode", "")
                started = item.get("started_at", "")
                lines.append(f"- {rid} [{st}] kind={kind} mode={mode} started={started}")
            append_pagination_hint(
                lines,
                tool_name=self.name,
                total=total,
                shown=len(items),
                limit=applied_limit,
                offset=applied_offset,
                filters={
                    "identifier": identifier,
                    "run_id_contains": kwargs.get("run_id_contains"),
                    "status": kwargs.get("status"),
                    "run_kind": kwargs.get("run_kind"),
                    "run_mode": kwargs.get("run_mode"),
                    "started_after": kwargs.get("started_after"),
                    "started_before": kwargs.get("started_before"),
                    "run_id": kwargs.get("run_id"),
                },
            )
            text = "\n".join(lines)

        payload: dict[str, Any] = {"status": "ok", **(result if isinstance(result, dict) else {"items": items})}
        if resolved_from is not None:
            payload["resolved_from_name"] = resolved_from
        return ToolResult(text=append_json_payload(text, payload))


class GetCycleRunTool(OperationHandler):
    name = "get_cycle_run"
    description = "Get detailed information about a specific cycle run, including proposals, decisions, and phase completion."
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "Exact cycle run run_id"},
        },
        "required": ["run_id"],
        "additionalProperties": False,
    }

    def __init__(self, platform_service: Any):
        self._svc = platform_service

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            return _contract_error_to_result(self, contract.error, contract.error_kind)
        kwargs = contract.kwargs
        run_id = kwargs.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            return ToolResult(
                text=format_error_text("validation_error", "run_id is required"),                is_error=True,
            )
        try:
            row = await self._svc.get_cycle_run(run_id)
        except Exception as exc:
            return ToolResult(
                text=format_error_text("get_cycle_run_failed", str(exc)),                is_error=True,
            )

        st = row.get("status") if isinstance(row, dict) else None
        header = f"Cycle run {run_id} [{st}]."
        return ToolResult(text=append_json_payload(header, {"status": "ok", "cycle_run": row}))
