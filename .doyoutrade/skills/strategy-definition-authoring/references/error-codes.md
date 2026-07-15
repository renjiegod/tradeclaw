# Error Codes & Repair Recipes

Stable error_code vocabulary returned by `doyoutrade-cli strategy authoring compile`,
`doyoutrade-cli sdk validate`, and runtime debug events. Load when a
command returned an `error_code` you don't recognize, or when a backtest
is repeatedly hitting one.

For the SDK surface see [`sdk-surface.md`](sdk-surface.md).
For indicator helpers see [`indicators.md`](indicators.md).

## Where the gate runs

The compile + smoke gate runs inside `doyoutrade-cli strategy authoring compile`
and `doyoutrade-cli sdk validate` (CLI dry-run, no DB writes).
`doyoutrade-cli strategy authoring finalize` re-runs the gate before persisting, so
failures leave the existing snapshot untouched (`persisted: false`).

These legacy tools (`generate_strategy_definition`,
`create_strategy_definition_from_source`, `validate_strategy_code`) were
removed in Task 6 (2026-05-24, strategy-as-files refactor). Strategy
authoring now exclusively goes through the `doyoutrade-cli strategy authoring`
lifecycle (`open` / `compile` / `finalize` / `cancel`), with source edits
done via the in-process file primitives (`write_file` / `edit_file` /
`read_file` / `list_files`).

The top-level response always carries `stage: "compile" | "smoke"` so
you can tell whether the AST pass or the synthetic-data pass triggered.

## Compile-time error codes

### `disallowed_import`
You imported something outside the whitelist (`__future__`, `decimal`,
`math`, `numpy`, `pandas`, `doyoutrade.strategy_sdk`, `typing`). **Fix:**
remove the import. All data access goes through `ctx.dp.*`.

### `syntax_error`
Standard Python `SyntaxError` from the source. `lineno` / `offset` point
at the problem. **Fix:** the syntax.

### `missing_required_class`
The compiled module didn't define a class with the expected `class_name`.
**Fix:** ensure `class <ExpectedName>(Strategy):` is in the source.

### `invalid_base_class`
The class doesn't inherit from `Strategy`. **Fix:** `class X(Strategy): ...`.

### `missing_on_bar`
Subclass didn't override the abstract `on_bar`. **Fix:** implement
`on_bar(self, df, ctx) -> Signal`.

### `missing_signal_tag`
A `Signal.buy()`, `Signal.sell()`, `Signal.target_exposure()`, or
`Signal.target_quantity()` call has no `tag=` keyword (or has `tag=""`).
Tag is mandatory so
`trade_fills.entry_tag` / `exit_tag` and signal diagnostics can attribute
the decision. **Fix:** `Signal.buy(tag="factor_name")`.

### `invalid_target_exposure`
`Signal.target_exposure(target=...)` received a literal outside `[0, 1]`.
`0` means flat; `1` means fully allocated. **Fix:** use a fraction inside
`[0, 1]`, e.g. `0.25` / `0.5` / `0.75`.

### `invalid_target_quantity`
`Signal.target_quantity(quantity=...)` received a literal below `0`.
`0` means flat; positive values are absolute share counts. **Fix:** use a
non-negative quantity such as `100` / `200` / `300`.

### `lookahead_access`
You read forward in time: either `df.iloc[i]` with `i >= 0` (positive
index) or `df.shift(-n)` (negative shift). **Fix:** use `df.iloc[-1]`
(current bar), `df.iloc[-2]` (prior bar), or `df.shift(N)` with `N >= 1`.

### `populate_cross_symbol_access`
Inside `populate_indicators` you called `ctx.dp.get_bars(symbol=<other>)`.
populate_indicators is per-symbol vectorized; cross-symbol reads break
that model. **Fix:** declare via `@informative('1d', symbol=X)` (vectorized
on the other symbol), or move the read into `on_bar` and pre-declare the
symbol in `informative_data()`.

### `silent_exception_swallow`
A broad `except Exception:` / `except:` followed by `pass` / silent
`continue` / bare `return`. CLAUDE.md's "错误可见性" rule forbids hidden
failures. **Fix:** either narrow the exception type *and* add
`logger.warning` + `emit_debug_event` with `reason` + `hint`, or remove
the try/except so failures propagate.

### `silent_type_coercion`
`if not isinstance(x, T): x = default` — masks shape violations behind a
default. **Fix:** raise `ValueError` / `TypeError` with the actual type
and value so the caller sees the mismatch.

