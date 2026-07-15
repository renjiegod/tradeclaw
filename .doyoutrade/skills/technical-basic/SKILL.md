---
name: technical-basic
description: Reference card for classic technical indicators (trend EMA / MACD / ADX, mean-reversion Bollinger Bands / RSI, volume-price OBV / volume ratio) and a three-dimensional voting scheme to combine them. Use this skill whenever the user asks "解释下 RSI / MACD / 布林带 / ADX / OBV 怎么算 / what does RSI mean / 这个指标是干什么的 / 怎么组合指标", or whenever you're about to implement one of these indicators in strategy code and want to confirm the formula and convention before guessing. Pairs with `strategy-definition-authoring` (the SDK contract + `indicators.*` API for calling these formulas from `on_bar` / `populate_indicators`).
category: reference
style: reference
---

<!-- style: reference — formula dictionary, not a runnable contract. The voting
scheme below is one example of composition, not a rule the runtime enforces.
Adapt thresholds and weights per strategy. -->

# Core Technical Indicators Reference

## When to use

- Before authoring strategy code that touches EMA / MACD / ADX / BB / RSI /
  OBV — read the relevant section so the formula choice (Wilder EWM vs. SMA,
  signal-line span, oversold thresholds) is conscious instead of guessed.
- When a human asks "what does this indicator mean?" — the table maps each
  indicator to its directional reading.
- When composing several indicators into a single rationale; the *Three-Dimensional
  Voting* section gives one working pattern.

This is **not** a runnable tool — there is no `technical_basic` assistant tool.
Computations land in strategy code inside `populate_indicators(self, df, ctx)`
and `on_bar(self, df, ctx)`, where `df` is a `pandas.DataFrame` of the current
symbol with lowercase `open / high / low / close / volume` columns and a
`DatetimeIndex`. `on_bar` returns a single `Signal` (target state); the runner
calls it once per symbol per cycle.

> **Prefer `doyoutrade.strategy_sdk.indicators` over hand-rolling.** The
> SDK ships vetted implementations of every indicator in this catalog
> (`indicators.macd`, `indicators.rsi`, `indicators.adx`,
> `indicators.bollinger`, `indicators.atr`, `indicators.obv`,
> `indicators.sma`, `indicators.ema`) **plus an extended set** —
> momentum (`kdj`, `williams_r`, `cci`, `roc`, `momentum`, `mfi`,
> `trix`), volume/price (`vwap`, `cmf`, `ad`, `volume_ratio`),
> channel/volatility (`keltner`, `donchian`, `stdev`,
> `hist_volatility`), advanced trend (`wma`, `dema`, `kama`,
> `supertrend`, `psar`, `ichimoku`), and swing structure (`zigzag`) —
> plus the `signal_from` helper that
> lifts a level comparison into a `{0, 1}` int (map `1` → `Signal.buy`,
> `0` → `Signal.sell` / `hold` inside `on_bar`). Manual
> `close.ewm(...).mean()` chains that reproduce these formulas are
> discouraged — a typo in the formula compiles cleanly and produces a
> "looks fine" backtest. See
> `strategy-definition-authoring/references/indicators.md` for the full
> indicator API and the `target_state` vs event-encoding contract.

## Indicator catalog

### Trend dimension — *which way is the market leaning*

| Indicator | Formula | Reading |
|---|---|---|
| EMA(span) | `close.ewm(span=N, adjust=False).mean()` | Direction & smoothness; cross of fast / slow signals trend change. |
| MACD(12, 26, 9) | `MACD = EMA(close,12) - EMA(close,26)` · `Signal = EMA(MACD,9)` · `Hist = MACD - Signal` | Bullish: MACD crosses above signal **or** histogram flips positive. Bearish: mirror. |
| ADX(14) | Wilder EWM (`alpha=1/14`) of DX where `DX = 100 * |+DI - -DI| / (+DI + -DI)`; full chain: `+DM/-DM → TR → +DI/-DI → DX → ADX`. | < 20 = no trend; > 25 = trending; > 40 = strong trend. Direction-agnostic. |

