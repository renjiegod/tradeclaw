from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from doyoutrade.core.models import TaskBudgetSnapshot

if TYPE_CHECKING:
    from doyoutrade.runtime.cycle_task import CycleTask


@dataclass
class CycleRunState:
    """Single trading-cycle execution context (state-machine payload).

    Created at the start of ``run_cycle`` and passed through phase boundaries
    and signal-generator / execution hooks so downstream code can correlate
    logs and side effects with ``run_id``, trace context, and the owning
    task instance.
    """

    run_id: str
    trace_id: str
    task_id: str
    agent_name: str
    phase: str = ""
    cycle_task: CycleTask | None = None
    # Logical clock for historical / simulated runs (stored as cycle_runs.cycle_time_utc).
    clock_mode: str = "wall"
    cycle_time_utc: datetime | None = None
    market_profile: str = "cn_a_share"
    settlement_mode: str = "t0"
    #: Resolved A-share fee model (doyoutrade.execution.fees.AShareFeeModel) or
    #: None when no fee_config is set (default → no transaction cost). Pushed
    #: onto the mock ledger each cycle, mirroring ``settlement_mode``.
    fee_model: Any = None
    #: Task-level logical budget view derived from persisted fills at
    #: cycle-start. ``None`` when the task has no task-budget caps configured.
    task_budget_snapshot: TaskBudgetSnapshot | None = None

    def enter_phase(self, phase: str) -> None:
        self.phase = phase

    @property
    def cycle_time(self) -> datetime | None:
        """Preferred alias for logical cycle clock."""
        return self.cycle_time_utc

    @cycle_time.setter
    def cycle_time(self, value: datetime | None) -> None:
        self.cycle_time_utc = value
