"""Volume Breakout scorer / 放量突破打分器.

Ported from the DSA `volume_breakout` skill. A hit (Signal.buy) fires when
the close breaks above the prior N-day high on volume >= vol_min x the
recent average, with a strong (upper-range) close.

Screener usage (code-screen mode; BUY == match):
    doyoutrade-cli stock screen --universe-file /tmp/u.txt \
      --scorer-file examples/stockpick_scorers/volume_breakout_scorer.py \
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
    patterns,
)


class Strategy(BaseStrategy):
    name = "stockpick_volume_breakout"
    timeframe = "1d"
    startup_history = 45

    lookback = IntParameter(10, 40, default=20, optimize=True)
    vol_window = IntParameter(5, 20, default=5)
    vol_min = DecimalParameter(1.5, 3.0, default=2.0, decimals=2)
    close_strength = DecimalParameter(0.5, 0.9, default=0.7, decimals=2)

    def populate_indicators(self, df, ctx):
        df["prior_hi"] = patterns.prior_high(df["high"], self.lookback.value)
        df["broke_hi"] = patterns.broke_above(df["close"], df["prior_hi"])
        df["vol_ratio"] = indicators.volume_ratio(df["volume"], self.vol_window.value)
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        if pd.isna(last["prior_hi"]) or pd.isna(last["vol_ratio"]):
            return Signal.hold(tag="warmup")

        broke = bool(last["broke_hi"])
        vol_ok = bool(last["vol_ratio"] >= float(self.vol_min.value))

        rng = float(last["high"]) - float(last["low"])
        strong_close = (
            rng > 0
            and (float(last["close"]) - float(last["low"])) / rng
            >= float(self.close_strength.value)
        )

        if broke and vol_ok and strong_close:
            return Signal.buy(tag="volume_breakout")
        if broke and vol_ok:
            return Signal.hold(tag="breakout_weak_close")
        if broke:
            return Signal.hold(tag="breakout_no_volume")
        return Signal.hold(tag="no_breakout")
