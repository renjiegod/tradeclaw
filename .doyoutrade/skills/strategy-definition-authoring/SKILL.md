---
name: strategy-definition-authoring
description: |
  Author or modify a trading strategy's Python source code in Doyoutrade's
  Strategy SDK (Strategy class + populate_indicators + on_bar returning Signal).
  Load ONLY when an actual source-code write is imminent — i.e. the user asked
  to write/modify/refactor a strategy, or you need to fix a compile/smoke
  error_code, or you need the ctx.dp / allowed-imports / indicators reference.
  Do NOT load when only reading, listing, or re-running existing definitions —
  those go through doyoutrade-cli strategy definition list/get and backtest run
  directly. Companion to strategy-iteration (post-backtest), `doyoutrade-debug`
  (`debug get-run-view` for failed-run recovery), and strategy-authoring (full lifecycle overview).
category: strategy
style: process
---

# Authoring a Doyoutrade Strategy Definition

## Before You Write Any Code: Discovery

Run these CLI commands first so you don't hallucinate method names:

1. `doyoutrade-cli sdk dp-methods` — every `ctx.dp.*` method with signature and example.
2. `doyoutrade-cli sdk data-requests` — every `DataRequest.*` factory.
3. `doyoutrade-cli sdk indicators` — every function in `doyoutrade.strategy_sdk.indicators`
   with `return_type.fields` for multi-output indicators.

**Common-indicator exemption**: if your strategy uses only MACD / Bollinger / ADX /
SMA / EMA / RSI / ATR / OBV — complete copy-paste references are in this skill.
Skip step 3 for those. Only run `sdk indicators` for indicators outside that list.

## Authoring Lifecycle

Lifecycle commands (open / cancel / compile / finalize) are **shell commands**
invoked via `execute_bash`. File operations use **in-process tools called
directly** — no CLI subcommand, no `execute_bash`.

```bash
# 1. Open a session (CLI) — capture session_id AND work_dir
OPEN=$(doyoutrade-cli strategy authoring open --definition-id <sd-...>)
SESSION=$(echo "$OPEN" | jq -r .data.session_id)
WORK_DIR=$(echo "$OPEN" | jq -r .data.work_dir)
# New definition:
OPEN=$(doyoutrade-cli strategy authoring open --name "<display name>")
SESSION=$(echo "$OPEN" | jq -r .data.session_id)
WORK_DIR=$(echo "$OPEN" | jq -r .data.work_dir)
#   → {definition_id, session_id, work_dir, base_version, status}

# 2. Inspect the workspace (in-process file tools — call directly)
list_files(directory="$WORK_DIR")
read_file(file_path="$WORK_DIR/strategy.py")

# 3. Write / edit strategy.py (in-process file tools)
write_file(file_path="$WORK_DIR/strategy.py", content="...")
edit_file(file_path="$WORK_DIR/strategy.py", old_string="old_string", new_string="new_string")
edit_file(file_path="$WORK_DIR/strategy.py", old_string="x = 1", new_string="x = 2", replace_all=True)

# 4. Compile: AST + smoke, no persistence (CLI)
doyoutrade-cli strategy authoring compile --session-id "$SESSION"
#   → runs the AST + smoke compile gate; iterate until no errors

# 5. Finalize: promotes to versions/v{N+1}-{hash}/ (CLI)
doyoutrade-cli strategy authoring finalize --session-id "$SESSION"
```

For metadata-only edits (name, description, status — no source code changes):
`doyoutrade-cli strategy definition update <sd-...> --name "..." [--status active]`
Note: `strategy definition update` has NO source_code parameter — code changes
always go through in-process `write_file` / `edit_file`, then
`doyoutrade-cli strategy authoring finalize`.

For dry-run validation from the shell without creating a definition:
```bash
doyoutrade-cli sdk validate /tmp/<strategy>.py
```

## Entry File Convention

The entry file is `strategy.py`. It must define a class named `Strategy`
that subclasses `doyoutrade.strategy_sdk.Strategy`.

Recommended idiom to avoid name collision:

```python
from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

class Strategy(BaseStrategy):
    startup_history = 30   # NOT required_history
```

