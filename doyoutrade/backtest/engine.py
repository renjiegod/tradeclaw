from __future__ import annotations

import asyncio
from dataclasses import dataclass

from doyoutrade.backtest.replay import ReplayClock


@dataclass
class BacktestRunResult:
    bars_processed: int
    submitted_count: int
    vetoed_count: int
    initial_equity: float | None = None
    final_equity: float | None = None
    total_return: float | None = None


class BarDrivenBacktestRunner:
    def __init__(self, timeline, on_bar):
        self.clock = ReplayClock(timeline)
        self.on_bar = on_bar

    def run(self) -> BacktestRunResult:
        bars_processed = 0
        submitted_count = 0
        vetoed_count = 0
        initial_equity: float | None = None
        final_equity: float | None = None

        while True:
            as_of_time = self.clock.current_time
            output = self.on_bar(as_of_time) or {}

            submitted_count += int(output.get("submitted", output.get("submitted_count", 0)))
            vetoed_count += int(output.get("vetoed", output.get("vetoed_count", 0)))
            eq = output.get("equity")
            if isinstance(eq, (int, float)):
                fv = float(eq)
                if initial_equity is None:
                    initial_equity = fv
                final_equity = fv
            bars_processed += 1

            if not self.clock.step():
                break

        total_return: float | None = None
        if initial_equity is not None and final_equity is not None and initial_equity > 0:
            total_return = (final_equity - initial_equity) / initial_equity

        return BacktestRunResult(
            bars_processed=bars_processed,
            submitted_count=submitted_count,
            vetoed_count=vetoed_count,
            initial_equity=initial_equity,
            final_equity=final_equity,
            total_return=total_return,
        )

    async def arun(self) -> BacktestRunResult:
        """Async variant: supports async on_bar callbacks (e.g. when strategy invoke is async)."""
        bars_processed = 0
        submitted_count = 0
        vetoed_count = 0
        initial_equity: float | None = None
        final_equity: float | None = None

        while True:
            as_of_time = self.clock.current_time
            output = self.on_bar(as_of_time)
            if asyncio.iscoroutine(output):
                output = await output
            output = output or {}

            submitted_count += int(output.get("submitted", output.get("submitted_count", 0)))
            vetoed_count += int(output.get("vetoed", output.get("vetoed_count", 0)))
            eq = output.get("equity")
            if isinstance(eq, (int, float)):
                fv = float(eq)
                if initial_equity is None:
                    initial_equity = fv
                final_equity = fv
            bars_processed += 1

            if not self.clock.step():
                break

        total_return: float | None = None
        if initial_equity is not None and final_equity is not None and initial_equity > 0:
            total_return = (final_equity - initial_equity) / initial_equity

        return BacktestRunResult(
            bars_processed=bars_processed,
            submitted_count=submitted_count,
            vetoed_count=vetoed_count,
            initial_equity=initial_equity,
            final_equity=final_equity,
            total_return=total_return,
        )
