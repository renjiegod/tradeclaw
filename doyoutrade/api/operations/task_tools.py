from __future__ import annotations

from typing import Any

from doyoutrade.tools import (
    OperationHandler,
    ToolResult,
    _json_dumps,
    call_with_task_name_fallback,
    tool_result_from_error_dict,
    wrong_identifier_type_error,
)
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._pagination import append_pagination_hint
from doyoutrade.tools._prose import (
    append_json_payload,
    format_error_text,
    format_unknown_args,
)
from doyoutrade.debug import emit_debug_event
from doyoutrade.data.cache_policy import (
    KNOWN_PROVIDER_NAMES,
    UNVERIFIABLE_GAP_POLICIES,
)
from doyoutrade.runtime.cycle_task import (
    validate_api_task_settings,
    validate_strategy_binding_block,
)


# Backwards-compat thin aliases — kept so in-file callers keep working
# without me touching every reference. New tool files should import the
# shared helpers from ``_prose`` directly.
_format_error_text = format_error_text
_format_unknown_args = format_unknown_args


_TASK_IDENTIFIER_DESCRIPTION = (
    "Task id (UUID). Exact task name is also accepted; if multiple tasks "
    "share the same name the call returns ambiguous_task_name with "
    "candidates so you can pick the right task_id. Use list_tasks(q=...) "
    "to discover the task_id."
)

# 子对象 schema —— 顶层 agent / strategy 各自的内部形状。
_AGENT_SUBSCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "Agent runtime settings.",
    "properties": {
        "react_max_turns": {"type": "integer", "minimum": 1},
        "signal_tool_names": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Signal tool names exposed to the model.",
        },
        "position_constraints": {
            "type": "object",
            "properties": {
                "max_single_order_amount": {"type": "number"},
                "max_position_ratio": {"type": "number"},
                "review_equity_fraction": {"type": "number"},
                "max_task_position_amount": {"type": "number"},
                "max_task_position_ratio": {"type": "number"},
            },
        },
        "approval": {
            "type": "object",
            "properties": {
                "min_notional_for_approval": {"type": "number"},
                "timeout_seconds": {"type": "integer"},
            },
        },
    },
    "additionalProperties": False,
}

_STRATEGY_SUBSCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Strategy binding. Pass ``definition_id`` (sd-...) with optional "
        "``parameter_overrides``."
    ),
    "properties": {
        "definition_id": {"type": "string", "description": "Strategy definition id (sd-...)."},
        "parameter_overrides": {"type": "object"},
        "execution_profile": {"type": "string"},
    },
    "additionalProperties": False,
}

