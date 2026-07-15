"""Box Range Oscillation scorer / 箱体震荡打分器.

Ported from the DSA `box_oscillation` skill. The box is defined by the prior
N-day high (box top / resistance) and prior N-day low (box bottom / support),
both excluding the current bar. A hit (Signal.buy) fires when price sits near
the box bottom (within `near_pct`), the box is wide enough to trade
(width >= min_width), and price has not broken below support. Shrinking or
surging volume at the bottom refines the tag.

Screener usage (code-screen mode; BUY == match):
    doyoutrade-cli stock screen --universe-file /tmp/u.txt \
      --scorer-file examples/stockpick_scorers/box_oscillation_scorer.py \
      --top-k 20
"""

from __future__ import annotations

import pandas as pd

from doyoutrade.strategy_sdk import (
    Strategy as BaseStrategy,
    Signal,
    IntParameter,
    DecimalParameter,
    indicators,
    patterns,
)


class Strategy(BaseStrategy):
    name = "stockpick_box_oscillation"
    timeframe = "1d"
    startup_history = 125

    box_window = IntParameter(30, 120, default=60, optimize=True)
    vol_window = IntParameter(5, 20, default=5)
    near_pct = DecimalParameter(0.01, 0.08, default=0.05, decimals=3)
    min_width = DecimalParameter(0.03, 0.15, default=0.05, decimals=3)

    def populate_indicators(self, df, ctx):
        df["box_top"] = patterns.prior_high(df["high"], self.box_window.value)
        df["box_bottom"] = patterns.prior_low(df["low"], self.box_window.value)
        df["vol_ratio"] = indicators.volume_ratio(df["volume"], self.vol_window.value)
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        if pd.isna(last["box_top"]) or pd.isna(last["box_bottom"]):
            return Signal.hold(tag="warmup")

        top = float(last["box_top"])
        bottom = float(last["box_bottom"])
        close = float(last["close"])
        if bottom <= 0 or top <= bottom:
            return Signal.hold(tag="invalid_box")

        width = (top - bottom) / bottom
        if width < float(self.min_width.value):
            return Signal.hold(tag="box_too_narrow")

        if close < bottom:
            return Signal.hold(tag="broke_box_bottom")

        near = float(self.near_pct.value)
        dist_bottom = (close - bottom) / bottom
        dist_top = (top - close) / top

        if dist_bottom <= near:
            vol_ratio = last["vol_ratio"]
            if not pd.isna(vol_ratio) and float(vol_ratio) >= 2.0:
                tag = "box_bottom_volume"
            elif not pd.isna(vol_ratio) and float(vol_ratio) < 1.0:
                tag = "box_bottom_shrink"
            else:
                tag = "box_bottom"
            return Signal.buy(tag=tag)

        if dist_top <= near:
            return Signal.hold(tag="box_top_no_chase")
        return Signal.hold(tag="box_middle")