`on_bar(self, df, ctx) -> Signal` must return a real `Signal` object.
Use `Signal.hold(tag="reason")` for no-op — always include a `tag`.
Actionable outputs are `Signal.buy(...)`, `Signal.sell(...)`, and
`Signal.target_exposure(target=..., tag=...)` / `Signal.target_quantity(quantity=..., tag=...)`.

## The 6 Layers (always in this order)

1. **Class metadata** — `name`, `timeframe`, `startup_history`.
2. **Tunable parameters** — `IntParameter` / `DecimalParameter` / `CategoricalParameter` /
   `BooleanParameter` class attributes, or `# @param` annotation comments.
3. **Informative data declaration** — `informative_data(self, ctx) -> list[DataRequest]`.
4. **Indicator population** — `populate_indicators(self, df, ctx) -> DataFrame`; plus optional
   `@informative('1w')` / `@informative('1d', symbol='600519.SH')` / `@informative_each` methods.
5. **Signal generation** — `on_bar(self, df, ctx) -> Signal`; ALWAYS attach `tag=`.
6. **Lifecycle hooks (optional)** — `on_strategy_start(ctx)`, `on_cycle_start(ctx)`.

Strategies do NOT manage stops, sizing, order routing, or order events —
those belong to PositionManager / OrderManager downstream.

## Minimal Valid Strategy

```python
from __future__ import annotations

from doyoutrade.strategy_sdk import (
    Strategy as BaseStrategy, Signal, IntParameter, indicators,
)


class Strategy(BaseStrategy):
    name = "simple_ma_cross"
    timeframe = "1d"
    startup_history = 30

    fast = IntParameter(5, 15, default=10, optimize=True)
    slow = IntParameter(20, 50, default=30, optimize=True)

    def populate_indicators(self, df, ctx):
        df["ma_fast"] = indicators.sma(df["close"], self.fast.value)
        df["ma_slow"] = indicators.sma(df["close"], self.slow.value)
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        if last["ma_fast"] > last["ma_slow"]:
            return Signal.buy(tag="ma_fast_above_slow")
        if ctx.position.is_long:
            return Signal.sell(tag="ma_fast_below_slow")
        return Signal.hold(tag="flat_no_position")
```

## Explicit Exposure Grid Example

Use `Signal.target_exposure(...)` when the strategy should declare a final
inventory level instead of an entry/exit state. This is the recommended
shape for grid strategies: map each price band to a target exposure and let
PositionManager rebalance toward it.

```python
from __future__ import annotations

import math
import pandas as pd

from doyoutrade.strategy_sdk import (
    Strategy as BaseStrategy, Signal, IntParameter, DecimalParameter, indicators,
)


class Strategy(BaseStrategy):
    name = "grid_target_exposure"
    timeframe = "1d"
    startup_history = 60

    anchor_window = IntParameter(20, 120, default=60)
    grid_step = DecimalParameter(0.01, 0.08, default=0.03, decimals=3)
    max_levels = IntParameter(2, 8, default=4)

    def populate_indicators(self, df, ctx):
        df["anchor"] = indicators.sma(df["close"], self.anchor_window.value)
        df["deviation"] = (df["close"] - df["anchor"]) / df["anchor"]
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        if pd.isna(last["anchor"]) or pd.isna(last["deviation"]):
            return Signal.hold(tag="warmup")
        if last["deviation"] >= 0:
            return Signal.target_exposure(target=0.0, tag="grid_l0")

        levels = min(
            self.max_levels.value,
            math.floor(abs(float(last["deviation"])) / self.grid_step.value) + 1,
        )
        return Signal.target_exposure(
            target=levels / self.max_levels.value,
            tag=f"grid_l{levels}",
        )
```

`target_exposure` is a true rebalance contract: if the desired band is still
25% on the next bar but price drift changed the current portfolio weight,
PositionManager may top up or trim back to 25%. If you want "trade only when
the grid level changes", that is a different inventory-state contract.

## Strict Inventory Grid Example

Use `Signal.target_quantity(...)` when the strategy should declare an
absolute post-cycle share inventory. This is the recommended shape for
strict layer-based grids such as A-share "100 / 200 / 300 / 400 shares"
inventory ladders.

