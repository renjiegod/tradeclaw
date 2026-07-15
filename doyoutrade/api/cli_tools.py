from __future__ import annotations

from typing import Any

from doyoutrade.tools import OperationRegistry
from doyoutrade.assistant.strategy_tools.authoring_tools import (
    CancelStrategyAuthoringTool,
    CompileStrategyDraftTool,
    FinalizeStrategyAuthoringTool,
    OpenStrategyAuthoringTool,
)
from doyoutrade.assistant.strategy_tools.binding_tools import (
    BindStrategyDefinitionToTaskTool,
    PromoteStrategyDefinitionToLiveTool,
)
from doyoutrade.assistant.strategy_tools.resource_tools import (
    GetBacktestSummaryTool,
    GetRunDebugViewTool,
    GetStrategyDefinitionTool,
    UpdateStrategyDefinitionTool,
)
from doyoutrade.assistant.strategy_tools.run_tools import (
    InspectStrategyResourcesTool,
    RunStrategyBacktestTool,
    SuggestStrategyIterationTool,
)
from doyoutrade.api.operations.cycle_run_tools import GetCycleRunTool, ListCycleRunsTool
from doyoutrade.api.operations.cron_tools import (
    GetCronJobRunTool,
    GetCronJobTool,
    ListCronJobRunsTool,
    ListCronJobsTool,
)
from doyoutrade.api.operations.model_route_tools import ListModelRoutesTool
from doyoutrade.api.operations.factor import FactorAnalysisTool
from doyoutrade.api.operations.data_news import DataNewsTool
from doyoutrade.api.operations.data_research import DataResearchReportsTool
from doyoutrade.api.operations.data_market_breadth import DataMarketBreadthTool
from doyoutrade.api.operations.data_lhb import DataLhbTool
from doyoutrade.api.operations.data_chips import DataChipsTool
from doyoutrade.api.operations.data_fund_flow import DataFundFlowTool
from doyoutrade.api.operations.data_earnings import DataEarningsTool
from doyoutrade.api.operations.data_run import DataRunTool
from doyoutrade.api.operations.data_events import DataEventsTool
from doyoutrade.api.operations.data_fundamentals import DataFundamentalsTool
from doyoutrade.api.operations.data_sector import DataSectorTool
from doyoutrade.api.operations.data_sector_heat import DataSectorHeatTool
from doyoutrade.api.operations.indicators_compute import IndicatorComputeTool
from doyoutrade.api.operations.pattern import PatternRecognitionTool
from doyoutrade.api.operations.recursive_analysis import ValidateRecursiveStabilityTool
from doyoutrade.api.operations.stock_lookup import LookupStockSymbolTool
from doyoutrade.api.operations.walk_forward import WalkForwardBacktestTool
from doyoutrade.api.operations.stock_screen import StockScreenTool
from doyoutrade.api.operations.strategy_discovery import (
    ListDataRequestsTool,
    ListDpMethodsTool,
    ListIndicatorsTool,
)
from doyoutrade.api.operations.task_tools import (
    CloneTaskTool,
    CreateTaskTool,
    DeleteTaskTool,
    GetTaskTool,
    ListTasksTool,
    UpdateTaskTool,
)


