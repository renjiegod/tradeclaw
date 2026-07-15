"""Deviation-guard Strategy — a discipline-reminder rule template.

This is the reference rule for the ``deviation_monitor`` cron executor: it does
NOT trade. It encodes "my held stock is deviating from the plan" as a
``Signal.sell`` (read by the executor as "plan violated → remind me") and
"still on plan" as ``Signal.hold``. Authoring the deviation logic as an ordinary
Strategy SDK strategy is what makes the check fully flexible — any indicator,
any multi-bar sequence, any candle math — while keeping the AST/compile safety
net (``sdk validate``).

When run intraday by the executor, ``df.iloc[-1]`` is TODAY's *forming* bar:
the live ~14:50 quote spliced onto warehouse history by
:class:`~doyoutrade.strategy_sdk.live_overlay.LiveBarOverlayHistoryFetcher`, so
the rules below see the live price, open, high, low, and partial volume.

Five plan-deviation rules (any one trips the reminder):

  1. 破5日线         — close below the 5-day moving average.
  2. 大阴线          — a big bearish candle (real body a large share of range)
                       closing down on the day.
  3. 连阳被破坏      — a run of consecutive bullish bars is broken by a bearish
                       close.
  4. 放量下跌        — a down close on volume well above its recent average.
  5. 跌破成本        — price drops below the position's cost basis (only when a
                       position is actually held).

Copy this into a strategy definition (``sd-…``) and adjust the thresholds, then
schedule it with ``deviation_monitor``.

Run::

    python -m doyoutrade.strategy_sdk.examples.deviation_guard
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


class DeviationGuardStrategy(Strategy):
    """Emit SELL when a held position deviates from the plan, else HOLD."""

    name: ClassVar[str] = "deviation_guard"
    timeframe: ClassVar[str] = "1d"
    startup_history: ClassVar[int] = 30

    # Tunable via the executor's ``parameter_overrides``.
    ma_period = IntParameter(3, 60, default=5)
    bullish_streak = IntParameter(2, 10, default=3)
    volume_lookback = IntParameter(3, 30, default=5)

    # Fixed thresholds (class constants — adjust in your own copy as needed).
    BEARISH_BODY_RATIO: ClassVar[float] = 0.6  # real body / range to count as 大阴线
    VOLUME_SPIKE_MULT: ClassVar[float] = 2.0   # vol >= mult * recent average

    def populate_indicators(self, df: pd.DataFrame, ctx) -> pd.DataFrame:
        df["ma"] = indicators.sma(df["close"], window=self.ma_period.value)
        return df

    def on_bar(self, df: pd.DataFrame, ctx) -> Signal:
        last = df.iloc[-1]
        close = float(last["close"])
        open_ = float(last["open"])
        high = float(last["high"])
        low = float(last["low"])

        triggered: list[str] = []
        diagnostics: dict[str, object] = {"close": round(close, 4)}

        # 1) 破5日线 — close below the moving average.
        ma = last["ma"]
        if not pd.isna(ma):
            diagnostics["ma"] = round(float(ma), 4)
            if close < float(ma):
                triggered.append("break_ma")

        # Down-day reference (vs previous close), shared by several rules.
        down_day = False
        if len(df) >= 2:
            prev_close = float(df["close"].iloc[-2])
            diagnostics["prev_close"] = round(prev_close, 4)
            down_day = close < prev_close

        # 2) 大阴线 — bearish candle with a large real body on a down day.
        rng = high - low
        body = abs(close - open_)
        body_ratio = (body / rng) if rng > 0 else 0.0
        diagnostics["body_ratio"] = round(body_ratio, 4)
        if close < open_ and body_ratio >= self.BEARISH_BODY_RATIO and down_day:
            triggered.append("bearish_engulf")

        # 3) 连阳被破坏 — a streak of bullish bars broken by today's bearish close.
        streak = int(self.bullish_streak.value)
        if len(df) >= streak + 1 and close < open_:
            prior = df.iloc[-(streak + 1):-1]
            if bool((prior["close"] > prior["open"]).all()):
                triggered.append("bullish_streak_broken")

        # 4) 放量下跌 — down close on volume well above its recent average.
        lookback = int(self.volume_lookback.value)
        if len(df) >= lookback + 1 and down_day:
            recent = df["volume"].iloc[-(lookback + 1):-1]
            avg_vol = float(recent.mean())
            cur_vol = float(last["volume"])
            if avg_vol > 0:
                ratio = cur_vol / avg_vol
                diagnostics["volume_ratio"] = round(ratio, 2)
                if ratio >= self.VOLUME_SPIKE_MULT:
                    triggered.append("volume_spike_down")

        # 5) 跌破成本 — price below the position's cost basis (only when held).
        if ctx.position.is_long and ctx.position.cost_price > 0:
            cost = float(ctx.position.cost_price)
            diagnostics["cost_price"] = round(cost, 4)
            if close < cost:
                triggered.append("below_cost")

        if triggered:
            tag = "+".join(sorted(triggered))
            return Signal.sell(
                tag=tag,
                rationale=_rationale_for(triggered),
                diagnostics=diagnostics,
                exit_reason="signal",
            )
        return Signal.hold(tag="on_plan", diagnostics=diagnostics)


_RULE_TEXT: dict[str, str] = {
    "break_ma": "已跌破均线",
    "bearish_engulf": "走出大阴线",
    "bullish_streak_broken": "连阳被破坏",
    "volume_spike_down": "放量下跌",
    "below_cost": "跌破买入成本",
}


def _rationale_for(triggered: list[str]) -> str:
    return "、".join(_RULE_TEXT.get(t, t) for t in sorted(triggered))


def _main() -> None:
    from doyoutrade.strategy_sdk.examples._synthetic import make_ohlcv

    strategy = DeviationGuardStrategy()
    samples = {
        "DEMO.UP": make_ohlcv("DEMO.UP", bars=120, drift=0.0015, seed=1),
        "DEMO.DOWN": make_ohlcv("DEMO.DOWN", bars=120, drift=-0.0015, seed=2),
    }
    print(f"startup_history={strategy.startup_history}")
    for symbol, df in samples.items():
        populated = strategy.populate_indicators(df.copy(), ctx=None)  # type: ignore[arg-type]
        last_ma = populated["ma"].iloc[-1]
        print(
            f"  {symbol:>10}: last_close={df['close'].iloc[-1]:8.2f} "
            f"ma={float(last_ma):8.2f}"
        )


if __name__ == "__main__":
    _main()


__all__ = ["DeviationGuardStrategy"]
