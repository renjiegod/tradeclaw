---
name: pattern-recognition
description: Run chart-pattern detection on cached OHLCV via `doyoutrade-cli analysis pattern`. Use for K线形态, candlestick patterns, support/resistance, peaks and valleys, head-and-shoulders, double top/bottom, triangles, broadening, trend slope, or post-`data run` structural analysis. Use `doyoutrade-data` first when OHLCV is missing. Read `references/extended-patterns.md` only for non-implemented pattern questions; it is a desired-state catalog, not callable CLI surface.
category: analysis
style: process
---

<!-- style: process — describes a real CLI command. The pattern list and signal codes
must match the implementation in doyoutrade/api/operations/pattern.py. When the
tool grows new pattern names, update both this SKILL.md and the user-facing tool
description in doyoutrade/api/operations/pattern.py together. -->

# Chart Pattern Recognition

## When to use

- After `doyoutrade-cli data run` has persisted an `ohlcv_{code}.csv` and the
  next question is *structural* — "where are the supports", "is this a
  topping pattern", "any candlestick reversal signal".
- When the user names a specific pattern keyword (candlestick / 头肩顶 /
  双底 / triangle / broadening / 支撑阻力位).
- As a confirmation layer for an entry idea — pair with indicator evidence
  (`technical-basic`) and the SDK examples (`doyoutrade/strategy_sdk/examples/`).

Do **not** use it for arbitrary feature engineering or as a substitute for
strategy code; the tool returns aggregate counts and level prices, not bar-by-bar
`Signal`s you can plug straight into `on_bar`.

## Input contract

The command reads CSV from `~/.doyoutrade/assistant/artifacts/ohlcv_{code}.csv`.
Run `doyoutrade-cli data run <code>` with the same `code` first, or the command
returns `error_code: ohlcv_csv_missing`.

```bash
# minimal call
doyoutrade-cli analysis pattern 600519.SH

# only specific patterns + custom window
doyoutrade-cli analysis pattern 600519.SH --patterns candlestick,support_resistance --window 20
```

| Flag | Type | Required | Default | Description |
|---|---|---|---|---|
| `<code>` | string | yes | — | Symbol that has been pulled via `doyoutrade-cli data run`. |
| `--patterns` | string | no | `"all"` | Comma-separated subset of the names in the table below, or `"all"`. |
| `--window` | integer | no | `10` | Detection window (bars). Try 10–30 for daily data. |

## Pattern catalog (implemented today)

These are the exact pattern names the tool accepts — `doyoutrade/api/operations/pattern.py` rejects any
other value with `error_code: unknown_patterns`. Anything **not** in this table
is in `references/extended-patterns.md` as desired-state work, not a callable
pattern.

### Candlestick (single + double bar)

| Sub-pattern | Direction | Heuristic |
|---|---|---|
| Doji | Neutral | body / range < 10% |
| Hammer | Bullish | lower shadow > 2 × body, upper shadow < body, not a doji |
| Bullish engulfing | Bullish | previous bar bearish, current bullish, current body covers previous open-close, current body > previous |
| Bearish engulfing | Bearish | mirror of bullish engulfing |

The tool returns a **summary**: counts of bullish / bearish / neutral bars over
the window plus the latest non-zero signal. Per-bar signals are not surfaced —
if you need them, call the underlying `candlestick_patterns()` in
`doyoutrade/api/operations/pattern.py` from strategy code.

### Structural patterns

| Name | Output | Heuristic |
|---|---|---|
| `support_resistance` | `{"support": [...], "resistance": [...]}` (prices) | peak / valley clustering with 5%-of-range threshold; up to `num_levels` (default 3) per side |
| `head_and_shoulders` | `{"count": N}` | 3 consecutive peaks where the middle peak is higher and the two shoulders are within 5% of their average |
| `double_top_bottom` | `{"double_top": N, "double_bottom": N}` | 2 consecutive peaks (or valleys) within 3% of each other |
| `triangle` | `{"ascending": N, "descending": N}` | ascending: rising valleys + flat peaks; descending: falling peaks + flat valleys; "flat" = within 2% of range |
| `broadening` | `{"count": N}` | peaks monotonically rising **and** valleys monotonically falling within the window |
| `trend_slope` | `{"mean_slope": float}` | mean of rolling linear-fit slope (window-size fit); positive = uptrend, negative = downtrend |

