"""`doyoutrade-cli schema <command>` — dump the underlying tool's JSON Schema.

CLI flags model the *common* shape of a tool's inputs, but rich tools
(``create_task`` etc.) have nested settings, identifier guards, and
error-code tables that don't fit neatly into a flat flag list. The
agent can run ``doyoutrade-cli schema task.get`` to retrieve the exact tool
parameter schema, the identifier-guard kinds, and the declared
coercion rules — enough to know how to shape a ``--params`` payload
when one lands in a later phase.

The command path uses dotted form (``task.get``) so it doesn't conflict
with click's natural argument parsing.
"""

from __future__ import annotations

from typing import Any

import click

from doyoutrade.cli.command_contracts import cli_contract_paths, get_cli_contract
from doyoutrade.cli._envelope import EXIT_OK, EXIT_VALIDATION, Meta, error_envelope, success_envelope
from doyoutrade.cli._format import write_envelope
from doyoutrade.cli._invoke import read_session_meta


# Registry mapping ``<group>.<cmd>`` → tool class import path.
# Kept declarative so adding a new command in Phase 1+ only needs an
# entry here, not a separate schema endpoint per tool.
_SCHEMA_TARGETS: dict[str, tuple[str, str, dict[str, Any] | None]] = {
    # cli command → (module, class_name, ctor_kwargs_for_inert_introspection)
    "task.get": ("doyoutrade.api.operations.task_tools", "GetTaskTool", {"platform_service": None}),
    "task.list": ("doyoutrade.api.operations.task_tools", "ListTasksTool", {"platform_service": None}),
    "task.create": ("doyoutrade.api.operations.task_tools", "CreateTaskTool", {"platform_service": None}),
    "task.update": ("doyoutrade.api.operations.task_tools", "UpdateTaskTool", {"platform_service": None}),
    "task.delete": ("doyoutrade.api.operations.task_tools", "DeleteTaskTool", {"platform_service": None}),
    "task.clone": ("doyoutrade.api.operations.task_tools", "CloneTaskTool", {"platform_service": None}),
    "stock.lookup": ("doyoutrade.api.operations.stock_lookup", "LookupStockSymbolTool", {}),
    "stock.screen": ("doyoutrade.api.operations.stock_screen", "StockScreenTool", {}),
    "strategy.definition.get": (
        "doyoutrade.assistant.strategy_tools.resource_tools",
        "GetStrategyDefinitionTool",
        {"repository": None},
    ),
    "strategy.authoring.open": (
        "doyoutrade.assistant.strategy_tools.authoring_tools",
        "OpenStrategyAuthoringTool",
        None,
    ),
    "strategy.authoring.cancel": (
        "doyoutrade.assistant.strategy_tools.authoring_tools",
        "CancelStrategyAuthoringTool",
        None,
    ),
    "strategy.authoring.compile": (
        "doyoutrade.assistant.strategy_tools.authoring_tools",
        "CompileStrategyDraftTool",
        None,
    ),
    "strategy.authoring.finalize": (
        "doyoutrade.assistant.strategy_tools.authoring_tools",
        "FinalizeStrategyAuthoringTool",
        None,
    ),
    "strategy.bind": (
        "doyoutrade.assistant.strategy_tools.binding_tools",
        "BindStrategyDefinitionToTaskTool",
        {"platform_service": None},
    ),
    "strategy.promote": (
        "doyoutrade.assistant.strategy_tools.binding_tools",
        "PromoteStrategyDefinitionToLiveTool",
        {"platform_service": None},
    ),
    # backtest watch is a streaming poll loop, not a tool. We still
    # expose its underlying tool's schema so the agent can discover the
    # shape of each per-poll envelope.
    "backtest.watch": (
        "doyoutrade.assistant.strategy_tools.resource_tools",
        "GetBacktestSummaryTool",
        {"platform_service": None},
    ),
    "cron.list": ("doyoutrade.api.operations.cron_tools", "ListCronJobsTool", {"cron_manager": None}),
    "cron.get": ("doyoutrade.api.operations.cron_tools", "GetCronJobTool", {"cron_manager": None}),
    "cron.runs.list": (
        "doyoutrade.api.operations.cron_tools",
        "ListCronJobRunsTool",
        {"run_repository": None},
    ),
    "cron.runs.get": (
        "doyoutrade.api.operations.cron_tools",
        "GetCronJobRunTool",
        {"run_repository": None},
    ),
    "cycle.list": (
        "doyoutrade.api.operations.cycle_run_tools",
        "ListCycleRunsTool",
        {"platform_service": None},
    ),
    "cycle.get": (
        "doyoutrade.api.operations.cycle_run_tools",
        "GetCycleRunTool",
        {"platform_service": None},
    ),
    "debug.get-run-view": (
        "doyoutrade.assistant.strategy_tools.resource_tools",
        "GetRunDebugViewTool",
        {"platform_service": None},
    ),
    "route.list": (
        "doyoutrade.api.operations.model_route_tools",
        "ListModelRoutesTool",
        {"platform_service": None},
    ),
    "sdk.dp-methods": (
        "doyoutrade.api.operations.strategy_discovery",
        "ListDpMethodsTool",
        {},
    ),
    "sdk.indicators": (
        "doyoutrade.api.operations.strategy_discovery",
        "ListIndicatorsTool",
        {},
    ),
    "sdk.data-requests": (
        "doyoutrade.api.operations.strategy_discovery",
        "ListDataRequestsTool",
        {},
    ),
    "data.run": ("doyoutrade.api.operations.data_run", "DataRunTool", {}),
    "data.news": ("doyoutrade.api.operations.data_news", "DataNewsTool", {}),
    "data.fundamentals": (
        "doyoutrade.api.operations.data_fundamentals",
        "DataFundamentalsTool",
        {},
    ),
    "data.sectors": ("doyoutrade.api.operations.data_sector", "DataSectorTool", {}),
    "data.sector-members": ("doyoutrade.api.operations.data_sector", "DataSectorTool", {}),
    "data.events": ("doyoutrade.api.operations.data_events", "DataEventsTool", {}),
    "analysis.pattern": ("doyoutrade.api.operations.pattern", "PatternRecognitionTool", {}),
    "analysis.indicators": (
        "doyoutrade.api.operations.indicators_compute",
        "IndicatorComputeTool",
        {},
    ),
    "analysis.factor": ("doyoutrade.api.operations.factor", "FactorAnalysisTool", {}),
    "backtest.run": (
        "doyoutrade.assistant.strategy_tools.run_tools",
        "RunStrategyBacktestTool",
        {"platform_service": None},
    ),
    "backtest.summary": (
        "doyoutrade.assistant.strategy_tools.resource_tools",
        "GetBacktestSummaryTool",
        {"platform_service": None},
    ),
    "backtest.suggest-iteration": (
        "doyoutrade.assistant.strategy_tools.run_tools",
        "SuggestStrategyIterationTool",
        {"platform_service": None},
    ),
    "strategy.inspect": (
        "doyoutrade.assistant.strategy_tools.run_tools",
        "InspectStrategyResourcesTool",
        {"definition_repository": None},
    ),
}