### Mean-reversion dimension — *is the move overextended*

| Indicator | Formula | Reading |
|---|---|---|
| Bollinger Bands(20, 2) | `mid = close.rolling(20).mean()` · `band = std * 2`; `boll_ub = mid + band`, `boll_lb = mid - band`. | Tag of `boll_ub` ≈ overbought; tag of `boll_lb` ≈ oversold; band squeeze precedes volatility expansion. |
| RSI(14) | Wilder EWM of up/down moves; `RSI = 100 - 100 / (1 + avg_gain / avg_loss)`. | < 30 oversold, > 70 overbought (defaults; tune per market). Divergence with price is a stronger signal than the absolute level. |

### Volume-price dimension — *is the move backed by participation*

| Indicator | Formula | Reading |
|---|---|---|
| OBV | `(volume * sign(close.diff())).cumsum()` | Rising OBV confirms a rising price; flat / falling OBV under a rising price is a divergence warning. |
| Volume ratio | `volume / volume.rolling(N).mean()` (`indicators.volume_ratio`) | > 1.5 = above-average participation; < 0.7 = thin tape. |

## Extended catalog (SDK `indicators.*`)

These ship alongside the classics; same `pandas.Series` in / `Series` (or
`NamedTuple`) out contract, same warm-up-NaN gating. Read the *level*, lift
through `signal_from` — never the cross event.

### Momentum / overbought-oversold

| Indicator | Call | Reading |
|---|---|---|
| KDJ(9,3,3) | `indicators.kdj(high, low, close)` → `(k, d, j)` | `j > 100` overbought, `j < 0` oversold; A-share favourite. Smoothed stochastic. |
| Williams %R(14) | `indicators.williams_r(high, low, close)` | `[-100, 0]`; below −80 oversold, above −20 overbought. Mirror of fast stochastic. |
| CCI(20) | `indicators.cci(high, low, close)` | > +100 strong up-thrust, < −100 down-thrust; unbounded oscillator. |
| ROC / Momentum | `indicators.roc(close, period)` (%) · `indicators.momentum(close, period)` (Δ) | Sign + magnitude of N-bar change; zero-line cross is the momentum flip. |
| MFI(14) | `indicators.mfi(high, low, close, volume)` | Volume-weighted RSI; < 20 oversold, > 80 overbought, with participation baked in. |
| TRIX(15) | `indicators.trix(close)` | Triple-smoothed RoC; zero-line cross filters whipsaw better than raw MACD. |

### Volume / price

| Indicator | Call | Reading |
|---|---|---|
| VWAP(window) | `indicators.vwap(high, low, close, volume, window=14)` | Fair-value anchor; price above = buyers in control. `window=None` ⇒ anchored cumulative. |
| CMF(20) | `indicators.cmf(high, low, close, volume)` | `[-1, 1]`; persistently > 0 = accumulation, < 0 = distribution. |
| A/D line | `indicators.ad(high, low, close, volume)` | Cumulative money-flow volume; divergence vs price warns like OBV. |

### Channel / volatility

| Indicator | Call | Reading |
|---|---|---|
| Keltner | `indicators.keltner(high, low, close)` → `(upper, middle, lower)` | EMA ± ATR band; close outside the band = breakout/exhaustion. |
| Donchian(20) | `indicators.donchian(high, low)` → `(upper, middle, lower)` | Rolling N-bar high/low channel; the classic turtle breakout. |
| Stdev / HV | `indicators.stdev(close)` · `indicators.hist_volatility(close)` | Dispersion / annualised vol; regime filter and position-sizing input. |

### Advanced trend

