---
name: doyoutrade-backtest
description: Manage Doyoutrade backtest runs via `doyoutrade-cli backtest ...` — start a fresh run (`backtest run`), stream a live run's status (`backtest watch`), re-fetch a finished run's report (`backtest summary`), get an iteration recommendation (`backtest suggest-iteration`), or check out-of-sample robustness across time (`backtest walk-forward`). Use when the user asks "跑一个回测 / start a backtest / 盯一下这个 run / watch the backtest / 看一下报告 / show me the summary / 下一步该怎么改 / what should I try next / 这个策略是不是过拟合 / 换个时间段还成立吗 / 样本外验证 / walk-forward / out-of-sample / does the edge generalise". Companion to `doyoutrade-task` (task lifecycle), `doyoutrade-strategy` (binding), and `doyoutrade-debug` (deep trace).
category: tool
style: process
---

<!-- Routing:
- Inspect debug spans / model_invocations / cycle_runs for a finished
  run → `doyoutrade-debug` (`debug get-run-view`).
- Edit a strategy's source_code after a failed run →
  `strategy-definition-authoring`.
-->

# doyoutrade-backtest

## When to use

Trigger this skill when the user (or another skill) wants to **observe a
running or recently-finished backtest** rather than start a new one. The
common shapes:

- "盯一下这个 run / poll until it finishes"
- "stream the backtest progress to my console"
- "wait for run X to terminate, then continue"
- "tell me when it's done" (combined with cron / chat reply)

If the user wants to *start* a backtest, run `doyoutrade-cli backtest
run ...` first. Its return carries the `run_id` you'll feed to
`backtest watch`.

**Prefer `watch_job` over blocking `backtest watch` for "通知我" intents**:
the in-process `watch_job(job_id=<run_id>, note=...)` tool registers a
completion wake-up — end your turn immediately and the system pushes the
report summary back into this session when the run reaches a terminal
status. Use `backtest watch` only when the user wants to *actively stream*
progress right now. Do NOT create a cron job to poll run status.

## Streaming contract

`backtest watch` is the CLI's first long-running command and follows the
same shape any future streaming command will. Memorise the three lines
the contract guarantees:

| Surface | Format | Note |
| --- | --- | --- |
| stderr ready marker | `[doyoutrade] ready kind=backtest_watch run_id=<id>\n` | One line, fields appended only — never reordered. |
| stdout | NDJSON — one envelope per line, ASCII-safe JSON | De-duplicated: identical consecutive snapshots are skipped. |
| stderr exit marker | `[doyoutrade] exited — received N event(s) in T (reason: <reason>)\n` | `reason ∈ {terminal, limit, timeout, signal}`. |

The process exit code is **always 0**. Branch on the `reason` token in
the stderr marker, not on `$?`.

## Commands

### `doyoutrade-cli backtest run`

Kick off a backtest run. Two input modes:

```bash
# Task mode — task already carries its strategy binding + universe
doyoutrade-cli backtest run --task 550e8400-... \
  --range-start 2024-01-01 --range-end 2024-06-01

# Definition mode (default post-authoring) — bind the definition directly
doyoutrade-cli backtest run --definition sd-3f1c2a9b8e7d \
  --params '{"window": 14}' \
  --range-start 2024-01-01 --range-end 2024-06-01 \
  --universe 600519.SH,000001.SZ

# Fire-and-forget (returns immediately) — pair with `backtest watch`
doyoutrade-cli backtest run --definition sd-... \
  --params '{"window": 14}' \
  --range-start 2024-01-01 --range-end 2024-06-01 \
  --universe 600519.SH --timeout 0
```

| Flag | Required | Notes |
| --- | --- | --- |
| `--task` xor `--definition` | yes | Exactly one entry mode. |
| `--range-start / --range-end` | yes | Inclusive `YYYY-MM-DD`. |
| `--universe` | yes in `--definition` mode | Comma-separated symbols. |
| `--params` | no | Strategy parameter overrides for `--definition` mode. |
| `--timeout` | no (default 120) | Wait seconds. `0` = fire-and-forget. |
| `--config-overrides` | no | JSON object merged onto task settings for this run. |
| `--debug / --no-debug` | no (default `--debug`) | `--no-debug` runs in fast mode (see below). |
| `--progress / --no-progress` | no (default auto) | Live progress bar on **stderr** while waiting. Auto = on for an interactive TTY, off otherwise — so agents (non-TTY) see nothing and the stdout JSON envelope is unchanged. Human-facing only. |
| `--name / --data-provider / --market-profile / --bar-interval / --model-route / --poll-interval` | no | Per-run overrides. |