def _load_tool_class(module_path: str, class_name: str) -> Any:
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _tool_metadata(tool_cls: Any, ctor_kwargs: dict[str, Any] | None) -> dict[str, Any]:
    """Return assistant-tool metadata without requiring runtime dependencies."""

    instance = None if ctor_kwargs is None else (tool_cls(**ctor_kwargs) if ctor_kwargs else tool_cls())
    source = instance if instance is not None else tool_cls
    return {
        "tool_name": getattr(source, "name", getattr(tool_cls, "name", tool_cls.__name__)),
        "tool_description": getattr(source, "description", getattr(tool_cls, "description", "")),
        "category": getattr(source, "category", getattr(tool_cls, "category", "agent")),
        "parameters": getattr(source, "parameters", getattr(tool_cls, "parameters", {})),
        "coercion_rules": [
            {
                "field": rule.field,
                "declared_type": rule.declared_type,
                "error_code": rule.resolved_error_code(),
            }
            for rule in getattr(source, "coercion_rules", ()) or ()
        ],
        "identifier_guards": [
            {"field": guard.field, "kind": guard.kind.value}
            for guard in getattr(source, "identifier_guards", ()) or ()
        ],
        "requires_session_id": bool(getattr(source, "requires_session_id", False)),
        "requires_calling_agent_id": bool(getattr(source, "requires_calling_agent_id", False)),
        "requires_calling_session_id": bool(getattr(source, "requires_calling_session_id", False)),
    }


@click.command("schema")
@click.argument("command_path")
def schema(command_path: str) -> None:
    """Print the JSON Schema of the tool backing ``command_path``.

    ``command_path`` is the dotted CLI path, e.g. ``task.get`` for
    ``doyoutrade-cli task get``. The output envelope's ``data`` block carries
    the parameter schema, identifier guards, and any coercion rules — the
    same surface the in-process tool would expose to the model.
    """

    fmt = click.get_current_context().find_root().obj.get("fmt", "json") if click.get_current_context().find_root().obj else "json"
    meta = read_session_meta()

    target = _SCHEMA_TARGETS.get(command_path)
    if target is None:
        # Contract-only fallback: commands without a backing OperationHandler
        # class (e.g. cron / account writes, which POST straight to the server)
        # can still expose their declarative flag contract from command_contracts.
        contract_only = get_cli_contract(command_path)
        if contract_only is not None:
            data = {"cli_contract": contract_only}
            summary = f"CLI contract for {command_path} (no tool schema; contract-only)."
            envelope = success_envelope(data, summary, meta=meta)
            write_envelope(envelope, fmt=fmt)
            click.get_current_context().exit(EXIT_OK)
            return
        known = sorted(set(_SCHEMA_TARGETS) | set(cli_contract_paths()))
        envelope = error_envelope(
            error_code="unknown_command",
            error_type="ValueError",
            message=f"unknown CLI command path: {command_path!r}",
            hint=f"available paths: {', '.join(known)}",
            meta=meta,
        )
        write_envelope(envelope, fmt=fmt)
        click.get_current_context().exit(EXIT_VALIDATION)
        return

    module_path, class_name, ctor_kwargs = target
    tool_cls = _load_tool_class(module_path, class_name)
    data = _tool_metadata(tool_cls, ctor_kwargs)
    cli_contract = get_cli_contract(command_path)
    if cli_contract is not None:
        data["cli_contract"] = cli_contract

    summary = f"Schema for {command_path} (tool={data['tool_name']})."
    envelope = success_envelope(data, summary, meta=meta)
    write_envelope(envelope, fmt=fmt)
    click.get_current_context().exit(EXIT_OK)


__all__ = ["schema"]
