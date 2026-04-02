from __future__ import annotations

from dataclasses import dataclass

from tradeclaw.backtest.replay import ReplayClock


@dataclass
class BacktestRunResult:
    bars_processed: int
    submitted_count: int
    vetoed_count: int


class BarDrivenBacktestRunner:
    def __init__(self, timeline, on_bar):
        self.clock = ReplayClock(timeline)
        self.on_bar = on_bar

    def run(self) -> BacktestRunResult:
        bars_processed = 0
        submitted_count = 0
        vetoed_count = 0

        while True:
            as_of_time = self.clock.current_time
            output = self.on_bar(as_of_time) or {}

            submitted_count += int(output.get("submitted", output.get("submitted_count", 0)))
            vetoed_count += int(output.get("vetoed", output.get("vetoed_count", 0)))
            bars_processed += 1

            if not self.clock.step():
                break

        return BacktestRunResult(
            bars_processed=bars_processed,
            submitted_count=submitted_count,
            vetoed_count=vetoed_count,
        )
