"""Strict inventory-grid Strategy using ``Signal.target_quantity(...)``.

This example demonstrates the share-inventory contract for classic grid
trading: map each price band to an absolute post-cycle share target rather
than a notional exposure target. If the strategy emits the same target
quantity on the next bar and the current share inventory already matches it,
PositionManager does nothing.

A股整手约束在执行层：把任务的 ``settings.position_constraints.lot_size`` 设为
``100``，PositionManager 会把买入 / 部分卖出的股数向下对齐到整手（清仓豁免，可清
零股），并可用 ``rebalance_hysteresis_lots`` 设一个"差不到 N 手不动"的防抖死区。
策略侧只要保证 ``shares_per_level`` 是 ``lot_size`` 的整数倍即可（默认 100 已对齐）。

Grid shape (A-share style, 100-share lots by default):

- price >= anchor                       -> 0 shares
- anchor - 1 * step <= price < anchor  -> 100 shares
- anchor - 2 * step <= price < anchor - 1 * step -> 200 shares
- anchor - 3 * step <= price < anchor - 2 * step -> 300 shares
- price < anchor - 3 * step            -> 400 shares

Run::

    python -m doyoutrade.strategy_sdk.examples.grid_target_quantity
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


class GridTargetQuantityStrategy(Strategy):
    """Map price deviation bands to absolute share inventory levels."""

    name: ClassVar[str] = "grid_target_quantity"
    timeframe: ClassVar[str] = "1d"
    startup_history: ClassVar[int] = 60

    anchor_window = IntParameter(20, 120, default=60)
    grid_step = DecimalParameter(0.01, 0.08, default=0.03, decimals=3)
    max_levels = IntParameter(2, 8, default=4)
    shares_per_level = IntParameter(100, 2000, default=100, step=100)

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
            return Signal.target_quantity(quantity=0, tag="grid_l0")

        levels = min(
            self.max_levels.value,
            math.floor(abs(float(deviation)) / self.grid_step.value) + 1,
        )
        quantity = levels * self.shares_per_level.value
        return Signal.target_quantity(quantity=quantity, tag=f"grid_l{levels}")


def _main() -> None:
    from doyoutrade.strategy_sdk.examples._synthetic import make_ohlcv

    strategy = GridTargetQuantityStrategy()
    samples = {
        "DEMO.REVERT": make_ohlcv("DEMO.REVERT", bars=160, drift=-0.0004, seed=31),
        "DEMO.CRASH": make_ohlcv("DEMO.CRASH", bars=160, drift=-0.0023, seed=32),
        "DEMO.RECOVER": make_ohlcv("DEMO.RECOVER", bars=160, drift=0.0010, seed=33),
    }
    print(
        "grid_step="
        f"{strategy.grid_step.value:.3f} max_levels={strategy.max_levels.value} "
        f"shares_per_level={strategy.shares_per_level.value} "
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


__all__ = ["GridTargetQuantityStrategy"]
