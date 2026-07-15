"""Shrink Volume Pullback scorer / 缩量回踩打分器.

Ported from the DSA `shrink_pullback` skill. A hit (Signal.buy) fires when
the stock is in a bullish MA stack (MA5 > MA10 > MA20), price has pulled back
to within `near_ma` of MA5, and the pullback is on shrinking volume
(vol_ratio below `vol_max`).

Screener usage (code-screen mode; BUY == match):
    doyoutrade-cli stock screen --universe-file /tmp/u.txt \
      --scorer-file examples/stockpick_scorers/shrink_pullback_scorer.py \
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
    name = "stockpick_shrink_pullback"
    timeframe = "1d"
    startup_history = 30

    fast = IntParameter(3, 10, default=5, optimize=True)
    mid = IntParameter(8, 15, default=10, optimize=True)
    slow = IntParameter(18, 30, default=20, optimize=True)
    vol_window = IntParameter(5, 20, default=5)
    vol_max = DecimalParameter(0.5, 1.0, default=0.7, decimals=2)
    near_ma = DecimalParameter(0.01, 0.05, default=0.02, decimals=3)

    def populate_indicators(self, df, ctx):
        df["ma_fast"] = indicators.sma(df["close"], self.fast.value)
        df["ma_mid"] = indicators.sma(df["close"], self.mid.value)
        df["ma_slow"] = indicators.sma(df["close"], self.slow.value)
        df["vol_ratio"] = indicators.volume_ratio(df["volume"], self.vol_window.value)
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        if pd.isna(last["ma_slow"]) or pd.isna(last["vol_ratio"]):
            return Signal.hold(tag="warmup")

        ma_fast = float(last["ma_fast"])
        uptrend = bool(ma_fast > float(last["ma_mid"]) > float(last["ma_slow"]))
        near = ma_fast > 0 and abs(float(last["close"]) - ma_fast) / ma_fast <= float(
            self.near_ma.value
        )
        shrink = bool(last["vol_ratio"] < float(self.vol_max.value))

        if uptrend and near and shrink:
            return Signal.buy(tag="shrink_pullback_ma5")
        if uptrend and near:
            return Signal.hold(tag="pullback_no_shrink")
        if uptrend:
            return Signal.hold(tag="uptrend_not_at_ma")
        return Signal.hold(tag="no_uptrend")
