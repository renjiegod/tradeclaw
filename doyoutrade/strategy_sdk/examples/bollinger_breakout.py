"""Bollinger breakout Strategy — band membership drives target state.

- ``close > upper`` → BUY  (breakout long).
- ``close < lower`` → SELL (force flat — momentum rolled over).
- Otherwise        → HOLD (PositionManager keeps current state).

Like :mod:`rsi_mean_reversion`, the in-band region intentionally leaves
the symbol's position unchanged rather than oscillating around the
midline.

Run::

    python -m doyoutrade.strategy_sdk.examples.bollinger_breakout
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


class BollingerBreakoutStrategy(Strategy):
    """Long on upper-band breakouts, flat on lower-band breakdowns."""

    name: ClassVar[str] = "bollinger_breakout"
    timeframe: ClassVar[str] = "1d"
    # Bollinger warm-up: rolling window padded once for non-NaN reads.
    startup_history: ClassVar[int] = 20 * 2

    window = IntParameter(10, 40, default=20)
    num_std = DecimalParameter(1.0, 3.0, default=2.0, decimals=1)

    def populate_indicators(self, df: pd.DataFrame, ctx) -> pd.DataFrame:
        bands = indicators.bollinger(
            df["close"], window=self.window.value, num_std=self.num_std.value
        )
        df["bb_upper"] = bands.upper
        df["bb_lower"] = bands.lower
        return df

    def on_bar(self, df: pd.DataFrame, ctx) -> Signal:
        last = df.iloc[-1]
        close = last["close"]
        upper = last["bb_upper"]
        lower = last["bb_lower"]
        if pd.isna(upper) or pd.isna(lower):
            return Signal.hold(tag="warmup")
        if close > upper:
            return Signal.buy(tag="bollinger_upper_breakout")
        if close < lower and ctx.position.is_long:
            return Signal.sell(tag="bollinger_lower_breakdown")
        return Signal.hold()


def _main() -> None:
    from doyoutrade.strategy_sdk.examples._synthetic import make_ohlcv

    strategy = BollingerBreakoutStrategy()
    samples = {
        "DEMO.PUMP": make_ohlcv("DEMO.PUMP", bars=120, drift=0.004, seed=20),
        "DEMO.DUMP": make_ohlcv("DEMO.DUMP", bars=120, drift=-0.004, seed=21),
        "DEMO.SIDEWAYS": make_ohlcv("DEMO.SIDEWAYS", bars=120, drift=0.0, seed=22),
    }
    print(f"startup_history={strategy.startup_history}")
    for symbol, df in samples.items():
        populated = strategy.populate_indicators(df.copy(), ctx=None)  # type: ignore[arg-type]
        last = populated.iloc[-1]
        print(
            f"  {symbol:>14}: close={last['close']:7.2f} "
            f"upper={float(last['bb_upper']):7.2f} "
            f"lower={float(last['bb_lower']):7.2f}"
        )


if __name__ == "__main__":
    _main()


__all__ = ["BollingerBreakoutStrategy"]
