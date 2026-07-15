"""Bottom Volume Surge scorer / 底部放量打分器.

Ported from the DSA `bottom_volume` skill. A hit (Signal.buy) fires when,
after an extended decline (>= decline_min off the prior N-day high), a
volume surge (>= vol_min x recent average) coincides with a bullish candle;
a long lower shadow strengthens the tag.

This is a reversal probe — inherently higher risk than trend follows.

Screener usage (code-screen mode; BUY == match):
    doyoutrade-cli stock screen --universe-file /tmp/u.txt \
      --scorer-file examples/stockpick_scorers/bottom_volume_scorer.py \
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
    name = "stockpick_bottom_volume"
    timeframe = "1d"
    startup_history = 45

    high_window = IntParameter(10, 40, default=20, optimize=True)
    decline_min = DecimalParameter(0.10, 0.30, default=0.15, decimals=2)
    vol_window = IntParameter(5, 20, default=5)
    vol_min = DecimalParameter(2.0, 4.0, default=3.0, decimals=2)
    shadow_min = DecimalParameter(0.20, 0.50, default=0.30, decimals=2)

    def populate_indicators(self, df, ctx):
        df["prior_hi"] = patterns.prior_high(df["high"], self.high_window.value)
        df["vol_ratio"] = indicators.volume_ratio(df["volume"], self.vol_window.value)
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        if pd.isna(last["prior_hi"]) or pd.isna(last["vol_ratio"]):
            return Signal.hold(tag="warmup")

        prior_hi = float(last["prior_hi"])
        close = float(last["close"])
        open_ = float(last["open"])
        high = float(last["high"])
        low = float(last["low"])

        declined = prior_hi > 0 and (prior_hi - close) / prior_hi >= float(
            self.decline_min.value
        )
        vol_surge = bool(last["vol_ratio"] >= float(self.vol_min.value))
        bullish = close > open_

        rng = high - low
        long_lower_shadow = (
            rng > 0
            and (min(open_, close) - low) / rng >= float(self.shadow_min.value)
        )

        if declined and vol_surge and bullish:
            tag = (
                "bottom_volume+long_lower_shadow"
                if long_lower_shadow
                else "bottom_volume"
            )
            return Signal.buy(tag=tag)
        if declined and vol_surge:
            return Signal.hold(tag="volume_surge_no_yang")
        if declined:
            return Signal.hold(tag="declined_no_volume")
        return Signal.hold(tag="no_decline")