def build_cli_tool_registry(
    *,
    service: Any,
    strategy_registry_service: Any | None = None,
    strategy_definition_repository: Any | None = None,
    cron_manager: Any | None = None,
    cron_run_repo: Any | None = None,
    strategy_storage: Any | None = None,
    compiler: Any | None = None,
) -> OperationRegistry:
    """Build the API-owned tool registry used by ``doyoutrade-cli``.

    These tools are intentionally separate from the assistant chat registry:
    the CLI needs broad operational coverage, while the chat agent keeps a
    smaller curated tool inventory.

    Note: sandboxed file primitives (read_file / write_file / edit_file /
    list_files) are intentionally NOT in this registry — they live on the
    agent's in-process tool surface (``build_default_tool_registry``), not
    the CLI surface.  Lifecycle tools (open / cancel / compile / finalize)
    DO remain here as they involve DB writes + AST validation.

    Args:
        strategy_storage: ``StrategyStorage`` singleton used by the
            authoring lifecycle tools.  When ``None`` the 4 lifecycle tools
            are silently omitted so the registry degrades gracefully in
            minimal test setups.
        compiler: ``StrategyCompiler`` instance used by the lifecycle tools.
            When ``None`` alongside ``strategy_storage=None`` the lifecycle
            tools are omitted.
    """

    tools = [
        GetTaskTool(service),
        ListTasksTool(service),
        CreateTaskTool(service),
        UpdateTaskTool(service),
        DeleteTaskTool(service),
        CloneTaskTool(service),
        ListCycleRunsTool(service),
        GetCycleRunTool(service),
        GetRunDebugViewTool(service),
        ListModelRoutesTool(service),
        InspectStrategyResourcesTool(strategy_definition_repository),
        GetStrategyDefinitionTool(strategy_definition_repository),
        UpdateStrategyDefinitionTool(strategy_registry_service),
        BindStrategyDefinitionToTaskTool(service),
        PromoteStrategyDefinitionToLiveTool(service),
        RunStrategyBacktestTool(service),
        WalkForwardBacktestTool(service),
        GetBacktestSummaryTool(service),
        SuggestStrategyIterationTool(service),
        LookupStockSymbolTool(
            instrument_catalog_repository=getattr(
                service,
                "instrument_catalog_repository",
                None,
            )
        ),
        DataRunTool(),
        DataNewsTool(),
        DataResearchReportsTool(),
        DataMarketBreadthTool(),
        DataLhbTool(),
        DataChipsTool(),
        DataFundFlowTool(),
        DataEarningsTool(),
        DataSectorTool(),
        DataSectorHeatTool(),
        DataFundamentalsTool(),
        DataEventsTool(),
        PatternRecognitionTool(),
        IndicatorComputeTool(),
        FactorAnalysisTool(),
        StockScreenTool(
            strategy_definition_repository=strategy_definition_repository,
            strategy_storage=strategy_storage,
            compiler=compiler,
            # Local-first read-through over the synced ``market_bars`` warehouse:
            # a full-market scan reads already-synced bars locally instead of one
            # network round-trip per symbol. ``None`` (minimal setups) → unchanged
            # direct-network behaviour.
            market_bars_repository=getattr(service, "market_bars_repository", None),
        ),
        ListDpMethodsTool(),
        ListIndicatorsTool(),
        ListDataRequestsTool(),
        # Compiler defaults to a fresh StrategyCompiler when ``compiler`` is
        # None, so this stays available in minimal setups.
        ValidateRecursiveStabilityTool(compiler=compiler),
    ]
    if cron_manager is not None:
        tools.extend([ListCronJobsTool(cron_manager), GetCronJobTool(cron_manager)])
    if cron_run_repo is not None:
        tools.extend([ListCronJobRunsTool(cron_run_repo), GetCronJobRunTool(cron_run_repo)])
    # Authoring lifecycle tools (open / cancel / compile / finalize).
    # File primitives (read_file / write_file / edit_file / list_files) are
    # NOT registered here — they live on the agent's in-process surface.
    # When storage is absent (minimal test setups) the 4 lifecycle tools are
    # omitted silently so the rest of the registry still works.
    if strategy_storage is not None and strategy_definition_repository is not None:
        _compiler = compiler  # may be None → lifecycle tools skipped below
        if _compiler is not None:
            tools.extend([
                OpenStrategyAuthoringTool(
                    storage=strategy_storage,
                    repository=strategy_definition_repository,
                    compiler=_compiler,
                ),
                CancelStrategyAuthoringTool(
                    storage=strategy_storage,
                    repository=strategy_definition_repository,
                    compiler=_compiler,
                ),
                CompileStrategyDraftTool(
                    storage=strategy_storage,
                    repository=strategy_definition_repository,
                    compiler=_compiler,
                ),
                FinalizeStrategyAuthoringTool(
                    storage=strategy_storage,
                    repository=strategy_definition_repository,
                    compiler=_compiler,
                ),
            ])
    return OperationRegistry(tools)


__all__ = ["build_cli_tool_registry"]
