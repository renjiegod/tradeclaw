"""RSI mean-reversion Strategy — hysteresis band entry / exit.

- ``RSI < oversold``  → BUY  (target_state long).
- ``RSI > overbought``→ SELL (close long when held).
- Otherwise          → HOLD (position unchanged).

The "do nothing inside the band" gap matches the diff-against-current
contract of PositionManager: between cycles where the strategy emits
HOLD, the most recent BUY/SELL still drives the actual position.

Run::

    python -m doyoutrade.strategy_sdk.examples.rsi_mean_reversion
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd

from doyoutrade.strategy_sdk import (
    DecimalParameter,
    IntParameter,
    Signal,
    Strategy,
    indicators,
)


class RSIMeanReversionStrategy(Strategy):
    """Long below ``oversold`` RSI, flat above ``overbought``, hold otherwise."""

    name: ClassVar[str] = "rsi_mean_reversion"
    timeframe: ClassVar[str] = "1d"
    # RSI warm-up: period * 4 (per indicators.py docstring).
    startup_history: ClassVar[int] = 14 * 4

    period = IntParameter(5, 30, default=14)
    oversold = DecimalParameter(10.0, 40.0, default=30.0, decimals=1)
    overbought = DecimalParameter(60.0, 90.0, default=70.0, decimals=1)

    def populate_indicators(self, df: pd.DataFrame, ctx) -> pd.DataFrame:
        df["rsi"] = indicators.rsi(df["close"], period=self.period.value)
        return df

    def on_bar(self, df: pd.DataFrame, ctx) -> Signal:
        rsi = df["rsi"].iloc[-1]
        if pd.isna(rsi):
            return Signal.hold(tag="warmup")
        if rsi < self.oversold.value:
            return Signal.buy(tag=f"rsi_oversold_{int(rsi)}")
        if rsi > self.overbought.value and ctx.position.is_long:
            return Signal.sell(tag=f"rsi_overbought_{int(rsi)}")
        return Signal.hold()


def _main() -> None:
    from doyoutrade.strategy_sdk.examples._synthetic import make_ohlcv

    strategy = RSIMeanReversionStrategy()
    samples = {
        "DEMO.RIP": make_ohlcv("DEMO.RIP", bars=120, drift=0.005, seed=10),
        "DEMO.DIP": make_ohlcv("DEMO.DIP", bars=120, drift=-0.005, seed=11),
        "DEMO.CHOPPY": make_ohlcv("DEMO.CHOPPY", bars=120, drift=0.0, seed=12),
    }
    print(f"startup_history={strategy.startup_history}")
    for symbol, df in samples.items():
        populated = strategy.populate_indicators(df.copy(), ctx=None)  # type: ignore[arg-type]
        rsi_last = populated["rsi"].iloc[-1]
        print(f"  {symbol:>12}: rsi={float(rsi_last):6.2f}  bars={len(df)}")


if __name__ == "__main__":
    _main()


__all__ = ["RSIMeanReversionStrategy"]
