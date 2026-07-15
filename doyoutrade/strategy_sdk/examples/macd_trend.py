"""MACD trend-following Strategy — buy while histogram is positive.

Long when ``macd_hist > 0`` (MACD line above signal line); flat when it
goes non-positive. Comparing the *level* of the histogram — not the
cross event — is what keeps this a real position-state signal: the
PositionManager diffs the strategy's decision against current position
every cycle, so emitting BUY only on the cross bar would produce a
1-cycle holding period.

Run::

    python -m doyoutrade.strategy_sdk.examples.macd_trend
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


class MACDTrendStrategy(Strategy):
    """Long while MACD histogram > 0, flat while MACD histogram <= 0."""

    name: ClassVar[str] = "macd_trend"
    timeframe: ClassVar[str] = "1d"
    # MACD warm-up: slow + signal + slow*3 (per indicators.py docstring).
    startup_history: ClassVar[int] = 26 + 9 + 26 * 3

    fast = IntParameter(5, 20, default=12)
    slow = IntParameter(20, 60, default=26)
    signal = IntParameter(5, 20, default=9)

    def populate_indicators(self, df: pd.DataFrame, ctx) -> pd.DataFrame:
        result = indicators.macd(
            df["close"],
            fast=self.fast.value,
            slow=self.slow.value,
            signal=self.signal.value,
        )
        df["macd_hist"] = result.hist
        return df

    def on_bar(self, df: pd.DataFrame, ctx) -> Signal:
        last_hist = df["macd_hist"].iloc[-1]
        if pd.isna(last_hist):
            return Signal.hold(tag="warmup")
        if last_hist > 0:
            return Signal.buy(tag="macd_hist_positive")
        if ctx.position.is_long:
            return Signal.sell(tag="macd_hist_non_positive")
        return Signal.hold()


def _main() -> None:
    from doyoutrade.strategy_sdk.examples._synthetic import make_ohlcv

    strategy = MACDTrendStrategy()
    samples = {
        "DEMO.UP": make_ohlcv("DEMO.UP", bars=200, drift=0.0015, seed=1),
        "DEMO.DOWN": make_ohlcv("DEMO.DOWN", bars=200, drift=-0.0015, seed=2),
        "DEMO.FLAT": make_ohlcv("DEMO.FLAT", bars=200, drift=0.0, seed=3),
    }
    print(f"startup_history={strategy.startup_history}")
    for symbol, df in samples.items():
        populated = strategy.populate_indicators(df.copy(), ctx=None)  # type: ignore[arg-type]
        last_hist = populated["macd_hist"].iloc[-1]
        print(
            f"  {symbol:>10}: last_close={df['close'].iloc[-1]:8.2f} "
            f"macd_hist={float(last_hist):8.4f}"
        )


if __name__ == "__main__":
    _main()


__all__ = ["MACDTrendStrategy"]
