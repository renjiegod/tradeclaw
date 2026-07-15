# SDK Indicators Reference

The vetted indicator library `doyoutrade.strategy_sdk.indicators`. Load
when about to write MACD / RSI / ADX / Bollinger / ATR / OBV math, or
when a smoke test fails with NaN propagation.

For the rest of the SDK surface (`Strategy`, `ctx`, `DataRequest`, etc.),
see [`sdk-surface.md`](sdk-surface.md). For indicator *meanings* (what a
high RSI says about the market) see the sibling `technical-basic` skill.
This file is about the **API**.

## Why use the library

Pre-injected as `indicators` in the compile sandbox. **Use these — do not
hand-roll** `close.ewm(...).mean()` chains that reproduce MACD / RSI /
ADX / Bollinger / ATR. A typo in a manually written EMA cascade (e.g.
`signal = ema_fast.ewm(...)` instead of `macd_line.ewm(...)`) compiles
cleanly and produces "looks fine but wrong" numbers — exactly the failure
mode that motivated centralizing the formulas here.

All functions take `pandas.Series` (or DataFrame columns) and return
Series (or a `NamedTuple` of Series for multi-output indicators). Outputs
are index-aligned with input; warm-up bars are `NaN`. Always gate
`.iloc[-1]` reads through `pd.isna(...)`.

> **Reminder**: `pd.isna` lives on the `pandas` module, not on the
> injected `indicators` namespace. Any file that calls `pd.isna(...)`
> (or any other `pd.*` helper) **must** declare `import pandas as pd`
> at the top — the smoke gate raises `NameError: name 'pd' is not
> defined` if it's missing.

## API table

| Call | Returns | startup_history hint |
|---|---|---|
| `indicators.sma(close, window)` | `Series` | `window` |
| `indicators.ema(close, span)` | `Series` | `span * 4` (EWM convergence) |
| `indicators.macd(close, fast=12, slow=26, signal=9)` | `MACDResult(macd, signal, hist)` | `slow + signal + slow * 3` |
| `indicators.rsi(close, period=14)` | `Series` (Wilder) | `period * 4` |
| `indicators.adx(high, low, close, period=14)` | `ADXResult(adx, plus_di, minus_di)` | `period * 4` |
| `indicators.bollinger(close, window=20, num_std=2.0)` | `BollingerResult(upper, middle, lower)` | `window` |
| `indicators.atr(high, low, close, period=14)` | `Series` | `period * 4` |
| `indicators.obv(close, volume)` | `Series` | `2` (uses `.diff()`) |
| `indicators.kdj(high, low, close, n=9, k_smooth=3, d_smooth=3)` | `KDJResult(k, d, j)` | `n + (k+d)*4` |
| `indicators.williams_r(high, low, close, period=14)` | `Series` (-100..0) | `period` |
| `indicators.cci(high, low, close, period=20)` | `Series` | `period` |
| `indicators.roc(close, period=12)` | `Series` (percent) | `period + 1` |
| `indicators.momentum(close, period=10)` | `Series` (price diff) | `period + 1` |
| `indicators.mfi(high, low, close, volume, period=14)` | `Series` (0..100) | `period + 1` |
| `indicators.trix(close, period=15)` | `Series` (percent) | `period*3*4 + 1` |
| `indicators.vwap(high, low, close, volume, window=14)` | `Series` (`window=None` ⇒ anchored) | `window` |
| `indicators.cmf(high, low, close, volume, period=20)` | `Series` (-1..1) | `period` |
| `indicators.ad(high, low, close, volume)` | `Series` | `2` (cumulative) |
| `indicators.volume_ratio(volume, window=20)` | `Series` | `window` |
| `indicators.keltner(high, low, close, ema_window=20, atr_period=10, multiplier=2.0)` | `KeltnerResult(upper, middle, lower)` | `max(ema_window*4, atr_period*4)` |
| `indicators.donchian(high, low, window=20)` | `DonchianResult(upper, middle, lower)` | `window` |
| `indicators.stdev(close, window=20)` | `Series` (ddof=0) | `window` |
| `indicators.hist_volatility(close, window=20, periods_per_year=252)` | `Series` (annualised) | `window + 1` |
| `indicators.wma(close, window)` | `Series` | `window` |
| `indicators.dema(close, span)` | `Series` | `span * 4` |
| `indicators.kama(close, period=10, fast=2, slow=30)` | `Series` | `period * 4` |
| `indicators.supertrend(high, low, close, period=10, multiplier=3.0)` | `SuperTrendResult(supertrend, direction)` | `period * 4` |
| `indicators.psar(high, low, step=0.02, max_step=0.2)` | `Series` | `5` (needs a trend) |
| `indicators.ichimoku(high, low, close, tenkan=9, kijun=26, senkou_b=52)` | `IchimokuResult(tenkan, kijun, senkou_a, senkou_b, chikou)` | `senkou_b + kijun` |
| `indicators.zigzag(close, threshold=0.05)` | `ZigZagResult(pivot, direction)` | data-dependent; one `threshold`-sized round trip |
| `indicators.crossed_above(a, b)` | `Series[bool]` | event-only; for diagnostics, not signal decisions |
| `indicators.crossed_below(a, b)` | `Series[bool]` | event-only |
| `indicators.signal_from(condition)` | `int` (0 or 1) | lifts a bool / scalar into 0/1 |
| `indicators.a_share_limit_pct(symbol)` | `float` | board limit ratio (10% / 20% / 30%); ST needs manual `0.05` |
| `indicators.limit_up_approx(close, high, symbol=...)` | `Series[bool]` | `2` — approx limit-up + `close==high` |
| `indicators.limit_down_approx(close, low, symbol=...)` | `Series[bool]` | `2` — approx limit-down + `close==low` |