**Debug vs fast mode.** By default (`--debug`) a run records the full trace —
debug session, OTel spans, per-bar cycle runs and model invocations — so
`doyoutrade-cli debug get-run-view <btjob-...>` can replay it. Pass `--no-debug`
to run in **fast mode**: that trace persistence is skipped, so the backtest
runs noticeably faster, but only the run status, report and trade fills are
kept. Under `--no-debug`, `debug get-run-view` returns `debug_enabled: false`
with empty `spans` / `cycle_runs` / `model_invocations` **by design** — that is
not an error. Use fast mode for quick parameter sweeps; keep `--debug` when you
intend to inspect why a run behaved the way it did.

Default mode (positive `--timeout`) waits for terminal status and returns
structured JSON from the OpenAPI `POST /backtest-runs` endpoint. Prefer
reading `data.summary` directly as the machine-readable truth; do not
expect `report_path` from this CLI path. For a human-facing report, call
`doyoutrade-cli backtest summary <btjob-...> --format markdown` and forward
`data.markdown`. For long-running production-sized backtests, prefer
`--timeout 0` + `backtest watch <run_id>` so the agent isn't blocked.

### A股交易费用（佣金 / 印花税 / 过户费）

Backtests are **fee-free by default** — fills record at the raw price with no
transaction cost, so equity / `return_pct` / realized PnL are pre-cost. For a
realistic result (especially short-holding / 做T / high-turnover strategies,
which look profitable fee-free but lose money live) enable the A-share fee
model via the run's `config_overrides`:

```bash
# Default A-share rates (佣金 万2.5 min 5元, 印花税 0.05% 卖出, 过户费 0.001%)
doyoutrade-cli backtest run --definition sd-... --universe 600519.SH \
  --range-start 2024-01-01 --range-end 2024-06-01 \
  --config-overrides '{"settings": {"fee_config": {"enabled": true}}}'

# Custom rates (any omitted key falls back to the A-share default)
... --config-overrides '{"settings": {"fee_config": {"commission_rate": 0.0003, "min_commission": 5, "stamp_tax_rate": 0.0005, "transfer_fee_rate": 0.00001}}}'
```

Fee config can also live permanently on the task settings (`settings.fee_config`)
so every run of that task is fee-aware. When enabled:

- the ledger deducts 佣金+过户费 on buys and 佣金+印花税+过户费 on sells, so the
  equity curve and `return_pct` reflect cost;
- realized PnL per closed trade is **net of both legs' fees** (full口径,
  reconciles with the equity curve);
- `data.summary.total_fees` reports the total fee drag (元).

`fee_config` absent / empty / `{"enabled": false}` → no fees, identical to the
historic default. `walk-forward` accepts the same `fee_config` on the task.

### 退出归因：`data.summary.by_exit_reason`

