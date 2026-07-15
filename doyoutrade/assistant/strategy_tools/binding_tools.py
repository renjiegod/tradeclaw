from __future__ import annotations

from typing import Any

from doyoutrade.tools import (
    OperationHandler,
    ToolResult,
    tool_result_from_error_dict,
)
from doyoutrade.tools._coercion import SchemaCoercion
from doyoutrade.tools._identifier_kinds import IdentifierGuard, IdentifierKind
from doyoutrade.tools._prose import format_error_text
from doyoutrade.debug import emit_debug_event


class BindStrategyDefinitionToTaskTool(OperationHandler):
    name = "bind_strategy_definition_to_task"
    description = (
        "Bind a strategy definition to a task by writing "
        "``settings.strategy.definition_id`` on the task. On success returns "
        "``status: ok`` with the updated ``task``. "
        "Known ``error_code``: ``wrong_identifier_type`` when ``task_id`` "
        "is not uuid-shaped or ``definition_id`` is not ``sd-...``."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Target task id (uuid-style).",
            },
            "definition_id": {
                "type": "string",
                "description": (
                    "Strategy definition id to bind, shaped ``sd-...``. "
                    "Use ``get_strategy_definition`` to look up the id."
                ),
            },
        },
        "required": ["task_id", "definition_id"],
    }

    identifier_guards = (
        IdentifierGuard(field="task_id", kind=IdentifierKind.TASK_ID),
        IdentifierGuard(field="definition_id", kind=IdentifierKind.DEFINITION_ID),
    )

    def __init__(self, platform_service: Any | None):
        self._platform_service = platform_service

    async def execute(self, task_id: str, definition_id: str) -> ToolResult:
        base_payload = {"tool": self.name, "task_id": task_id, "definition_id": definition_id}
        await emit_debug_event(f"operation_{self.name}.request", dict(base_payload))
        guard = self._apply_identifier_guards(
            {"task_id": task_id, "definition_id": definition_id}
        )
        if guard is not None:
            await emit_debug_event(
                f"operation_{self.name}.rejected", {**base_payload, "error": guard}
            )
            return tool_result_from_error_dict(guard)
        if self._platform_service is None or not hasattr(self._platform_service, "update_task"):
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "platform_service_unavailable"},
            )
            return ToolResult(
                text=format_error_text(
                    "platform_service_unavailable",
                    "platform service is not available",
                ),
                is_error=True,
            )
        await emit_debug_event(f"operation_{self.name}.validated", dict(base_payload))
        await self._platform_service.update_task(
            task_id,
            settings={"strategy": {"definition_id": definition_id}},
        )
        return ToolResult(
            text=(
                f"Bound strategy definition {definition_id} to task {task_id}. "
                "Next: run a backtest or promote to live when ready."
            ),
        )


class PromoteStrategyDefinitionToLiveTool(OperationHandler):
    name = "promote_strategy_definition_to_live"
    description = (
        "Promote a strategy definition binding to a live task. Patch semantics: "
        "only the fields you supply are written to ``settings.strategy`` — "
        "omit ``approval_policy`` / ``risk_overrides`` to leave existing "
        "values untouched. On success returns ``status: ok`` with the "
        "updated ``task``. "
        "Known ``error_code`` values: "
        "``wrong_identifier_type`` (bad ``task_id`` / ``definition_id`` "
        "shape); ``invalid_approval_policy_json`` / "
        "``invalid_risk_overrides_json`` (malformed JSON string for those "
        "fields)."
    )
    category = "strategy"
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Target task id (uuid-style).",
            },
            "definition_id": {
                "type": "string",
                "description": (
                    "Strategy definition id to bind, shaped ``sd-...``."
                ),
            },
            "approval_policy": {
                "type": "object",
                "description": (
                    "Optional approval-policy object written under "
                    "``settings.strategy.approval_policy``. Omit to leave "
                    "the task's existing policy untouched. JSON-encoded "
                    "string tolerated; malformed strings return "
                    "``invalid_approval_policy_json``."
                ),
            },
            "risk_overrides": {
                "type": "object",
                "description": (
                    "Optional risk-overrides object written under "
                    "``settings.strategy.risk_overrides``. Omit to leave "
                    "existing overrides untouched. JSON-encoded string "
                    "tolerated; malformed strings return "
                    "``invalid_risk_overrides_json``."
                ),
            },
        },
        "required": ["task_id", "definition_id"],
    }

    identifier_guards = (
        IdentifierGuard(field="task_id", kind=IdentifierKind.TASK_ID),
        IdentifierGuard(field="definition_id", kind=IdentifierKind.DEFINITION_ID),
    )
    coercion_rules = (
        SchemaCoercion(field="approval_policy", declared_type="object"),
        SchemaCoercion(field="risk_overrides", declared_type="object"),
    )

    def __init__(self, platform_service: Any | None):
        self._platform_service = platform_service

    async def execute(
        self,
        task_id: str,
        definition_id: str,
        approval_policy: dict[str, Any] | str | None = None,
        risk_overrides: dict[str, Any] | str | None = None,
    ) -> ToolResult:
        base_payload = {"tool": self.name, "task_id": task_id, "definition_id": definition_id}
        await emit_debug_event(f"operation_{self.name}.request", dict(base_payload))
        guard = self._apply_identifier_guards(
            {"task_id": task_id, "definition_id": definition_id}
        )
        if guard is not None:
            await emit_debug_event(
                f"operation_{self.name}.rejected", {**base_payload, "error": guard}
            )
            return tool_result_from_error_dict(guard)
        coercion = self._apply_schema_coercion(
            {"approval_policy": approval_policy, "risk_overrides": risk_overrides}
        )
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
        approval_policy = coercion.kwargs.get("approval_policy")
        risk_overrides = coercion.kwargs.get("risk_overrides")
        if self._platform_service is None or not hasattr(self._platform_service, "update_task"):
            await emit_debug_event(
                f"operation_{self.name}.failed",
                {**base_payload, "error_code": "platform_service_unavailable"},
            )
            return ToolResult(
                text=format_error_text(
                    "platform_service_unavailable",
                    "platform service is not available",
                ),
                is_error=True,
            )
        await emit_debug_event(f"operation_{self.name}.validated", dict(base_payload))
        # Patch semantics: only write fields the caller explicitly supplied.
        # None fields are ignored so an existing approval_policy /
        # risk_overrides on the task is never clobbered.
        strategy_patch: dict[str, Any] = {"definition_id": definition_id}
        if approval_policy is not None:
            strategy_patch["approval_policy"] = approval_policy
        if risk_overrides is not None:
            strategy_patch["risk_overrides"] = risk_overrides
        await self._platform_service.update_task(
            task_id,
            settings={"strategy": strategy_patch},
        )
        applied = sorted(k for k in strategy_patch.keys() if k != "definition_id")
        applied_str = f" (applied: {', '.join(applied)})" if applied else ""
        return ToolResult(
            text=(
                f"Promoted strategy definition {definition_id} to task {task_id}{applied_str}. "
                "Next: monitor cycle_runs and use suggest_strategy_iteration if behaviour drifts."
            ),
        )
