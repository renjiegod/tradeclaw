"""One Yang Three Yin scorer / 一阳夹三阴打分器.

Ported from the DSA `one_yang_three_yin` skill. A hit (Signal.buy) fires on
the 5-bar consolidation-end pattern:

  * Day 1  — a large bullish candle (body >= body_min of open).
  * Days 2-4 — three small / bearish bars that neither break below day-1's
    open nor close outside day-1's body (a tight pause), preferably on
    shrinking volume.
  * Day 5  — another bullish candle closing above day-1's close.

Screener usage (code-screen mode; BUY == match):
    doyoutrade-cli stock screen --universe-file /tmp/u.txt \
      --scorer-file examples/stockpick_scorers/one_yang_three_yin_scorer.py \
      --top-k 20
"""

from __future__ import annotations

from doyoutrade.strategy_sdk import (
    Strategy as BaseStrategy,
    Signal,
    DecimalParameter,
)


class Strategy(BaseStrategy):
    name = "stockpick_one_yang_three_yin"
    timeframe = "1d"
    startup_history = 10

    body_min = DecimalParameter(0.01, 0.05, default=0.02, decimals=3)

    def populate_indicators(self, df, ctx):
        return df

    def on_bar(self, df, ctx) -> Signal:
        if len(df) < 5:
            return Signal.hold(tag="insufficient_bars")

        d1 = df.iloc[-5]
        mids = [df.iloc[-4], df.iloc[-3], df.iloc[-2]]
        d5 = df.iloc[-1]

        o1 = float(d1["open"])
        c1 = float(d1["close"])
        if o1 <= 0:
            return Signal.hold(tag="invalid_open")

        big_yang = c1 > o1 and (c1 - o1) / o1 >= float(self.body_min.value)
        if not big_yang:
            return Signal.hold(tag="no_leading_yang")

        # Each of days 2-4 must hold above day-1's open and close within body.
        mids_ok = all(
            float(b["low"]) >= o1 and o1 <= float(b["close"]) <= c1 for b in mids
        )
        # Optional: shrinking volume through the pause.
        d1_vol = float(d1["volume"])
        shrinking = d1_vol > 0 and all(float(b["volume"]) < d1_vol for b in mids)

        d5_ok = float(d5["close"]) > float(d5["open"]) and float(d5["close"]) > c1

        if big_yang and mids_ok and d5_ok:
            tag = "one_yang_three_yin+shrink" if shrinking else "one_yang_three_yin"
            return Signal.buy(tag=tag)
        if big_yang and mids_ok:
            return Signal.hold(tag="awaiting_confirming_yang")
        return Signal.hold(tag="pattern_incomplete")
