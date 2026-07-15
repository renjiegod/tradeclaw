---
name: doyoutrade-sdk
description: Explore the Doyoutrade Strategy SDK surface via `doyoutrade-cli sdk ...` — list DataProvider methods, built-in indicators, DataRequest shapes, validate a draft strategy file (compile + smoke-test), and check recursive-indicator / startup_history stability so backtest values reproduce live. Use when the user asks "什么 indicator 可用 / what's in dp / 我可以用哪些 data request / 试一下我这个策略能编译过吗 / validate my strategy code / 我的 startup_history 够不够 / 这个策略会不会前视 / 指标稳不稳定 / does the backtest reproduce live". Companion to `strategy-definition-authoring` (the deep-dive author skill).
category: reference
style: reference
---

<!-- Routing:
- Author / modify strategy source → `strategy-definition-authoring`
  (write the file locally, then `doyoutrade-cli strategy authoring open |
  compile | finalize`). The `--source-file` / `--class-name` CLI flags
  were removed; source authoring now goes through the authoring lifecycle.
- Run / inspect a definition's existing source_code → `doyoutrade-cli
  strategy definition get` (in `doyoutrade-strategy`).
-->

# doyoutrade-sdk

## When to use

Trigger when the user wants to **discover** what the strategy SDK
offers, or to **validate** a draft they've already written.

## Commands

### `doyoutrade-cli sdk dp-methods`

Lists `DataProvider` methods (`get_data`, `get_indicator`, ...) with
their signatures and docstring summaries. The agent reads this before
writing or editing `Strategy.populate_indicators` / `on_bar(self, df, ctx)`.

```bash
doyoutrade-cli sdk dp-methods
doyoutrade-cli sdk dp-methods | jq '.data.methods[] | {name, signature}'
```

This listing also includes `ctx.dp.watchlist_symbols(tag=None)` — a
read-only, frozen per-cycle snapshot of the symbols in the user's
watchlist (自选股), optionally filtered to one tag (omit / `None` for all).
It returns the symbol list (metadata), not bars; prefetch bars still go
through a `DataRequest`. Authoring details live in
`strategy-definition-authoring`.

### `doyoutrade-cli sdk indicators`

Built-in indicators (e.g. `RSI`, `MACD`, `ATR`) with parameter
schemas. Pair with the corresponding `dp.get_indicator(...)` call when
authoring.

```bash
doyoutrade-cli sdk indicators
```

### `doyoutrade-cli sdk data-requests`

Lists the valid `DataRequest` field shapes for the
`StrategyDefinition.data_requests` declaration — what `kind`s exist,
which fields each kind requires, default values, etc.

```bash
doyoutrade-cli sdk data-requests
```

### `doyoutrade-cli sdk validate <source-file>`

Compile + smoke-test a draft strategy file **without persisting** it.
The file must define a class named `Strategy` (the convention the authoring
lifecycle uses); the `--class-name` flag was removed. Runs the same
compile + smoke gate as `strategy authoring compile` / `finalize`.

```bash
doyoutrade-cli sdk validate ./draft_strategy.py

# In a tight write-validate loop
$EDITOR draft.py && doyoutrade-cli sdk validate draft.py
```

On success, `data.status == "ok"` and the response carries any compile
warnings.

### `doyoutrade-cli sdk validate-recursive <source-file> --symbol <CODE.EXCHANGE>`

Quantify how much the strategy's indicators drift with `startup_history`.
A recursive indicator (EMA / Wilder-RSI / ADX / MACD / ATR …) keeps
warming up for many bars; if the declared `startup_history` is too small,
the live cron path (which only fetches `startup_history` bars) computes a
*different* last-row value than the backtest (which feeds the full
window), so the backtest's edge silently fails to reproduce live.

Compiles + smoke-tests the file, fetches a long reference window of real
OHLCV for `--symbol`, then re-runs `populate_indicators` at several shorter
tail-history lengths and reports each indicator's last-row percent drift
vs its fully-warmed value.

```bash
# Auto ladder (declared, 1.5x, 2x, 3x), latest data, 1% drift tolerance
doyoutrade-cli sdk validate-recursive ./draft.py --symbol 600519.SH

# Pin the window + custom ladder + looser tolerance
doyoutrade-cli sdk validate-recursive ./draft.py --symbol 600519.SH \
  --as-of 2025-06-10 --ladder 30,60,120 --threshold-pct 2.0
```

- `data.status == "stable"` → exit 0; every indicator converged at the
  declared `startup_history`.
- `data.status == "unstable"` → **exit 1** (gate semantics, like
  `sdk validate`). Read `data.unstable_columns` and bump `startup_history`
  to `data.recommended_startup_history`.
- Per-column detail is under `data.indicators[<col>].by_history[<len>]`
  (`value`, `drift_pct`, optional `note`).
- Hard failures (compile / smoke / no data / bad flag) → `ok:false`,
  **exit 2** (`validation_error`); the specific cause is in
  `error.message` (see the error table below).
- Resolve `--symbol` to a canonical `CODE.EXCHANGE` via
  `doyoutrade-cli stock lookup` first.

## Reading tool errors

| `error_code` | Exit | Where | Repair |
| --- | --- | --- | --- |
| `compile_failed` | 1 | `sdk validate` | Fix the Python syntax / runtime error reported in `error.message`. |
| `smoke_runtime_failed` | 1 | `sdk validate` | The class compiled but `Strategy.on_bar(...)` / `populate_indicators` raised under synthetic smoke inputs — fix the data shape or guards. |
| `class_name_mismatch` | 2 | `sdk validate` | The file doesn't declare a class named `Strategy`. Rename the strategy class to `Strategy` (the `--class-name` flag was removed). |
| `validation_error` | 2 | `sdk validate-recursive` (+ any) | All hard failures of `validate-recursive` arrive over the CLI as `validation_error` (exit 2); the **specific reason is in `error.message`** — it names the underlying cause: `runtime_smoke_failed` (missing `on_bar` / `__init__` rejects defaults — run `sdk validate` for the full report), `invalid_symbol` (resolve via `stock lookup`), `insufficient_history` / `data_fetch_failed` / `no_bars` (widen `--as-of`, pick a liquid symbol, or change `--data-source`), `populate_requires_data_provider` (`populate_indicators` calls `ctx.dp.*` — compute indicators purely from the bar DataFrame), `populate_indicators_failed` (compiled but raised on real bars), or a bad `--as-of` / `--ladder` / `--threshold-pct`. Note: `status="unstable"` is **not** an error — it returns `ok:true` and exits 1 (gate). |

See the main-agent system prompt's "CLI envelope 速读" section for the general envelope.

## Combining with bash

```bash
# Validate every Python file in a strategies dir (each must define class Strategy)
for f in strategies/*.py; do
  echo "=== $f ==="
  doyoutrade-cli sdk validate "$f" \
    | jq -c '{ok, error_code: .error.error_code // null}'
done

# Inspect available indicators that match a keyword
doyoutrade-cli sdk indicators | jq '.data.indicators[] | select(.name | test("ma|ema"; "i"))'
```

## What this skill does *not* cover

- Writing the strategy's `source_code` from scratch — see
  `strategy-definition-authoring`.
- Persisting a validated draft to a `StrategyDefinition` —
  `doyoutrade-cli strategy authoring open` → write the file →
  `strategy authoring compile` → `strategy authoring finalize`
  (`--source-file` / `--class-name` were removed). The authoring lifecycle
  embeds the same compile + smoke-test gate this skill's `sdk validate` runs.
- Field-by-field SDK docs — `strategy-definition-authoring/references/`
  hosts those.