### `unknown_dp_method`
`ctx.dp.<name>()` where `<name>` isn't registered. The compiler's
whitelist tracks `_REGISTERED_DP_METHODS`. **Fix:** run
`doyoutrade-cli sdk dp-methods` to see the registered set, or use a different
access pattern.

### `unknown_data_request_type`
`DataRequest.<name>()` where `<name>` isn't a registered factory. **Fix:**
run `doyoutrade-cli sdk data-requests` to see the registered set.

### `history_check_literal_disallowed`
A `rolling(N)` literal where `N > startup_history`. The smoke data has
only `startup_history` rows; this rolling produces all-NaN. **Fix:**
raise `startup_history` to ≥ N, or use a smaller window.

### `invalid_class_attribute`
A class attribute has the wrong type or value:
- `timeframe` must be one of `"1m"/"5m"/"15m"/"30m"/"60m"/"1d"/"1w"/"1mo"` (hourly is `"60m"` not `"1h"`; monthly is `"1mo"`; no `"4h"`).
- `startup_history` must be a positive int.
- `name` must be a string.

### `invalid_informative_decorator`
`@informative(...)` arguments are malformed (unknown timeframe, empty
symbol string, etc.). **Fix:** check the signature in
[`sdk-surface.md`](sdk-surface.md).

### `compile_runtime_error`
The compile-time `exec(...)` raised something other than the categorized
errors above (e.g. ImportError from an SDK symbol that doesn't exist).
**Fix:** inspect the message; usually a typo or a reference to a
removed/renamed symbol.

## Smoke-test error codes (only reached after compile success)

### `runtime_smoke_failed`
Strategy instantiation or `populate_indicators` / `on_bar` raised on at
least one of the synthetic regimes (monotone / flat / zigzag).
`traceback_excerpt` shows where. Common causes:
- `AttributeError` on a `ctx.dp` method that exists but rejected the args
- `KeyError` when reading a column that `populate_indicators` didn't add
- `IndexError` when reading `df.iloc[-2]` on a single-row frame (gate with `len(df) >= 2`)
- `pd.isna` not used before reading a fresh indicator → NaN comparison

**Fix:** read `traceback_excerpt` and either guard the path or pre-condition
the data.

### `smoke_output_invalid`
`populate_indicators` returned non-DataFrame, or `on_bar` returned non-Signal.
**Fix:** ensure both `return df` (with new columns) and `return Signal.buy(...) / .sell(...) / .target_exposure(...) / .hold()`.
When using strict inventory grids, `Signal.target_quantity(...)` is equally valid.

## Runtime error codes (only triggered during a real cycle)

### `data_insufficient`
`ctx.dp.get_bars(window=N)` got fewer than N rows. **Fix:** reduce
`window`, raise `startup_history`, or verify the symbol has enough listed
history by `ctx.now`.

### `invalid_symbol`
Symbol argument was empty, malformed, or not a known instrument.

### `invalid_argument`
A numeric / shape argument failed validation (e.g. `window <= 0`,
`top_n` not int).

### `informative_data_not_declared`
You tried to read `ctx.dp.get_bars(symbol=X)` for a symbol not declared
in `informative_data()`. **Fix:** add `DataRequest.bars(symbol=X, ...)`.

### `industry_resolution_failed`
`$self.industry` couldn't be resolved (no industry mapping for the
current symbol). **Fix:** verify symbol is a listed equity with a
registered industry; the runtime may need an IndustryResolver wired.

### `live_only_method`
You called `ctx.dp.ticker()` / `orderbook()` in backtest mode. **Fix:**
use `get_bars(window=1)` for the last close in backtest; reserve
ticker/orderbook for live runs.

### `invalid_populate_indicators_return`
`populate_indicators` returned non-DataFrame at runtime. (Same as
smoke-time `smoke_output_invalid`, but caught later.)

### `invalid_on_bar_return`
`on_bar` returned non-Signal at runtime.

### `invalid_informative_data_return`
`informative_data` returned something other than a sequence of
DataRequest instances. **Fix:** use `DataRequest.bars(...)` /
`DataRequest.peers(...)` / etc. factories, not raw dicts.

## Naming convention

`error_code` tokens are stable strings — once shipped, they don't get
renamed. Skill documentation, debug events, and operator dashboards all
key off these tokens. If you genuinely need a new failure mode, add a
new code rather than overloading an existing one.