```python
from __future__ import annotations

import math
import pandas as pd

from doyoutrade.strategy_sdk import (
    Strategy as BaseStrategy, Signal, IntParameter, DecimalParameter, indicators,
)


class Strategy(BaseStrategy):
    name = "grid_target_quantity"
    timeframe = "1d"
    startup_history = 60

    anchor_window = IntParameter(20, 120, default=60)
    grid_step = DecimalParameter(0.01, 0.08, default=0.03, decimals=3)
    max_levels = IntParameter(2, 8, default=4)
    shares_per_level = IntParameter(100, 2000, default=100, step=100)

    def populate_indicators(self, df, ctx):
        df["anchor"] = indicators.sma(df["close"], self.anchor_window.value)
        df["deviation"] = (df["close"] - df["anchor"]) / df["anchor"]
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        if pd.isna(last["anchor"]) or pd.isna(last["deviation"]):
            return Signal.hold(tag="warmup")
        if last["deviation"] >= 0:
            return Signal.target_quantity(quantity=0, tag="grid_l0")

        levels = min(
            self.max_levels.value,
            math.floor(abs(float(last["deviation"])) / self.grid_step.value) + 1,
        )
        return Signal.target_quantity(
            quantity=levels * self.shares_per_level.value,
            tag=f"grid_l{levels}",
        )
```

`target_quantity` is an inventory contract, not a rebalance ratio. If the
strategy emits `target_quantity(quantity=300, ...)` on the next bar and the
current position is already 300 shares, PositionManager does nothing even if
the position's notional value drifted with price.

## MACD Reference (copy-paste-friendly)

`indicators.macd(...)` returns a `MACDResult` NamedTuple with **`macd`, `signal`, `hist`**
fields — NOT `.histogram`. `startup_history` for MACD(fast, slow, signal) must be at
least `slow + signal + 5` bars (≈40 for classic 12/26/9).

```python
from __future__ import annotations

import pandas as pd
from doyoutrade.strategy_sdk import (
    Strategy as BaseStrategy, Signal, IntParameter, indicators,
)


class Strategy(BaseStrategy):
    name = "macd_cross"
    timeframe = "1d"
    startup_history = 40  # slow=26 + signal=9 + 5 safety margin

    fast_period = IntParameter(5, 30, default=12, optimize=True)
    slow_period = IntParameter(15, 60, default=26, optimize=True)
    signal_period = IntParameter(5, 20, default=9, optimize=True)

    def populate_indicators(self, df, ctx):
        macd_out = indicators.macd(
            df["close"],
            fast=self.fast_period.value,
            slow=self.slow_period.value,
            signal=self.signal_period.value,
        )
        # MACDResult fields: .macd / .signal / .hist  (NOT .histogram)
        df["macd"] = macd_out.macd
        df["macd_signal"] = macd_out.signal
        df["macd_hist"] = macd_out.hist
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if pd.isna(last["macd"]) or pd.isna(last["macd_signal"]):
            return Signal.hold(tag="warmup")

        if prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]:
            return Signal.buy(tag="macd_golden_cross")

        if prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]:
            if ctx.position.is_long:
                return Signal.sell(tag="macd_dead_cross")
            return Signal.hold(tag="macd_dead_cross_no_pos")

        return Signal.hold(tag="no_cross")
```

`indicators.bollinger(...)` returns `BollingerResult` with `upper`, `middle`, `lower`.
`indicators.adx(...)` returns `ADXResult` with `adx`, `plus_di`, `minus_di`.
Always check `return_type.fields` in `doyoutrade-cli sdk indicators` output before
writing the access.

## patterns.* (chart-pattern primitives)

`doyoutrade.strategy_sdk.patterns` is the sibling of `indicators` for
candlestick / breakout / swing / structural-pattern math. It is **pre-injected
as `patterns`** in the compile sandbox alongside `indicators` — import via
`from doyoutrade.strategy_sdk import patterns`. Use these instead of
hand-rolling shadow / body / pivot math.

### Causal contract (read first)

`patterns.*` functions are **lookahead-safe (causal)**: the output Series at
index `i` depends only on bars at indices `<= i`. For swing- and
structural-pattern detectors (`swing_high` / `swing_low` /
`last_swing_high_level` / `last_swing_low_level` / `double_top` /
`double_bottom` / `head_and_shoulders` / `triangle` / `broadening`) a pivot
that occurs at bar `i` is **stamped at the confirmation bar `i + right`** —
the earliest bar at which the pivot is unambiguously known. This means
`.iloc[-1]` reads in `on_bar` only see pivots / patterns whose `right`
confirming bars have already arrived; the strategy never peeks forward.