| Indicator | Call | Reading |
|---|---|---|
| WMA / DEMA | `indicators.wma(close, window)` · `indicators.dema(close, span)` | Lower-lag moving averages; faster cross response than SMA/EMA. |
| KAMA | `indicators.kama(close)` | Adapts smoothing to the efficiency ratio — tracks fast in trends, flattens in chop. |
| SuperTrend | `indicators.supertrend(high, low, close)` → `(supertrend, direction)` | ATR trailing stop; `direction.iloc[-1] > 0` = up-trend target state. |
| PSAR | `indicators.psar(high, low)` | Stop-and-reverse trailing dots; price crossing the SAR is the reversal. |
| Ichimoku | `indicators.ichimoku(high, low, close)` → `(tenkan, kijun, senkou_a, senkou_b, chikou)` | Cloud system. **`chikou` is shifted into the future — never read it for a current-bar signal** (see indicators.md). |
| ZigZag | `indicators.zigzag(close, threshold=0.05)` → `(pivot, direction)` | Percent-reversal swing filter. **`pivot` repaints — only `direction.iloc[-1]` (`+1`/`-1` confirmed swing) is look-ahead-safe** (see indicators.md). |

### A-share limit-up / limit-down (historical daily, approximate)

| Indicator | Call | Reading |
|---|---|---|
| Limit-up approx | `indicators.limit_up_approx(close, high, symbol=sym)` → `Series[bool]` | `True` when `close` is at the board's rounded limit-up price **and** `close == high`. Board pct from `a_share_limit_pct(symbol)` (10% main, 20% ChiNext/STAR, 30% BJ). **ST 5% is not inferred from code** — pass `limit_pct=0.05`. For target state: `signal_from(limit_up_approx(...).iloc[-1])`. Screener: `stock screen --limit-up-approx`. `data run --indicators limit_up_approx` auto-injects the symbol. |
| Limit-down approx | `indicators.limit_down_approx(close, low, symbol=sym)` → `Series[bool]` | Symmetric to limit-up: `close` at rounded limit-down price **and** `close == low`. Same board pct / ST override. Screener: `stock screen --limit-down-approx`. `data run --indicators limit_down_approx` auto-injects the symbol. |

## Three-dimensional voting (one composition scheme)

Treat **trend / mean-reversion / volume** as three independent votes and only
commit to a side when at least two agree:

- **Long**: trend is bullish (EMA12 > EMA26 **or** MACD histogram > 0) **and**
  RSI not overbought **and** OBV rising.
- **Short**: trend is bearish **and** RSI not oversold **and** OBV falling.
- **Stand aside** when signals are mixed.

This is a *template*, not a contract — adapt thresholds and weights to the
strategy. The point of three dimensions is to avoid the common failure mode of
trading a single overbought RSI signal in a strong uptrend.

## Recommended parameter defaults

These are sane starting points, not optimums. Tune via the task's
`parameter_overrides` (or `backtest run --params`) during the
`strategy-iteration` loop.

| Parameter | Default | Notes |
|---|---|---|
| `ema_fast` / `ema_slow` | 12 / 26 | Classic MACD spans; also reused for trend EMA |
| `macd_signal` | 9 | EWM span for the signal line |
| `adx_period` | 14 | Wilder default |
| `adx_threshold` | 25 | Anything below means "no clear trend" |
| `bb_window` / `bb_std` | 20 / 2.0 | Standard Bollinger; widen `bb_std` to 2.5 on volatile markets |
| `rsi_period` | 14 | Wilder default |
| `rsi_oversold` / `rsi_overbought` | 30 / 70 | A-shares sometimes tune to 20 / 80 |
| `vol_ma_period` | 20 | Volume baseline window |
| `obv_ma_period` | 20 | OBV smoothing for divergence checks |

## Embedding in a Doyoutrade strategy

These indicators run inside `populate_indicators` / `on_bar` against the
per-symbol DataFrame. Do **not** import `talib`, `stockstats`, or any other
third-party indicator library — `strategy-definition-authoring/references/sdk-surface.md`
lists the exact allowed-imports whitelist.