_DATA_CACHE_SUBSCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Per-task data cache / backfill / continuity policy. Controls the "
        "local-DB-first read, the upstream gap-backfill source order, and the "
        "write-time continuity guarantee. Omit to use defaults (local_first, "
        "auto_backfill, calendar continuity, fail on an unverifiable gap)."
    ),
    "properties": {
        "source_priority": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(KNOWN_PROVIDER_NAMES)},
            "description": (
                "Ordered backfill provider ids to try on a local miss. "
                "Default mirrors the auto chain: qmt, baostock, mootdx, akshare, tushare."
            ),
        },
        "local_first": {
            "type": "boolean",
            "description": "Read the local DB before hitting upstream. Default true.",
        },
        "auto_backfill": {
            "type": "boolean",
            "description": "Fetch from upstream and persist on a local miss. Default true.",
        },
        "continuity": {
            "type": "object",
            "properties": {
                "on_unverifiable_gap": {
                    "type": "string",
                    "enum": sorted(UNVERIFIABLE_GAP_POLICIES),
                    "description": (
                        "When an authoritative-calendar gap cannot be proven a "
                        "suspension (provider gives no per-date 停牌 signal): "
                        "'fail' rejects the write, 'degrade' persists + warns. "
                        "Default 'fail'."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

# create_task / update_task 共用的顶层字段（不含 name / mode / description /
# identifier，那几个是 tool-specific）。
_COMMON_FLAT_PROPERTIES: dict[str, Any] = {
    "universe": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Tradable universe symbols.",
    },
    "strategy_preferences": {
        "type": "string",
        "description": "Free-text strategy preferences.",
    },
    "data_provider": {
        "type": "string",
        "description": "Data provider (auto / qmt / mock / akshare / baostock / tushare / mootdx). Default 'auto'.",
        "default": "auto",
    },
    "account_id": {
        "type": "string",
        "description": (
            "Account id (acct-...) this task runs against. Pass empty string on "
            "update to clear an explicit binding (runtime falls back to the "
            "default account). The account record carries live/mock mode and "
            "the QMT / mock portfolio connection."
        ),
    },
    "agent": _AGENT_SUBSCHEMA,
    "strategy": _STRATEGY_SUBSCHEMA,
    "data_cache": _DATA_CACHE_SUBSCHEMA,
}

_FLAT_SETTINGS_KEYS = (
    "universe",
    "strategy_preferences",
    "agent",
    "strategy",
    "account_id",
    "data_cache",
)
_COERCION_RULES: tuple[SchemaCoercion, ...] = (
    SchemaCoercion(field="agent", declared_type="object"),
    SchemaCoercion(field="strategy", declared_type="object"),
    SchemaCoercion(field="data_cache", declared_type="object"),
    SchemaCoercion(field="universe", declared_type="array", item_type=str),
)


def _build_settings_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Collect caller-provided flat kwargs into the settings dict shape the
    platform service expects. None values and absent keys are dropped so
    update_task gets clean patch semantics.

    Note: ``data_provider`` is deliberately excluded — the platform service
    accepts it as a top-level kwarg, not as a settings field.
    """
    out: dict[str, Any] = {}
    for key in _FLAT_SETTINGS_KEYS:
        value = kwargs.get(key)
        if value is None:
            continue
        out[key] = value
    return out


class CreateTaskTool(OperationHandler):
    name = "create_task"
    description = (
        "Create a trading task. All fields are top-level; strategy binding is a "
        "required `strategy` object containing `definition_id` plus optional "
        "`parameter_overrides`. Do not nest under "
        "`settings` — that field is rejected with unknown_arguments."
    )
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Human-readable task name."},
            "mode": {"type": "string", "description": "Run mode.", "default": "paper"},
            "description": {"type": "string", "description": "Free-text description.", "default": ""},
            **_COMMON_FLAT_PROPERTIES,
        },
        "required": ["name", "strategy"],
        "additionalProperties": False,
    }

    coercion_rules = _COERCION_RULES

    def __init__(self, platform_service: Any):
        self._svc = platform_service

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        base_payload: dict[str, Any] = {
            "tool": self.name,
            "input_keys": sorted(kwargs.keys()),
            "unknown_keys": list(contract.error.get("unknown", []))
            if contract.error and contract.error_kind == "unknown_arguments"
            else [],
        }
        if contract.error is not None:
            event = (
                "operation_create_task.rejected"
                if contract.error_kind == "unknown_arguments"
                else "operation_create_task.failed"
            )
            await emit_debug_event(event, {**base_payload, "error": contract.error})
            if contract.error_kind == "unknown_arguments":
                text = _format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                )
            else:
                text = _format_error_text(
                    "validation_error",
                    str(contract.error.get("message") or contract.error.get("error") or "validation failed"),
                )
            return ToolResult(
                text=text,
                is_error=True,
            )

        coercion = self._apply_schema_coercion(contract.kwargs)
        if coercion.error is not None:
            await emit_debug_event(
                "operation_create_task.failed",
                {
                    **base_payload,
                    "error_code": coercion.error.get("error_code"),
                    "error": coercion.error.get("error"),
                },
            )
            return ToolResult(
                text=_format_error_text(
                    str(coercion.error.get("error_code") or "coercion_error"),
                    str(coercion.error.get("error") or "input coercion failed"),
                ),
                is_error=True,
            )
        kwargs = coercion.kwargs
        if coercion.coerced_fields:
            base_payload["coerced_fields"] = coercion.coerced_fields

        name = kwargs.get("name")
        if not isinstance(name, str) or not name.strip():
            err = {
                "error_code": "missing_name",
                "error_type": "ValueError",
                "error": "name is required",
                "hint": "pass a non-empty task name",
            }
            await emit_debug_event("operation_create_task.failed", {**base_payload, **err})
            return ToolResult(
                text=_format_error_text("missing_name", "name is required", "pass a non-empty task name"),
                is_error=True,
            )

        strategy = kwargs.get("strategy")
        if not isinstance(strategy, dict):
            err = {
                "error_code": "missing_strategy_binding",
                "error_type": "ValueError",
                "error": "strategy object is required",
                "hint": (
                    "pass strategy={'definition_id': 'sd-...', 'parameter_overrides': {...}}"
                ),
            }
            await emit_debug_event("operation_create_task.failed", {**base_payload, **err})
            return ToolResult(
                text=_format_error_text(
                    "missing_strategy_binding",
                    err["error"],
                    err["hint"],
                ),
                is_error=True,
            )
        try:
            validate_strategy_binding_block(strategy)
        except ValueError as exc:
            err = {
                "error_code": "missing_strategy_binding",
                "error_type": "ValueError",
                "error": str(exc),
                "hint": (
                    "pass strategy={'definition_id': 'sd-...', 'parameter_overrides': {...}}"
                ),
            }
            await emit_debug_event("operation_create_task.failed", {**base_payload, **err})
            return ToolResult(
                text=_format_error_text(
                    "missing_strategy_binding",
                    err["error"],
                    err["hint"],
                ),
                is_error=True,
            )

        settings = _build_settings_payload(kwargs)
        await emit_debug_event(
            "operation_create_task.request",
            {**base_payload, "settings_keys": sorted(settings.keys())},
        )
        try:
            validate_api_task_settings(settings)
            await emit_debug_event(
                "operation_create_task.validated",
                {**base_payload, "name": name, "mode": str(kwargs.get("mode") or "paper")},
            )
            instance = await self._svc.create_task(
                name=name,
                mode=kwargs.get("mode") or "paper",
                description=kwargs.get("description") or "",
                data_provider=kwargs.get("data_provider") or "auto",
                settings=settings,
            )
            await emit_debug_event(
                "operation_create_task.created",
                {**base_payload, "task_id": instance.task_id, "name": instance.config.name},
            )
            text = (
                f"Created task '{instance.config.name}' "
                f"(task_id={instance.task_id}, mode={instance.config.mode})."
            )
            return ToolResult(text=text)
        except Exception as exc:
            error_type = "validation_error" if isinstance(exc, ValueError) else "service_error"
            await emit_debug_event(
                "operation_create_task.failed",
                {**base_payload, "error_type": error_type, "error": str(exc)},
            )
            return ToolResult(
                text=_format_error_text(error_type, str(exc)),
                is_error=True,
            )


class GetTaskTool(OperationHandler):
    name = "get_task"
    description = "Get a trading task by task_id (UUID) or exact task name."
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {
            "identifier": {"type": "string", "description": _TASK_IDENTIFIER_DESCRIPTION},
        },
        "required": ["identifier"],
    }

    def __init__(self, platform_service: Any):
        self._svc = platform_service

    async def execute(self, identifier: str) -> ToolResult:
        guard = wrong_identifier_type_error(identifier)
        if guard is not None:
            return tool_result_from_error_dict(guard)
        task, err, resolved_from = await call_with_task_name_fallback(
            self._svc, identifier, self._svc.get_task_status
        )
        if err is not None:
            return tool_result_from_error_dict(err)
        tid = task.get("task_id") if isinstance(task, dict) else None
        st = task.get("status") if isinstance(task, dict) else None
        nm = task.get("name") if isinstance(task, dict) else None
        resolved_hint = f" (resolved name {resolved_from!r})" if resolved_from else ""
        header = f"Task {tid} [{st}] {nm or ''}{resolved_hint}.".rstrip(".") + "."
        payload: dict[str, Any] = {"status": "ok", "task": task}
        if resolved_from is not None:
            payload["resolved_from_name"] = resolved_from
        return ToolResult(text=append_json_payload(header, payload))


class ListTasksTool(OperationHandler):
    name = "list_tasks"
    description = "List trading tasks."
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "List query substring filter."},
            "status": {"type": "string", "description": "List exact status filter."},
            "mode": {"type": "string", "description": "Run mode filter."},
            "definition_id": {
                "type": "string",
                "description": "Exact strategy definition id filter (sd-...).",
            },
            "limit": {"type": "integer", "description": "List page size.", "default": 20},
            "offset": {"type": "integer", "description": "List page offset.", "default": 0},
        },
    }

    def __init__(self, platform_service: Any):
        self._svc = platform_service

    async def execute(
        self,
        q: str | None = None,
        status: str | None = None,
        mode: str | None = None,
        definition_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> ToolResult:
        try:
            result = await self._svc.list_tasks_summary(
                q=q,
                status=status,
                mode=mode,
                definition_id=definition_id,
                limit=limit,
                offset=offset,
            )
        except Exception as exc:
            return ToolResult(
                text=_format_error_text("list_tasks_failed", str(exc)),
                is_error=True,
            )

        items = result["items"]
        total = result["total"]
        applied_limit = result["limit"]
        applied_offset = result["offset"]
        filters: list[str] = []
        if q:
            filters.append(f"q={q!r}")
        if status:
            filters.append(f"status={status!r}")
        if mode:
            filters.append(f"mode={mode!r}")
        if definition_id:
            filters.append(f"definition_id={definition_id!r}")
        filter_suffix = f" [filters: {', '.join(filters)}]" if filters else ""

        if not items:
            text = (
                f"No tasks found{filter_suffix} "
                f"(total={total}, limit={applied_limit}, offset={applied_offset})."
            )
        else:
            header = (
                f"Found {len(items)} task(s) of {total} total"
                f"{filter_suffix} (limit={applied_limit}, offset={applied_offset}):"
            )
            lines = [header]
            for item in items:
                tid = item.get("task_id", "?")
                nm = item.get("name", "")
                st = item.get("status", "")
                md = item.get("mode", "")
                lines.append(f"- {tid} [{st}] {nm} ({md})")
            append_pagination_hint(
                lines,
                tool_name=self.name,
                total=total,
                shown=len(items),
                limit=applied_limit,
                offset=applied_offset,
                filters={"q": q, "status": status, "mode": mode, "definition_id": definition_id},
            )
            text = "\n".join(lines)

        return ToolResult(
            text=text,        )


class UpdateTaskTool(OperationHandler):
    name = "update_task"
    description = (
        "Update a trading task by task_id or exact task name. All fields are "
        "top-level patch fields (universe, strategy, agent, ...). Do not nest "
        "under `settings`."
    )
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {
            "identifier": {"type": "string", "description": _TASK_IDENTIFIER_DESCRIPTION},
            "name": {"type": "string", "description": "Human-readable task name."},
            "mode": {"type": "string", "description": "Run mode."},
            "description": {"type": "string", "description": "Free-text description."},
            **_COMMON_FLAT_PROPERTIES,
        },
        "required": ["identifier"],
        "additionalProperties": False,
    }

    coercion_rules = _COERCION_RULES

    def __init__(self, platform_service: Any):
        self._svc = platform_service

    async def execute(self, **kwargs: Any) -> ToolResult:
        contract = self._enforce_kwargs_contract(kwargs)
        if contract.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.{'rejected' if contract.error_kind == 'unknown_arguments' else 'failed'}",
                {"tool": self.name, "input_keys": sorted(kwargs.keys()), "error": contract.error},
            )
            if contract.error_kind == "unknown_arguments":
                text = _format_unknown_args(
                    list(contract.error.get("unknown", [])),
                    sorted(self._allowed_top_level_kwargs()),
                    dict(contract.error.get("suggested_path") or {}),
                )
            else:
                text = _format_error_text(
                    "validation_error",
                    str(contract.error.get("message") or contract.error.get("error") or "validation failed"),
                )
            return ToolResult(
                text=text,
                is_error=True,
            )

        coercion = self._apply_schema_coercion(contract.kwargs)
        if coercion.error is not None:
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {
                    "tool": self.name,
                    "input_keys": sorted(kwargs.keys()),
                    "error_code": coercion.error.get("error_code"),
                    "error": coercion.error.get("error"),
                },
            )
            return ToolResult(
                text=_format_error_text(
                    str(coercion.error.get("error_code") or "coercion_error"),
                    str(coercion.error.get("error") or "input coercion failed"),
                ),
                is_error=True,
            )
        kwargs = coercion.kwargs

        identifier = kwargs.get("identifier")
        if not isinstance(identifier, str) or not identifier.strip():
            return ToolResult(
                text=_format_error_text("validation_error", "identifier is required"),
                is_error=True,
            )

        guard = wrong_identifier_type_error(identifier)
        if guard is not None:
            return tool_result_from_error_dict(guard)

        settings_patch = _build_settings_payload(kwargs)
        name = kwargs.get("name")
        mode = kwargs.get("mode")
        description = kwargs.get("description")
        data_provider = kwargs.get("data_provider")

        async def _action(tid: str) -> Any:
            return await self._svc.update_task(
                tid,
                name=name,
                mode=mode,
                description=description,
                data_provider=data_provider,
                settings=settings_patch if settings_patch else None,
            )

        updated, err, resolved_from = await call_with_task_name_fallback(
            self._svc, identifier, _action
        )
        if err is not None:
            return tool_result_from_error_dict(err)
        applied = updated.get("__applied_settings_keys__") if isinstance(updated, dict) else None
        payload: dict[str, Any] = {"status": "updated", "task": updated}
        if isinstance(applied, list):
            payload["applied_settings_keys"] = applied
            if isinstance(updated, dict):
                updated.pop("__applied_settings_keys__", None)
        if resolved_from is not None:
            payload["resolved_from_name"] = resolved_from
        tid = updated.get("task_id") if isinstance(updated, dict) else None
        applied_str = f" (applied: {', '.join(applied)})" if isinstance(applied, list) and applied else ""
        resolved_hint = f" via name {resolved_from!r}" if resolved_from else ""
        header = f"Updated task {tid}{resolved_hint}{applied_str}."
        return ToolResult(text=append_json_payload(header, payload))


class DeleteTaskTool(OperationHandler):
    name = "delete_task"
    description = "Delete a trading task by task_id or exact task name."
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {
            "identifier": {"type": "string", "description": _TASK_IDENTIFIER_DESCRIPTION},
        },
        "required": ["identifier"],
    }

    def __init__(self, platform_service: Any):
        self._svc = platform_service

    async def execute(self, identifier: str) -> ToolResult:
        guard = wrong_identifier_type_error(identifier)
        if guard is not None:
            return tool_result_from_error_dict(guard)

        async def _action(tid: str) -> str:
            await self._svc.delete_task(tid)
            return tid

        deleted_id, err, resolved_from = await call_with_task_name_fallback(
            self._svc, identifier, _action
        )
        if err is not None:
            return tool_result_from_error_dict(err)
        data: dict[str, Any] = {"status": "deleted", "task_id": deleted_id}
        if resolved_from is not None:
            data["resolved_from_name"] = resolved_from
        resolved_hint = f" (resolved name {resolved_from!r})" if resolved_from else ""
        text = f"Deleted task {deleted_id}{resolved_hint}."
        return ToolResult(text=text)


class CloneTaskTool(OperationHandler):
    name = "clone_task"
    description = (
        "Duplicate an existing trading task. Backtest tasks are one-shot — once a "
        "run exists, the task cannot be re-run, so clone it to start a fresh "
        "configured task with the same strategy binding, universe and settings."
    )
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {
            "source_identifier": {
                "type": "string",
                "description": (
                    "Source task to clone, identified by task_id (UUID) or "
                    "exact task name. " + _TASK_IDENTIFIER_DESCRIPTION
                ),
            },
            "name": {
                "type": "string",
                "description": "Optional new task name. Defaults to '{source}_copy'.",
            },
            "description": {
                "type": "string",
                "description": "Optional override for the cloned task description.",
            },
        },
        "required": ["source_identifier"],
    }

    def __init__(self, platform_service: Any):
        self._svc = platform_service

    async def execute(
        self,
        source_identifier: str,
        name: str | None = None,
        description: str | None = None,
    ) -> ToolResult:
        guard = wrong_identifier_type_error(source_identifier)
        if guard is not None:
            return tool_result_from_error_dict(guard)

        async def _action(tid: str) -> Any:
            return await self._svc.clone_task(
                tid,
                name=name,
                description=description,
            )

        instance, err, resolved_from = await call_with_task_name_fallback(
            self._svc, source_identifier, _action
        )
        if err is not None:
            return tool_result_from_error_dict(err)
        task_id = getattr(instance, "task_id", None)
        config = getattr(instance, "config", None)
        clone_name = config.name if config is not None else None
        resolved_hint = f" (resolved name {resolved_from!r})" if resolved_from else ""
        text = (
            f"Cloned task {source_identifier!r}{resolved_hint} -> "
            f"task_id={task_id} name={clone_name!r}."
        )
        return ToolResult(text=text)
