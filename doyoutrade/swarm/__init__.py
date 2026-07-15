"""Swarm 多智能体编排子系统。

把一个复杂的投研/交易任务拆给多个有不同角色、工具、skill 的 worker，按 DAG
依赖图并行/串行协作，最后汇总成结论。worker 直接复用 doyoutrade 的
``AssistantService``，持久化复用 SQLAlchemy。

移植自 Vibe-Trading 的 Swarm Teams（线程+文件存储 → asyncio+SQLAlchemy）。
"""

from __future__ import annotations

from doyoutrade.swarm.models import (
    RunStatus,
    SwarmAgentSpec,
    SwarmEvent,
    SwarmRun,
    SwarmTask,
    TaskStatus,
    WorkerResult,
    WorkerStatus,
)
from doyoutrade.swarm.presets import (
    build_run_from_preset,
    inspect_preset,
    list_presets,
    load_preset,
)

__all__ = [
    "RunStatus",
    "SwarmAgentSpec",
    "SwarmEvent",
    "SwarmRun",
    "SwarmTask",
    "TaskStatus",
    "WorkerResult",
    "WorkerStatus",
    "build_run_from_preset",
    "inspect_preset",
    "list_presets",
    "load_preset",
]
