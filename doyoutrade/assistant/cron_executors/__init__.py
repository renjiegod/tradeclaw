"""Cron job pre-action executors and registry.

See `docs/superpowers/specs/2026-05-14-cron-driven-strategy-execution-design.md`
§5 for the contract. Each executor implements :class:`JobPreActionExecutor` and
is registered against a unique ``kind`` string. New kinds are added in a new
module under this package and registered at bootstrap.
"""

from .agent_chat_reply import AgentChatReplyExecutor
from .daily_review import DailyReviewExecutor
from .deviation_monitor import DeviationMonitorExecutor, LoadedStrategy
from .base import (
    JobExecutorRegistry,
    JobPreActionExecutor,
    JobRunContext,
    JobTaskExecutor,
    JobTaskRegistry,
    PreActionResult,
    TaskResult,
)
from .noop import NoopExecutor
from .stock_report import StockReportExecutor

__all__ = [
    "AgentChatReplyExecutor",
    "DailyReviewExecutor",
    "DeviationMonitorExecutor",
    "StockReportExecutor",
    "LoadedStrategy",
    "JobExecutorRegistry",
    "JobPreActionExecutor",
    "JobRunContext",
    "JobTaskExecutor",
    "JobTaskRegistry",
    "PreActionResult",
    "TaskResult",
    "NoopExecutor",
]
