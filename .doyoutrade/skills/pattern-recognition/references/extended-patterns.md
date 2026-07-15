# Extended Pattern Catalog (Desired-State)

These patterns are **not** in the live `doyoutrade-cli analysis pattern` command
today — calling it with these names raises `error_code: unknown_patterns`.
They live here as a reference for:

1. Strategy authors who want to implement the formula directly inside
   `build_target` (`ctx.ohlcv_df(symbol)` → pandas rolling math).
2. Future tool work — when one of these is implemented in the pattern
   operation, move the entry from this file up to `SKILL.md` and add it to
   the `ALL_PATTERNS` list in `doyoutrade/api/operations/pattern.py`.

The shapes below are the geometric heuristics that *would* be checked if the
pattern were lifted into the tool; treat them as starting points, not as
locked contracts.

---

## Additional candlestick patterns

The tool's `candlestick` summary covers doji, hammer (bullish), and engulfing
(bullish / bearish). The patterns below are conventional shapes that aren't yet
detected.

### Single-bar

| Pattern | Direction | Geometry |
|---|---|---|
| Inverted hammer | Bullish reversal | Small body near the bottom of the range, long upper shadow ≥ 2 × body, short lower shadow — ✅ 现已在 `doyoutrade.strategy_sdk.patterns` 实现，策略代码用 `patterns.is_inverted_hammer(open_, high, low, close)`，不要再手 import `doyoutrade.api.operations.pattern`。 |
| Shooting star | Bearish reversal (after uptrend) | Same shape as inverted hammer but the prior trend is bullish |
| Spinning top | Neutral | Small body with roughly equal upper and lower shadows (both > body) |

### Double-bar

| Pattern | Direction | Geometry |
|---|---|---|
| Bullish harami | Bullish reversal | Prior bar bearish, current bar bullish, current body fully contained inside prior body — ✅ 现已在 `doyoutrade.strategy_sdk.patterns` 实现，策略代码用 `patterns.is_bullish_harami(open_, high, low, close)`。 |
| Bearish harami | Bearish reversal | Mirror of bullish harami — ✅ 现已在 `doyoutrade.strategy_sdk.patterns` 实现，策略代码用 `patterns.is_bearish_harami(open_, high, low, close)`。 |
| Piercing line | Bullish reversal | Prior bearish, current opens below prior low, closes above prior bar's midpoint |
| Dark cloud cover | Bearish reversal | Mirror of piercing line |

### Triple-bar

| Pattern | Direction | Geometry |
|---|---|---|
| Morning star | Bullish reversal | Bearish → small body (gap down) → bullish bar closing above bar-1's midpoint |
| Evening star | Bearish reversal | Mirror of morning star |
| Three white soldiers | Bullish trend confirmation | Three consecutive bullish bars, each closing near its high, each open inside prior body |
| Three black crows | Bearish trend confirmation | Mirror of three white soldiers |

## Implementing in strategy code

**首选**：先看 `doyoutrade.strategy_sdk.patterns`（见
`strategy-definition-authoring` SKILL.md 的 "patterns.* (chart-pattern
primitives)" 段）。harami / inverted hammer / engulfing / hammer / doji /
swing / double top/bottom / head-and-shoulders / triangle / broadening
都已经在 SDK 里以 lookahead-safe 的形式实现，**不要**再从
`doyoutrade.api.operations.pattern` 手 import 形态函数到策略代码——那个
模块用 `center=True` 的双向窗口，会把未来 bar 喂进入场判断，
`StrategyCompiler` 的 `disallowed_import` 会拒。

仅当 `patterns.*` 未覆盖（如晨星 / 启明星、三白兵 / 三黑鸦、刺透线 /
乌云盖顶、流星 / 陀螺等）时，按下面的模板在 `populate_indicators` 里手
工算，再在 `on_bar` 里 `.iloc[-1]` 读：

```python
df = ctx.ohlcv_df(symbol)        # float64 OHLCV, DatetimeIndex
if df.empty or len(df) < 3:
    return PortfolioTarget(allocations=())

body = (df["close"] - df["open"]).abs()
total_range = df["high"] - df["low"]
upper_shadow = df["high"] - df[["open", "close"]].max(axis=1)
lower_shadow = df[["open", "close"]].min(axis=1) - df["low"]

# Example: morning star (not in patterns.* yet)
prev2_bearish = df["close"].shift(2) < df["open"].shift(2)
prev1_small_body = body.shift(1) / total_range.shift(1) < 0.3
curr_bullish = df["close"] > df["open"]
prev2_mid = (df["open"].shift(2) + df["close"].shift(2)) / 2
curr_closes_above_mid = df["close"] > prev2_mid

morning_star = prev2_bearish & prev1_small_body & curr_bullish & curr_closes_above_mid
# morning_star.iloc[-1] → True / False for the latest bar
```

手工模板只能用 `.shift(N)`（N >= 0）回看过去 bar，不要用 `.shift(-N)` /
`rolling(..., center=True)` / `df.iloc[i]` 且 i >= 0——这些会被
`StrategyCompiler` 的 `lookahead_access` 检查拦掉。Keep numeric work in
floats; switch back to `Decimal` only when sizing the allocation.
