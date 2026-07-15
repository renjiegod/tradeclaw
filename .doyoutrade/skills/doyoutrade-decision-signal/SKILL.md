---
name: doyoutrade-decision-signal
description: Inspect and verify persisted decision signals (决策信号, dsig-…) with `doyoutrade-cli decision-signal list/get/evaluate`, and record a new conversational decision with the in-process `record_decision_signal` tool. Use when the user asks "记录这个买入决策 / 我这个信号后来对了吗 / 信号命中率 / verify the signal / list decision signals / 重新评估信号". Signals also flow in automatically from finished backtests; outcomes (hit/miss/neutral) are computed against cached daily bars per horizon.
category: tool
style: process
---

<!-- Routing:
- Reads + re-evaluation go through `execute_bash doyoutrade-cli decision-signal ...`
  (thin envelope adapter over the API server's `/decision-signals` endpoints;
  server down → structured `api_unavailable`).
- Recording a NEW signal from conversation is the in-process tool
  `record_decision_signal` (NOT a CLI command). Backtest-sourced signals are
  written automatically when a backtest run finalizes — never hand-record those.
- Symbols must be canonical (600519.SH) — run `doyoutrade-cli stock lookup` first.
-->

# doyoutrade-decision-signal

## What a decision signal is

A durable, attributable trading decision — `dsig-…` row in `decision_signals`:

- **source**: `backtest` (auto-extracted from a finished run's fills),
  `assistant` (recorded from conversation via `record_decision_signal`),
  `strategy` (reserved).
- **action** (八态): `buy` / `sell` / `hold` / `add` / `reduce` / `watch` /
  `take_profit` / `stop_loss`.
- **status**: `active` → (`evaluated` | `expired` | `invalidated`). Expiry is
  lazy: overdue `active` signals flip to `expired` on the next `list`.
- **outcome** (per `horizon` × `engine_version`, unique): `hit` / `miss` /
  `neutral`, with `entry_price` / `exit_price` / `return_pct` /
  `max_gain_pct` / `max_drawdown_pct` computed from cached daily bars strictly
  AFTER the anchor date (entry = first post-anchor open).
- Attribution: `run_id` / `task_id` / `cycle_run_id` / `trace_id` /
  `session_id` — a backtest signal is reachable from its run like any cycle
  artifact.

## When to use

- "我的信号最近准不准 / list signals for 600519" → `decision-signal list`.
- "看这条信号的验证结果" → `decision-signal get <dsig-…>` (includes `outcomes`).
- "重新评估 / 换个窗口验证" → `decision-signal evaluate <dsig-…> --horizon 10d`.
- "记录一下：我建议买入 XX，目标价 YY" → in-process `record_decision_signal`.

## Commands

### `doyoutrade-cli decision-signal list`

```bash
doyoutrade-cli decision-signal list --symbol 600519.SH --status evaluated
doyoutrade-cli decision-signal list --run-id <btjob-...> --limit 100
```

Filters: `--task-id` / `--run-id` / `--symbol` / `--status` / `--limit` /
`--offset`. `data.items[]` rows carry the fields above; `data.expired_now` is
how many overdue signals were lazily expired by this call.

### `doyoutrade-cli decision-signal get <dsig-…>`

```bash
doyoutrade-cli decision-signal get dsig-1a2b3c4d5e6f
```

Returns the signal plus `outcomes[]` (one per horizon × engine_version).

### `doyoutrade-cli decision-signal evaluate <dsig-…>`

```bash
doyoutrade-cli decision-signal evaluate dsig-1a2b3c4d5e6f --horizon 5d
```

Reads cached daily bars after the signal's anchor date, computes the outcome,
and upserts it (idempotent — re-running replaces the row for that horizon).
`data.status == "skipped"` with `reason=data_insufficient` means not enough
post-anchor bars are cached yet: backfill with `doyoutrade-cli data run` for
that symbol/date range, then evaluate again. This is a normal condition, not
an error.

## Recording a signal from conversation (in-process tool)

Minimal valid payload:

```json
{
  "symbol": "600519.SH",
  "action": "buy"
}
```

Full example:

```json
{
  "symbol": "600519.SH",
  "action": "buy",
  "confidence": 0.7,
  "horizon": "5d",
  "target_price": "1800.00",
  "stop_loss": "1650.00",
  "reason": "放量突破箱体上沿，白酒板块回暖",
  "expires_in_days": 10,
  "metadata": {"theme": "白酒"}
}
```

Rules the tool enforces (violations return structured errors, never silent
coercion): `symbol` canonical and non-empty; `action` one of the 八态; prices
are decimal STRINGS (`"1800.00"`, never floats); `confidence` in [0, 1];
`horizon` like `"5d"`. The calling session is attributed automatically — do
not pass any session id. A repeat call for the same (symbol, action, horizon)
in the same session returns `deduped: true` with the existing `signal_id`.

## Reading tool errors

| error_code | Meaning | What to do |
| --- | --- | --- |
| `decision_signal_not_found` | Unknown `dsig-…` id (404). | `decision-signal list` to find the right id; don't guess ids. |
| `validation_error` | Bad input: unknown action, non-decimal price string, bad horizon, confidence out of [0,1]. | Fix the named field per the message; prices must be decimal strings. |
| `invalid_metadata_json` | `metadata` was not a JSON object (or an unparsable JSON string). | Pass a real JSON object, e.g. `{"theme": "白酒"}`. |
| `decision_signal_unwired` | Runtime has no decision-signal repository wired (tool not registered / server started without DB). | Report the wiring gap; do not retry — use the CLI read path if the API server is up. |
| `decision_signal_write_failed` | DB write failed after validation passed. | Check server logs / DB connectivity; retry once at most. |
| `api_unavailable` | CLI could not reach the API server. | Start the server (`uv run doyoutrade`), then retry. |
| `unknown_arguments` | Top-level kwarg typo (schema is `additionalProperties: false`). | Use only the documented keys; follow `suggested_path` if present. |
