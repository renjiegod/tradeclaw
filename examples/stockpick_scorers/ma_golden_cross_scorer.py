"""MA Golden Cross scorer / 均线金叉打分器.

Ported from the DSA `ma_golden_cross` skill. A hit (Signal.buy) fires when
MA5 has crossed above MA10 within the recent window, the short averages are
stacked bullishly, and the cross is confirmed by above-average volume.

Screener usage (code-screen mode; BUY == match):
    doyoutrade-cli stock screen --universe-file /tmp/u.txt \
      --scorer-file examples/stockpick_scorers/ma_golden_cross_scorer.py \
      --rank-by-diagnostic vol_ratio --top-k 20
"""

from __future__ import annotations

import pandas as pd

from doyoutrade.strategy_sdk import (
    Strategy as BaseStrategy,
    Signal,
    IntParameter,
    DecimalParameter,
    indicators,
)


class Strategy(BaseStrategy):
    name = "stockpick_ma_golden_cross"
    timeframe = "1d"
    startup_history = 30

    fast = IntParameter(3, 10, default=5, optimize=True)
    mid = IntParameter(8, 15, default=10, optimize=True)
    slow = IntParameter(18, 30, default=20, optimize=True)
    cross_window = IntParameter(1, 5, default=3)
    vol_window = IntParameter(5, 20, default=5)
    vol_min = DecimalParameter(1.0, 2.0, default=1.2, decimals=2)

    def populate_indicators(self, df, ctx):
        df["ma_fast"] = indicators.sma(df["close"], self.fast.value)
        df["ma_mid"] = indicators.sma(df["close"], self.mid.value)
        df["ma_slow"] = indicators.sma(df["close"], self.slow.value)
        df["golden"] = indicators.crossed_above(df["ma_fast"], df["ma_mid"])
        df["vol_ratio"] = indicators.volume_ratio(df["volume"], self.vol_window.value)
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        if pd.isna(last["ma_slow"]) or pd.isna(last["vol_ratio"]):
            return Signal.hold(tag="warmup")

        window = self.cross_window.value
        recent_golden = bool(df["golden"].iloc[-window:].any())
        bullish_stack = bool(last["ma_fast"] > last["ma_mid"])
        vol_ok = bool(last["vol_ratio"] > float(self.vol_min.value))

        if recent_golden and bullish_stack and vol_ok:
            return Signal.buy(tag="ma_golden_cross+volume")
        if recent_golden and bullish_stack:
            return Signal.hold(tag="golden_cross_no_volume")
        return Signal.hold(tag="no_golden_cross")