> **Limit-up / limit-down approximation**: For historical daily backtests
> only. Compares `close` to `round(prev_close * (1 ± limit_pct), 2)` using
> :func:`a_share_limit_pct` (688/689 SH & 300–302 SZ → 20%, `.BJ` → 30%,
> else 10%). Also requires `close == high` (limit-up) or `close == low`
> (limit-down) on that bar. Does **not** detect ST/*ST 5% from the code alone
> — pass `limit_pct=0.05` when needed. Use `indicators.signal_from(...)` for
> target state on the latest bar.

> **Look-ahead caution (`ichimoku`)**: `senkou_a` / `senkou_b` are shifted
> *forward* `kijun` bars — at the current bar they reflect data `kijun`
> bars ago (past-derived, safe). `chikou` is shifted *back* `kijun` bars,
> so its last `kijun` values are `NaN` and mid-series it references future
> closes — **never** read `chikou` for a current-bar signal. The
> iterative `psar` / `kama` / `supertrend` carry trend state across bars;
> read `supertrend().direction.iloc[-1] > 0` as the long target-state via
> `signal_from`, not the band-cross event.

> **Look-ahead caution (`zigzag`)**: `pivot` **repaints** — the running
> extreme of the leg in progress only becomes a confirmed pivot on a
> *later* bar (once price reverses by `threshold`), so `pivot.iloc[-1]`
> peeks at a not-yet-confirmed swing. Treat `pivot` as chart anchors, not
> a live signal. Use `direction.iloc[-1]` (`+1` up-swing / `-1`
> down-swing) for decisions: it flips only at the confirmation bar from
> past data and never repaints — route it through `signal_from`.

## Idiomatic usage

```python
from __future__ import annotations

import pandas as pd  # required wherever you call pd.isna / pd.Timestamp / ...

from doyoutrade.strategy_sdk import Signal, Strategy, indicators


class MacdRsiCombo(Strategy):
    timeframe = "1d"
    startup_history = 120  # max( MACD(12,26,9) warm-up, RSI(14) warm-up )

    def populate_indicators(self, df, ctx):
        macd = indicators.macd(df["close"], fast=12, slow=26, signal=9)
        df["macd_hist"] = macd.hist
        df["rsi"] = indicators.rsi(df["close"], period=14)
        bb = indicators.bollinger(df["close"], window=20)
        df["bb_upper"] = bb.upper
        df["bb_lower"] = bb.lower
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        if pd.isna(last["macd_hist"]) or pd.isna(last["rsi"]):
            return Signal.hold(tag="warmup")
        if last["macd_hist"] > 0 and last["rsi"] < 65:
            return Signal.buy(tag="macd_positive+rsi_room")
        if ctx.position.is_long and last["macd_hist"] < 0:
            return Signal.sell(tag="macd_negative")
        return Signal.hold()
```

## Compare levels, not events

`Signal.buy` / `Signal.sell` are **target-state** decisions: "do I want
this symbol held long *as of this bar*". The runner converts that to a
position via PositionManager's diff against current holdings. Any
encoding that emits BUY only on a transition bar (and SELL on the very
next bar) produces a 1-cycle holding period.

`Signal.target_exposure(target=..., tag=...)` is the explicit-inventory
variant: map each regime / band / grid level to a desired equity fraction
when you need 0% / 25% / 50% / 75% / 100% style inventory control.
`Signal.target_quantity(quantity=..., tag=...)` is the strict-grid variant:
map each regime / band / grid level to a desired absolute share inventory
when you need 100 / 200 / 300 share ladders and want no within-band
notional rebalancing.

| Anti-pattern (event) | Use instead (target state) |
|---|---|
| Buy only on `crossed_above` bar, sell next bar | Buy while `fast > slow`, sell while `fast <= slow` |
| Buy only on RSI crossing 30 downward | Buy while `RSI < oversold`, sell while `RSI > overbought` |
| Buy on histogram sign change | Buy while `hist > 0` |

`crossed_above` / `crossed_below` are useful for **diagnostics** (e.g.
"count golden crosses in the window") — just not as the direct buy/sell
trigger.

## startup_history sizing

`startup_history` is the single source of truth for the data window the
runner provisions. The compiler rejects `rolling(N)` literals where
`N > startup_history` with `history_check_literal_disallowed` — so size
`startup_history` to the **longest** rolling window across all indicators
plus their warm-up factor.

For a MACD(12,26,9) + RSI(14) + ADX(14) combo: max is MACD's
`26 + 9 + 26*3 = 113`. Round up — `startup_history = 120` is reasonable.