`peaks_valleys` is a primitive used by the other detectors and is **not**
exposed as a standalone pattern name today; the tool would reject
`patterns="peaks_valleys"` with `unknown_patterns`. To inspect peak / valley
indices, compute them in strategy code via the same `find_peaks_valleys`
helper imported from `doyoutrade.api.operations.pattern`.

## Window sizing rules

- Daily bars: start with `window=10`, widen to 20–30 if peak / valley density
  is too noisy.
- Intraday (≤15-min): smaller windows (5–10) tend to surface more spurious
  patterns; prefer aggregation to daily before scanning.
- `head_and_shoulders` and `double_top_bottom` rely on at least 2–3 detected
  peaks; on short series the `count` will trivially be 0. Confirm `bars` in the
  response is comfortably > `2 * window + 1`.

## Signal interpretation

- A **`broadening` count > 0** by itself is weak; broadenings often precede
  breakouts in *either* direction. Wait for a confirmed close beyond the
  pattern boundary.
- **Support / resistance** levels nearest current price are most actionable;
  far-historical levels lose information.
- **Candlestick summary** counts include the whole window, not just the most
  recent bar — when the user asks "is *this* bar a hammer", inspect the latest
  non-zero direction in the summary or drop down to `candlestick_patterns()`.

## Composing with other signals

A pattern fires on price geometry alone — it has no volume or trend context.
Strengthen the signal by cross-checking with indicator evidence:

- Reversal candlestick + RSI overbought / oversold (see `technical-basic`).
- Triangle apex + ADX rising (trend coming back).
- Support / resistance touch + above-average volume.

See `strategy-definition-authoring` for a worked example of combining
pattern + indicator + volume into a single rationale.

## Scanning many symbols at once

`analysis pattern` reads **one** symbol's cached OHLCV CSV per call. If the
user wants "find every stock in this list that just printed a hammer", do
**not** loop `analysis pattern` over the universe — switch to
`doyoutrade-cli stock screen --universe-file <path> --patterns hammer,...`
(documented in the `doyoutrade-data` skill). The screener auto-fetches bars
per symbol, evaluates the pattern condition, and returns a single CSV of
matches; it also supports AND-combining the pattern with RSI / MA cross /
volume-ratio filters so the rationale lives in one envelope rather than
many. Pattern names in the screener come from the same catalog above
(plus `bullish_engulfing` / `bearish_engulfing` / `double_top` /
`double_bottom` / `ascending_triangle` / `descending_triangle` as
direction-explicit splits).

## Error codes

| `error_code` | Meaning | Fix |
|---|---|---|
| `ohlcv_csv_missing` | No persisted CSV for the code | Run `doyoutrade-cli data run <code>` first. |
| `ohlcv_csv_read_failed` | CSV exists but pandas couldn't parse it | Re-pull market data; the artifact may be truncated. |
| `ohlcv_csv_empty` | CSV present but zero rows | Re-pull with a wider date range. |
| `unknown_patterns` | A name in `patterns=` is not in the catalog above | Drop the unknown name or pick from the catalog. |

## Anti-patterns

- ❌ Listing aspirational pattern names (`morning_star`, `three_white_soldiers`,
  `piercing_line`) in `--patterns` — these are in
  [`references/extended-patterns.md`](references/extended-patterns.md) for
  a reason; the tool rejects them.
- ❌ Treating `trend_slope.mean_slope` as a directional signal without a
  magnitude reference; it's a raw `dy/dx` and depends on the price scale.
- ❌ Re-running pattern detection inside strategy code via the *tool* — the
  tool is an assistant-side analysis helper, not a runtime helper. Inside
  `generate`, import the pure functions from
  `doyoutrade.api.operations.pattern` directly (or copy the formulas).

## References

Deep-dive files in this skill's `references/` folder. Load the file that
matches the gap rather than reading everything up front.

- [`references/extended-patterns.md`](references/extended-patterns.md) —
  catalog of pattern names the tool does **not** implement today (morning
  star, three white soldiers, harami, broadening sub-variants, piercing
  line, etc.). Desired-state work, not a callable surface. **Read this
  only when the user asks for a pattern that returned
  `unknown_patterns`, or when you need to confirm whether a requested
  pattern is on the implemented list vs the aspirational list.**
