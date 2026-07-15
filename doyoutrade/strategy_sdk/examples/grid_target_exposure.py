"""Exposure-grid mean-reversion Strategy.

This example demonstrates the new ``Signal.target_exposure(...)`` contract:
the strategy maps the current price band to a desired post-cycle inventory
level, and PositionManager rebalances toward that exposure.

Grid shape:

- price >= anchor                  -> 0%
- anchor - 1 * step <= price < anchor -> 25%
- anchor - 2 * step <= price < anchor - 1 * step -> 50%
- anchor - 3 * step <= price < anchor - 2 * step -> 75%
- price < anchor - 3 * step       -> 100%

Run::

    python -m doyoutrade.strategy_sdk.examples.grid_target_exposure
"""

from __future__ import annotations

import math
from typing import ClassVar

import pandas as pd

from doyoutrade.strategy_sdk import (
    DecimalParameter,
    IntParameter,
    Signal,
    Strategy,
    indicators,
)


class GridTargetExposureStrategy(Strategy):
    """Map price deviation bands to target exposure levels."""

    name: ClassVar[str] = "grid_target_exposure"
    timeframe: ClassVar[str] = "1d"
    startup_history: ClassVar[int] = 60

    anchor_window = IntParameter(20, 120, default=60)
    grid_step = DecimalParameter(0.01, 0.08, default=0.03, decimals=3)
    max_levels = IntParameter(2, 8, default=4)

    def populate_indicators(self, df: pd.DataFrame, ctx) -> pd.DataFrame:
        df["anchor"] = indicators.sma(df["close"], self.anchor_window.value)
        df["deviation"] = (df["close"] - df["anchor"]) / df["anchor"]
        return df

    def on_bar(self, df: pd.DataFrame, ctx) -> Signal:
        last = df.iloc[-1]
        anchor = last["anchor"]
        deviation = last["deviation"]
        if pd.isna(anchor) or pd.isna(deviation):
            return Signal.hold(tag="warmup")

        if deviation >= 0:
            return Signal.target_exposure(target=0.0, tag="grid_l0")

        levels = min(
            self.max_levels.value,
            math.floor(abs(float(deviation)) / self.grid_step.value) + 1,
        )
        target = levels / self.max_levels.value
        return Signal.target_exposure(target=target, tag=f"grid_l{levels}")


def _main() -> None:
    from doyoutrade.strategy_sdk.examples._synthetic import make_ohlcv

    strategy = GridTargetExposureStrategy()
    samples = {
        "DEMO.REVERT": make_ohlcv("DEMO.REVERT", bars=160, drift=-0.0004, seed=21),
        "DEMO.CRASH": make_ohlcv("DEMO.CRASH", bars=160, drift=-0.0020, seed=22),
        "DEMO.RECOVER": make_ohlcv("DEMO.RECOVER", bars=160, drift=0.0012, seed=23),
    }
    print(
        "grid_step="
        f"{strategy.grid_step.value:.3f} max_levels={strategy.max_levels.value} "
        f"startup_history={strategy.startup_history}"
    )
    for symbol, df in samples.items():
        populated = strategy.populate_indicators(df.copy(), ctx=None)  # type: ignore[arg-type]
        signal = strategy.on_bar(populated, ctx=None)  # type: ignore[arg-type]
        last = populated.iloc[-1]
        print(
            f"  {symbol:>12}: close={last['close']:8.2f} "
            f"anchor={last['anchor']:8.2f} "
            f"deviation={float(last['deviation']):7.3%} "
            f"signal={signal.to_dict()}"
        )


if __name__ == "__main__":
    _main()


__all__ = ["GridTargetExposureStrategy"]