```python
from typing import ClassVar

import pandas as pd

from doyoutrade.strategy_sdk import Signal, Strategy, indicators


class ThreeDimVoteStrategy(Strategy):
    """Long while >=2 of {trend, RSI room, OBV up} agree; flat otherwise."""

    name: ClassVar[str] = "three_dim_vote"
    timeframe: ClassVar[str] = "1d"
    # MACD slow(26) + signal(9) + warm-up headroom.
    startup_history: ClassVar[int] = 60

    def populate_indicators(self, df: pd.DataFrame, ctx) -> pd.DataFrame:
        macd = indicators.macd(df["close"], fast=12, slow=26, signal=9)
        df["macd_hist"] = macd.hist
        df["rsi"] = indicators.rsi(df["close"], period=14)
        df["obv"] = indicators.obv(df["close"], df["volume"])
        return df

    def on_bar(self, df: pd.DataFrame, ctx) -> Signal:
        last = df.iloc[-1]
        # Warm-up NaN guard — startup_history covers length, but indicator
        # warm-up can still leave NaN on the first bars.
        if pd.isna(last["macd_hist"]) or pd.isna(last["rsi"]):
            return Signal.hold(tag="warmup")

        trend_long = bool(last["macd_hist"] > 0)          # *level*, not event
        rsi_room = bool(last["rsi"] < 70)
        obv_up = bool(df["obv"].iloc[-1] > df["obv"].iloc[-2])
        votes = sum([trend_long, rsi_room, obv_up])

        if votes >= 2:
            return Signal.buy(tag="trend_rsi_obv_long")
        if ctx.position.is_long:
            return Signal.sell(tag="votes_failed")
        return Signal.hold()
```

Float math is fine inside `populate_indicators` for indicator computation.
Position sizing happens downstream in `PositionManager`; `on_bar` only emits
a target-state `Signal`. `tag` is mandatory on every actionable signal
(`buy` / `sell` / `target_exposure` / `target_quantity`) — it identifies the
factor(s) behind the decision and is persisted onto fills for post-hoc
analysis.

## Anti-patterns

- ❌ Calling indicator libraries (`talib`, `stockstats`, `ta`) — the compiler
  rejects them via `disallowed_import`. The whitelist is `decimal`, `math`,
  `numpy`, `pandas`, `doyoutrade.strategy_sdk`. The blessed indicators live in
  `doyoutrade.strategy_sdk.indicators`; **use them** instead of re-deriving
  the same formula inline.
- ❌ Hand-rolling MACD / RSI / ADX / Bollinger / ATR with
  `close.ewm(...).mean()` chains. The vetted `indicators.*` calls produce
  the same numbers without typo risk; the failure mode is a "1-character
  shift in spans" or "wrong base series for the signal line", and that
  shape compiles cleanly and produces a misleading backtest. Reserve
  manual math for indicators the SDK does not ship yet — and even then,
  prefer adding the indicator to the SDK over inlining.
- ❌ Encoding cross *events* as the `on_bar` return (e.g. emitting
  `Signal.buy(...)` only on the bar where `prev <= 0 < curr`). The
  runner reads the `Signal` as a *target state* and diffs it against the
  portfolio every cycle — event encoding produces 1-cycle holding bugs.
  Compare *levels* (e.g. `macd.hist.iloc[-1] > 0`) and return
  `Signal.buy` / `Signal.sell` accordingly. The smoke gate rejects event
  encoding with `smoke_signal_flap_on_steady_bar`.
- ❌ Using a plain rolling mean for RSI / ADX. The Wilder convention is
  `ewm(alpha=1/N, adjust=False)`; SMA-based RSI agrees on long history but
  drifts on short windows. (`indicators.rsi` / `indicators.adx` already
  use Wilder — another reason to prefer them over inline math.)
- ❌ Voting on a single RSI > 70 reading inside a strong trend. The whole
  point of the three-dimensional scheme is to ignore that vote when the trend
  and volume both still agree with the move.
- ❌ Hardcoding indicator periods as magic numbers. Pull them from
  `ctx.parameters` so the iteration loop can sweep without touching code.
