"""Dual-SMA target-state Strategy — compare levels, not cross events.

Long while ``SMA(close, fast) > SMA(close, slow)``, flat otherwise. We
deliberately compare the *current bar's* SMA levels rather than the
``crossed_above`` event: PositionManager diffs the strategy's per-cycle
decision against the live portfolio, so a level comparison produces a
stable target-state, whereas a cross-only decision would force a
1-cycle holding period.

Run::

    python -m doyoutrade.strategy_sdk.examples.dual_sma_crossover
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd

from doyoutrade.strategy_sdk import (
    IntParameter,
    Signal,
    Strategy,
    indicators,
)


class DualSMACrossoverStrategy(Strategy):
    """Long while fast SMA is above slow SMA, flat otherwise."""

    name: ClassVar[str] = "dual_sma_crossover"
    timeframe: ClassVar[str] = "1d"
    # slow_window + 1 so .iloc[-1] always sees a non-NaN slow SMA.
    startup_history: ClassVar[int] = 51

    fast_window = IntParameter(5, 30, default=20)
    slow_window = IntParameter(30, 100, default=50)

    def populate_indicators(self, df: pd.DataFrame, ctx) -> pd.DataFrame:
        df["sma_fast"] = indicators.sma(df["close"], self.fast_window.value)
        df["sma_slow"] = indicators.sma(df["close"], self.slow_window.value)
        return df

    def on_bar(self, df: pd.DataFrame, ctx) -> Signal:
        last = df.iloc[-1]
        fast = last["sma_fast"]
        slow = last["sma_slow"]
        if pd.isna(fast) or pd.isna(slow):
            return Signal.hold(tag="warmup")
        if fast > slow:
            return Signal.buy(tag="sma_fast_above_slow")
        if ctx.position.is_long:
            return Signal.sell(tag="sma_fast_below_slow")
        return Signal.hold()


def _main() -> None:
    from doyoutrade.strategy_sdk.examples._synthetic import make_ohlcv

    strategy = DualSMACrossoverStrategy()
    samples = {
        "DEMO.TREND_UP": make_ohlcv("DEMO.TREND_UP", bars=150, drift=0.003, seed=30),
        "DEMO.TREND_DOWN": make_ohlcv("DEMO.TREND_DOWN", bars=150, drift=-0.003, seed=31),
        "DEMO.NOISY": make_ohlcv("DEMO.NOISY", bars=150, drift=0.0, volatility=0.03, seed=32),
    }
    print(f"startup_history={strategy.startup_history}")
    for symbol, df in samples.items():
        populated = strategy.populate_indicators(df.copy(), ctx=None)  # type: ignore[arg-type]
        last = populated.iloc[-1]
        print(
            f"  {symbol:>16}: fast={float(last['sma_fast']):7.2f} "
            f"slow={float(last['sma_slow']):7.2f}"
        )


if __name__ == "__main__":
    _main()


__all__ = ["DualSMACrossoverStrategy"]