When a strategy tags its exits with `Signal.sell(exit_reason=...)` (or a
task-level exit engine fires), the summary carries a `by_exit_reason` array
parallel to `by_symbol` — one row per exit kind with `trade_count_closed`,
`pnl`, `win_rate`, `win_rate_sample_size`, `avg_holding_trading_days`,
pre-sorted by descending `|pnl|`. Reasons: `signal`, `stop_loss`,
`take_profit`, `trailing_stop`, `roi`, `circuit_breaker`. Use it during
iteration to see *which kind of exit* drives or drags returns — e.g.
"take_profit 笔多但 stop_loss 吃掉了大半 PnL". The block is **empty** (`[]`)
when no closed round-trip carried a reason (the default), so don't assume it's
populated. The markdown report renders it as a 「按退出原因拆解」 table when
non-empty. (Authoring exits with reasons: see the strategy-definition-authoring
skill's "Exit Reason" section.)

### 因子归因：`data.summary.by_tag`

Parallel to `by_exit_reason`, the summary also carries `by_tag` — closed
round-trips grouped by the **entry** factor tag (`Signal.buy(tag=...)`), one row
per tag with `trade_count_closed` / `pnl` / `win_rate` / `win_rate_sample_size`
/ `avg_holding_trading_days`, sorted by descending `|pnl|`. Since every
`Signal.buy` is required to carry a `tag`, this block is populated for any real
strategy backtest — use it to see *which factor combination actually carried
the PnL* (e.g. "ma_cross+rsi: 70% 胜率, breakout: 40%"). Pairs with
`by_exit_reason`: `by_tag` answers which factor opened the trade, `by_exit_reason`
which exit kind closed it. Empty `[]` only when no closed round-trip's entry was
tagged (e.g. legacy fills). Markdown report renders it as 「按入场因子拆解」.

### 风险护栏：组合熔断 + 仓位集中度

Two opt-in risk knobs on the task settings (apply to backtest, paper, and live):

- **组合熔断 (drawdown circuit breaker)** — `settings.protection.max_drawdown_pct`
  (e.g. `0.2`). When the account's peak-to-trough equity drawdown breaches the
  threshold, the worker **halts new BUY entries** for the cycle (SELL/exit still
  dispatch, so positions can be unwound) and emits a `protection_triggered`
  debug event; the blocked buys count as vetoes. Default-off: absent / empty /
  `{"enabled": false}` → no halt, loop unchanged.
- **单名集中度 (single-name cap)** — `settings.position_constraints.max_position_ratio`
  (in `(0, 1]`). Now **enforced** as a sizing cap: an over-cap buy is scaled
  down to `equity * ratio` (not vetoed/ignored), emitting
  `position_manager_ratio_capped`. Default `1.0` is non-binding.
- **A股整手 (board lot)** — `settings.position_constraints.lot_size` (integer
  ≥ 1; A股 = `100`). Applies to the `target_quantity` / `target_exposure`
  rebalance paths: buy and partial-sell deltas are floored to lot multiples
  (a sub-lot buy/sell is skipped with `target_quantity_buy_below_one_lot` /
  `target_exposure_sell_below_one_lot` etc.; a non-lot target emits
  `position_manager_target_quantity_lot_aligned`). **Full exits (target 0) are
  exempt** so odd lots always clear. Default `1` = whole-share trading.
- **网格防抖 (rebalance hysteresis)** —
  `settings.position_constraints.rebalance_hysteresis_lots` (integer ≥ 0). A
  rebalance whose share delta is below `rebalance_hysteresis_lots * lot_size`
  is skipped (`position_manager_skipped` / `hysteresis_dead_band`) so a grid
  oscillating around a band edge does not churn. Full exits bypass the band.
  Default `0` = disabled. Set `1` for a one-lot dead band on A股 grids.

Enable via `task create/update --config-overrides '{"settings": {"protection": {"max_drawdown_pct": 0.2}}}'`
or persist in the task settings. Inspect a halted cycle's `protection_triggered`
/ `intent_protection_halted` events with `doyoutrade-cli debug get-run-view`.

### `doyoutrade-cli backtest summary <run_id>`

Re-fetch a finished run's persisted summary. JSON is the default and is
the expected agent-facing format. Markdown is the expected user-facing
report format:

```bash
doyoutrade-cli backtest summary btjob-9b8e2c1a3f5d                       # default json
doyoutrade-cli backtest summary btjob-9b8e2c1a3f5d --format json
doyoutrade-cli backtest summary btjob-9b8e2c1a3f5d --format markdown     # user-facing report
```

The CLI envelope is always JSON; `--format` controls the body
encoding. Use markdown for human report rendering, and JSON for programmatic
inspection. Prefer the `btjob-...` id returned by `backtest run`; the
rendered report separately names the final cycle run when available.

### `doyoutrade-cli backtest suggest-iteration <run_id>`

```bash
doyoutrade-cli backtest suggest-iteration run-9b8e2c1a3f5d
```

Inspects the run's debug view and recommends the next iteration step.
`data.suggestion.action_type` is one of:

* `definition_change` — definition risks / code-level issues found.
* `parameter_only` — final target produced no allocations; tune the
  `parameter_overrides` first (on the task, or via `backtest run --params`).
* `task_change` — no useful spans/model_invocations; verify the
  definition binding / task config.

`data.suggestion.recommended_tools` lists the next concrete CLI
commands (e.g. `doyoutrade-cli strategy definition update`,
`doyoutrade-cli task update`, `doyoutrade-cli strategy bind`).

### `doyoutrade-cli backtest walk-forward --definition sd-... --universe ...`

Out-of-sample robustness: split a date range into N consecutive windows and
run the **same** strategy + parameters on each as its own backtest, then
report per-window return / Sharpe / drawdown / trades. Answers "does the edge
hold across time, or did it only work on the one window the author tuned?" —
the core overfitting check for LLM-authored strategies.

```bash
# Default 3 windows over the range, fast mode, 1% gate
doyoutrade-cli backtest walk-forward --definition sd-3f1c2a9b8e7d \
  --universe 600519.SH,000001.SZ \
  --range-start 2023-01-01 --range-end 2024-12-31

# 4 windows, custom params, keep the per-window tasks to drill into
doyoutrade-cli backtest walk-forward --definition sd-... \
  --universe 600519.SH --params '{"window": 14}' \
  --range-start 2023-01-01 --range-end 2024-12-31 \
  --segments 4 --keep-tasks
```

| Flag | Required | Notes |
| --- | --- | --- |
| `--definition` | yes | Definition mode only (`sd-...`); each window auto-creates its own backtest task. |
| `--universe` | yes | Comma-separated symbols. |
| `--range-start / --range-end` | yes | Full range `YYYY-MM-DD`; split into `--segments` windows. |
| `--params` | no | Parameter overrides (same across every window). |
| `--segments` | no (default 3) | Number of consecutive windows, 2–6. |
| `--min-trades` | no (default 1) | A window with fewer closed trades is "inconclusive", excluded from the verdict. |
| `--keep-tasks` | no (default off) | Keep the per-window tasks (and `run_id`s) for drill-in; default deletes them after collecting the summary. |
| `--timeout` | no (default 120) | Per-window completion timeout; the HTTP call blocks for the whole sweep (sized as `segments × timeout`). |

Verdict in `data.status`:

* `robust` → exit 0; every window that traded was profitable — the edge holds out of sample.
* `fragile` → **exit 1** (gate); the edge only held in some windows — likely in-sample overfit or regime-dependent. Read `data.windows[].return_pct` to see which periods failed.
* `inconclusive` → exit 0; fewer than 2 windows traded enough — widen the range or lower `--min-trades`.

`data.windows[]` carries per-window `range_start/range_end`, `return_pct`,
`sharpe`, `max_drawdown_pct`, `trade_count_closed`, and `eligible`. Note
`data.reoptimization` is always `false`: this is fixed-parameter multi-window
OOS, **not** re-optimising walk-forward (that needs an automated parameter
search, which is a separate capability). Hard failures (unknown definition,
range too short, every window failed) come back as `ok:false` exit 2 with the
reason in `error.message` (`all_windows_failed` / `range_too_short_for_segments`
/ `invalid_segments` / `missing_universe` / strategy-definition lookup errors).

### `doyoutrade-cli backtest watch <run_id>`

```bash
# Block until terminal (default --until terminal). Polls every 2s.
doyoutrade-cli backtest watch run-9f8a3c1b2e7d

# Faster polling, hard cap of 5 minutes.
doyoutrade-cli backtest watch run-9f8a3c1b2e7d --interval 0.5 --timeout 300

# One-shot status snapshot — useful as a status probe.
doyoutrade-cli backtest watch run-9f8a3c1b2e7d --max-events 1

# Open-ended stream that ignores terminal status; caller must signal.
doyoutrade-cli backtest watch run-9f8a3c1b2e7d --until none --timeout 600
```

| Flag | Default | Notes |
| --- | --- | --- |
| `<run_id>` | — | The `run_id` returned by `doyoutrade-cli backtest run` inside `backtest_job`. |
| `--interval N` | `2.0` | Poll seconds (lower bound 0.5). |
| `--max-events N` | `0` (unlimited) | Stop after emitting N envelopes. |
| `--timeout T` | `0.0` (none) | Stop after T seconds elapsed. |
| `--until terminal\|none` | `terminal` | `terminal` exits when status hits completed / finished / failed / cancelled. `none` ignores status; user must signal. |

Each emitted envelope wraps a `GetBacktestSummaryTool --format json`
snapshot. Use `doyoutrade-cli schema backtest.watch` to dump the exact
`data` shape.

### Snapshot shape (success)

```json
{
  "ok": true,
  "data": {
    "status": "ok",
    "summary": {
      "starting_equity": 1000000.0,
      "ending_equity": 1043280.5,
      "return_pct": 4.328,
      "max_drawdown_pct": 1.21,
      "win_rate": 0.58,
      "trade_count_closed": 21,
      "trade_count_open": 0,
      "fills_count": 42,
      "...": "..."
    },
    "run": {
      "run_id": "...",
      "status": "completed",
      "starting_equity": 1000000.0,
      "ending_equity": 1043280.5,
      "error_message": null
    },
    "_summary": "Backtest run-... completed."
  },
  "meta": {...}
}
```

### Error envelope (non-fatal — still emitted as one NDJSON line)

```json
{
  "ok": false,
  "error": {
    "error_code": "backtest_summary_not_ready",
    "message": "...",
    "hint": "the run is in flight — re-poll later"
  }
}
```

A non-terminal error like `backtest_summary_not_ready` is the normal
"backtest is still warming up" signal. The watch loop keeps polling
through it; it stops only when status enters `_TERMINAL_STATUSES` or
one of the explicit limits fires.

## Reading tool errors

| `error_code` | Continues watching? | Meaning |
| --- | --- | --- |
| `backtest_summary_not_ready` | yes | Run row exists but the finalize step hasn't persisted yet. |
| `backtest_summary_not_found` | no (no run to watch) | Wrong `run_id`. The watch emits this once then exits at `--max-events` or via terminal heuristic in older patches. Verify via `doyoutrade-cli cycle list`. |
| `backtest_summary_stale` | maybe | A newer backtest overwrote the summary; payload carries `latest_summary_run_id` so you can re-`watch` that one. |

See the main-agent system prompt's "CLI envelope 速读" section for the general envelope and exit-code
rules.

## Stopping the watch cleanly

Three ways to stop, in increasing order of "polite":

1. **`--until terminal` reaches a terminal status**. Most common. Loop
   exits naturally; reason `terminal`.
2. **`--max-events N` / `--timeout T` fires**. The loop terminates at the
   next check; reasons `limit` / `timeout`.
3. **Caller closes stdin** (e.g. `bash` parent exits). The watch
   detects EOF and exits with reason `signal`. This is the preferred
   shutdown path for `execute_bash`-driven subprocesses — no signal
   needed.

`SIGINT` / `SIGTERM` are also honoured and map to `reason: signal`.

## Combining with bash

```bash
# Wait for one specific run to terminate; capture the final status
final=$(doyoutrade-cli backtest watch run-9f8a3c1b2e7d --until terminal 2>/dev/null \
  | tail -1 \
  | jq -r '.data.run.status // .error.error_code')
echo "final status: $final"

# Race: kick off a backtest, then watch it with a 10-minute upper bound
# (`doyoutrade-cli backtest run --timeout 0` returns once the job is queued).
RUN_ID=$(doyoutrade-cli backtest run --task <id> --timeout 0 | jq -r '.data.run_id')
doyoutrade-cli backtest watch "$RUN_ID" --timeout 600 --interval 1 \
  | jq -c 'select(.ok) | .data.summary | {return_pct, max_drawdown_pct}'

# Quick "is it still running?" probe — one envelope, exits in <2s
doyoutrade-cli backtest watch "$RUN_ID" --max-events 1 \
  | jq -r '.data.run.status // .error.error_code'
```

The de-duplication step inside the watch means a long-idle "running"
backtest emits exactly one envelope at the start of the watch and one
when its status transitions, not one per poll — `tail -1` always gets
the most recent state.

## What this skill does *not* cover

- Starting a backtest run (use `doyoutrade-cli backtest run`).
- Deep debug / cycle-by-cycle inspection (use `doyoutrade-cli debug
  get-run-view`).
- Listing all runs on a task (use `doyoutrade-cli cycle list`).