> ⚠️ Do **not** import `doyoutrade.api.operations.pattern` (or any
> `doyoutrade.api.*` helper) into strategy code — that module powers the
> operator-facing analysis CLI and uses two-sided `rolling(...,
> center=True)` windows that read `i + window` future bars. Wiring it into
> a backtest silently leaks the future into the entry decision. The
> `disallowed_import` rule rejects it at compile time. Strategy code goes
> through `patterns.*` only.

### Function table

| Call | Returns | startup_history hint |
|---|---|---|
| `patterns.is_doji(open_, high, low, close, body_frac=0.10)` | `Series[bool]` | `1` |
| `patterns.is_hammer(open_, high, low, close)` | `Series[bool]` | `1` |
| `patterns.is_inverted_hammer(open_, high, low, close)` | `Series[bool]` | `1` |
| `patterns.is_bullish_engulfing(open_, high, low, close)` | `Series[bool]` | `2` |
| `patterns.is_bearish_engulfing(open_, high, low, close)` | `Series[bool]` | `2` |
| `patterns.is_bullish_harami(open_, high, low, close)` | `Series[bool]` | `2` |
| `patterns.is_bearish_harami(open_, high, low, close)` | `Series[bool]` | `2` |
| `patterns.prior_high(high, lookback)` | `Series` (NaN warm-up) | `lookback + 1` |
| `patterns.prior_low(low, lookback)` | `Series` (NaN warm-up) | `lookback + 1` |
| `patterns.broke_above(series, level)` | `Series[bool]` | `2` |
| `patterns.broke_below(series, level)` | `Series[bool]` | `2` |
| `patterns.touched_above(high, level)` | `Series[bool]` | `2` |
| `patterns.touched_below(low, level)` | `Series[bool]` | `2` |
| `patterns.bounced_from(low, close, support, tol=0.01)` | `Series[bool]` | `1` |
| `patterns.swing_high(high, left=3, right=3)` | `Series[bool]` (stamped at `i + right`) | `left + right + 1` |
| `patterns.swing_low(low, left=3, right=3)` | `Series[bool]` | `left + right + 1` |
| `patterns.last_swing_high_level(high, left=3, right=3)` | `Series` (ffill of pivot price; NaN until first confirmation) | `left + right + 1` |
| `patterns.last_swing_low_level(low, left=3, right=3)` | `Series` | `left + right + 1` |
| `patterns.double_top(high, left=3, right=3, tol=0.03)` | `Series[bool]` (stamped at second peak's confirmation bar) | `2 * (left + right + 1)` |
| `patterns.double_bottom(low, left=3, right=3, tol=0.03)` | `Series[bool]` | `2 * (left + right + 1)` |
| `patterns.head_and_shoulders(high, left=3, right=3, shoulder_tol=0.05)` | `Series[bool]` (stamped at third peak's confirmation bar) | `3 * (left + right + 1)` |
| `patterns.triangle(high, low, window=20, left=3, right=3)` | `Series[int]` (`+1` ascending / `-1` descending / `0` none) | `window + left + right` |
| `patterns.broadening(high, low, window=20, left=3, right=3)` | `Series[bool]` | `window + left + right` |

`level` accepts a `pd.Series` (compared bar-by-bar; the "was below / was
above" half uses the previous-bar level) or a numeric scalar (broadcast to a
constant). `tol` / `shoulder_tol` / `body_frac` are fractional (`0.03` = 3%)
and must be `>= 0`.

### Minimal example: hammer breakout with swing-low stop

```python
from __future__ import annotations

import pandas as pd
from doyoutrade.strategy_sdk import (
    Strategy as BaseStrategy, Signal, IntParameter, patterns,
)


class Strategy(BaseStrategy):
    name = "hammer_breakout"
    timeframe = "1d"
    startup_history = 30   # >= lookback + swing(left+right+1) + safety

    lookback = IntParameter(10, 30, default=20, optimize=True)

    def populate_indicators(self, df, ctx):
        df["hammer"] = patterns.is_hammer(
            df["open"], df["high"], df["low"], df["close"]
        )
        df["prior_hi"] = patterns.prior_high(df["high"], self.lookback.value)
        df["broke_hi"] = patterns.broke_above(df["close"], df["prior_hi"])
        df["swing_lo"] = patterns.last_swing_low_level(df["low"], left=3, right=3)
        return df

    def on_bar(self, df, ctx) -> Signal:
        last = df.iloc[-1]
        if pd.isna(last["prior_hi"]) or pd.isna(last["swing_lo"]):
            return Signal.hold(tag="warmup")
        if bool(last["hammer"]) and bool(last["broke_hi"]):
            return Signal.buy(tag="hammer+breakout")
        if ctx.position.is_long and last["close"] < last["swing_lo"]:
            return Signal.sell(tag="below_last_swing_low")
        return Signal.hold(tag="no_setup")
```

### Compile / smoke errors

`patterns.*` validates inputs strictly: passing a non-`pd.Series` raises
`TypeError`, and negative / zero lookback / window / left / right or
negative tolerances raise `ValueError`. Both surface through
`StrategyCompiler` smoke as `runtime_smoke_failed` with the underlying
type + value in the message. Fix direction:

- `<name> must be a pandas.Series` → pass `df["high"]` (etc.) instead of a
  list / numpy array / scalar.
- `<name> must be a positive integer` / `must be >= 0` → raise the param's
  lower bound or drop the `0` default.
- `tol must be >= 0` → tolerances are fractions, not basis points; use
  `0.03` for 3%, not `3`.

## Compile Hard Rules

| error_code | Triggered by | Fix |
|---|---|---|
| `entry_file_missing` | `strategy.py` not in session | Create via `write_file` |
| `missing_required_class` | No class named `Strategy` | Add `class Strategy(BaseStrategy):` |
| `not_a_class_definition` | `Strategy` name used for non-class | Fix the definition |
| `invalid_base_class` | Doesn't subclass `doyoutrade.strategy_sdk.Strategy` | Use aliased import |
| `disallowed_import` | `import requests / akshare / doyoutrade.data.*` | All data via `ctx.dp.*` |
| `history_check_literal_disallowed` | `rolling(N)` literal where N > `startup_history` | Raise `startup_history` or reduce window |
| `lookahead_access` | `df.iloc[i]` with i >= 0, or `df.shift(-n)` | Use `iloc[-1]`, `iloc[-2]` |
| `silent_exception_swallow` | `except Exception: pass` / silent `continue` | Narrow exception + log |
| `syntax_error` | Python syntax error in any `.py` | Fix the syntax |
| `compile_runtime_error` | Exception during smoke run | Check traceback in `message` |
| `unknown_dp_method` | `ctx.dp.foo()` not registered | Use a method from `sdk dp-methods` |
| `unknown_data_request_type` | `DataRequest.foo()` not registered | Use a factory from `sdk data-requests` |
| `invalid_exit_reason` | `Signal.sell(exit_reason=...)` not in the `ExitReason` enum | Use one of: `signal` / `stop_loss` / `take_profit` / `trailing_stop` / `roi` / `circuit_breaker`, or omit it |
| `invalid_signal_fraction` | `Signal.sell(fraction=...)` outside `(0, 1]` | Use a fraction in `(0, 1]` (1.0 = full exit, the default; 0.5 = half) |
| `invalid_target_exposure` | `Signal.target_exposure(target=...)` outside `[0, 1]` | Use a fraction of equity in `[0, 1]` where `0=flat` and `1=fully allocated` |
| `invalid_target_quantity` | `Signal.target_quantity(quantity=...)` below `0` | Use a non-negative post-cycle share inventory such as `0` / `100` / `200` |

## File Tool Error Codes

| error_code | Meaning | Fix |
|---|---|---|
| `session_not_found` | `session_id` invalid or expired | Re-open with `doyoutrade-cli strategy authoring open` |
| `session_disappeared` | Session dir removed externally | Re-open; re-author |
| `definition_not_found` | `definition_id` passed to `strategy authoring open` not found | Verify with `strategy definition get` |
| `name_required_for_new_definition` | Opening without `definition_id` and without `name` | Provide `--name` |
| `path_outside_workspace` | `write_file` / `edit_file` `file_path` escapes `work_dir` (read_file has no sandbox) | Use relative paths for write/edit |
| `file_not_found` | `read_file` on absent path | Check `list_files` first |
| `io_error` | Disk IO failed while reading/writing the file (permissions, disk full, target is a directory, etc.) | Surface the error message back to the operator; do not retry blindly |
| `old_string_not_found` | `edit_file` fragment not in file | Read file, pick correct fragment |
| `old_string_not_unique` | Fragment matches multiple locations | Use longer context or `replace_all=true` |
| `no_op_edit` | `old_string == new_string` | Provide a different `new_string` |
| `strategy_no_current_version` | `strategy authoring open` on def with no version yet | Use `strategy authoring finalize` to create first version |
| `strategy_version_not_pinned` | Expected a pinned version but none set | Pin a version first |

## Cross-Symbol / Cross-Timeframe Reference

| Need | Use | Notes |
|---|---|---|
| Bars of current symbol | `df` argument directly | provided to populate_indicators / on_bar |
| Bars of another symbol | `informative_data` + `ctx.dp.get_bars(symbol=X)` in on_bar | symbol MUST be declared |
| Indicators on another symbol (vectorized) | `@informative('1d', symbol='600519.SH')` | merges columns with suffix `_600519_SH_1d` |
| Same indicators on many symbols | `@informative_each('1d', symbols=(...))` | method takes extra `symbol` kwarg |
| Current symbol, different timeframe | `@informative('1w')` | merges columns with suffix `_1w` |
| Industry peers | `DataRequest.peers(window=N, top_n=20)` + `ctx.dp.get_peer_bars(...)` | |
| Market index / ETF | `DataRequest.index_bars(code, window=N)` + `ctx.dp.get_index_bars(code, ...)` | |
| Symbols under a watchlist tag | `ctx.dp.watchlist_symbols(tag="核心持仓")` | Read-only metadata snapshot, not bars — see below |

### `ctx.dp.watchlist_symbols(tag=None)`

Returns the canonical symbols the user has saved in their watchlist (自选股),
optionally filtered to one tag. Omit `tag` (or pass `None`) for every
watchlist symbol. It's a **frozen per-cycle, read-only snapshot** — no live
DB hit, deterministic within a cycle — so it's safe to call from
`populate_indicators` / `on_bar`. It returns *metadata* (the symbol list),
**not** bars: to prefetch OHLCV for those symbols still declare a
`DataRequest.bars` / `informative_data`. Confirm the exact signature with
`doyoutrade-cli sdk dp-methods` before use (it appears in that listing). When
no snapshot is wired into the run it raises a `DataAccessError`
(`invalid_argument`) rather than silently returning an empty list.

```python
def on_bar(self, df, ctx) -> Signal:
    core = ctx.dp.watchlist_symbols(tag="核心持仓")
    if ctx.symbol not in core:
        return Signal.hold(tag="not_in_core_watchlist")
    ...
```

## Signal Tagging (mandatory)

Always attach `tag=` to every `Signal.buy()`, `Signal.sell()`,
`Signal.target_exposure()`, `Signal.target_quantity()`, and `Signal.hold()`.
An untagged hold collapses to `<untagged_hold>` in diagnostics — you can't distinguish
"warmup" from "no_cross" from "cooldown" when diagnosing zero-trade runs.

```python
def on_bar(self, df, ctx) -> Signal:
    factors = []
    if self._momentum_ok(df):  factors.append("momentum")
    if self._volume_ok(df):    factors.append("volume")

    if len(factors) >= 2:
        return Signal.buy(tag="+".join(sorted(factors)))
    if factors:
        return Signal.hold(tag="factors_below_threshold")
    return Signal.hold(tag="no_factors_active")
```

## Exit Reason (optional, for attribution)

`Signal.sell(...)` accepts an optional `exit_reason=` that categorizes *why* the
position is exiting — orthogonal to `tag` (which says *which factor*). It's
purely additive: omit it and behavior is unchanged. When set, it rides
signal → intent → fill and powers the backtest report's `by_exit_reason`
breakdown (笔数 / PnL / 胜率 / 平均持仓 per reason), so you can tell whether your
take-profits or your stop-losses are carrying the strategy.

Allowed values (string or the `ExitReason` enum; anything else raises
`invalid_exit_reason` at construction/compile — never silently coerced):
`signal`, `stop_loss`, `take_profit`, `trailing_stop`, `roi`, `circuit_breaker`.
(`trailing_stop` / `roi` / `circuit_breaker` are normally set by the task-level
exit engine, not strategy code — see the doyoutrade-backtest skill.)

```python
def on_bar(self, df, ctx) -> Signal:
    if ctx.position.is_long:
        profit = ctx.position.current_profit  # unrealized return, 0.05 = +5%
        if profit >= 0.15:
            return Signal.sell(tag="ma_cross", exit_reason="take_profit")
        if profit <= -0.07:
            return Signal.sell(tag="ma_cross", exit_reason="stop_loss")
        if self._exit_signal(df):
            return Signal.sell(tag="ma_cross", exit_reason="signal")
    return Signal.hold(tag="hold")
```

## Partial Exit (optional, scale down a position)

`Signal.sell(...)` accepts an optional `fraction=` in `(0, 1]` — the portion
of the held position to sell. `1.0` (default) is a full exit; `0.5` sells
half (`floor(sellable_shares * fraction)`). Use it for staged take-profit /
de-risking without any task-level config. Combine freely with `exit_reason=`.
Out-of-range values raise `invalid_signal_fraction` (compile-time for
literals, construction-time for computed) — never silently clamped. A fraction
that floors to 0 whole shares on a tiny position emits a visible
`position_manager_skipped` (`reason=partial_exit_rounds_to_zero`) rather than a
phantom zero-share order.

```python
def on_bar(self, df, ctx) -> Signal:
    if ctx.position.is_long and ctx.position.current_profit >= 0.20:
        # First leg of a staged exit: take half off at +20%.
        return Signal.sell(tag="ma_cross", exit_reason="take_profit", fraction=0.5)
    return Signal.hold(tag="hold")
```

## Parameter Annotation (alternate to class attributes)

`# @param` comment lines are an equivalent way to declare tunable parameters:

```
# @param <name> int       [default=N] [range=lo,hi[,step]] [optimize=true|false]
# @param <name> decimal   [default=N] [range=lo,hi] [decimals=N]
# @param <name> categorical [default=X] choices=a,b,c
# @param <name> bool      [default=true|false]
```

Read at runtime via `self.<name>.value`. When the same name is declared via both
a class attribute and an annotation, the class attribute wins.

## Self-Check Before Declaring Done

- [ ] Ran `sdk dp-methods` / `sdk data-requests` / `sdk indicators` before drafting
- [ ] `class Strategy(BaseStrategy):` with aliased import
- [ ] `startup_history` set (integer; NOT `required_history`)
- [ ] `startup_history` >= the longest rolling window in `populate_indicators`
- [ ] Every `Signal.buy()` / `Signal.sell()` / `Signal.target_exposure()` / `Signal.target_quantity()` / `Signal.hold()` has `tag=`
- [ ] No `try / except Exception: pass`; no silent `continue`
- [ ] No `df.iloc[i]` with `i >= 0`; no `df.shift(-n)` with negative n
- [ ] Any use of `pd.*` has `import pandas as pd` at the top
- [ ] Cross-symbol references declared in `informative_data()` or `@informative`
- [ ] Files written via in-process `write_file(file_path="$WORK_DIR/...")` (not CLI)
- [ ] `doyoutrade-cli strategy authoring compile --session-id "$SESSION"` returned no errors
- [ ] `doyoutrade-cli strategy authoring finalize --session-id "$SESSION"` called to persist

## Diagnostic Events (operators grep these)

- `strategy_runner_cycle` — per-cycle summary: signals_buy/sell/hold/target_exposure/target_quantity counts, per_symbol_tags.
- `strategy_dp_<method>` / `strategy_dp_<method>_failed` — each ctx.dp call.
- `strategy_<phase>_failed` — populate_indicators / on_bar / informative_data failures.
- `strategy_prefetch_failed` — informative_data prefetch failure per declared DataRequest.
- `strategy_base_history_insufficient` — base bars below `startup_history`; runner holds.

## References

- `references/sdk-surface.md` — Strategy base class, ctx.dp methods, DataRequest factories.
- `references/error-codes.md` — every error_code with repair recipe.
- `references/indicators.md` — doyoutrade.strategy_sdk.indicators reference.
