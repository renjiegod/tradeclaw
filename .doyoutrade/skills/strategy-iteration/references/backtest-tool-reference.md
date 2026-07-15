# `doyoutrade-cli backtest run` & `backtest summary` Reference

Read this file when you're about to launch the next backtest in an iteration
loop, are decoding a structured error from one of these commands, or need
to pull a finished-run summary.

## `doyoutrade-cli backtest run` payload

Default behavior: **waits for terminal status** (`--timeout 120`). Set
`--timeout 0` for fire-and-forget — pair with `doyoutrade-cli backtest
watch <run_id>` instead of polling manually.

### Task mode (run an existing backtest task)

```bash
doyoutrade-cli backtest run \
  --task <uuid> \
  --range-start 2026-01-01 \
  --range-end 2026-01-10
```

### Definition mode (auto-create a backtest task from a strategy definition, then run it)

```bash
doyoutrade-cli backtest run \
  --definition sd-abc123 \
  --params '{"window": 14}' \
  --universe 600000.SH \
  --range-start 2026-01-01 \
  --range-end 2026-01-10
```

Definition mode binds the `sd-…` directly (the `--params` object lands
on the auto-created task's `settings.strategy.parameter_overrides`) and
returns `data.auto_created_task_id` alongside `data.backtest_job` so the
agent can fetch / iterate on the new task later. There is no strategy-
instance layer.

### Optional `--config-overrides`

Only these top-level keys are accepted; unknown keys are rejected by
the platform:

```bash
doyoutrade-cli backtest run \
  --task <uuid> \
  --range-start 2026-01-01 --range-end 2026-01-10 \
  --config-overrides '{
    "settings": {"...": "deep-merged onto task settings"},
    "universe": ["600000.SH", "000001.SZ"]
  }'
```

## Backtest tasks are one-shot

A backtest task can only carry **one** run. Once a backtest has been
started for a task, future calls return an envelope like:

```json
{"ok": false, "error": {"error_code": "backtest_run_already_exists", "existing_run_ids": [...]}}
```

## `doyoutrade-cli backtest run` error_code reference

- `wrong_identifier_type` — `--task` got an `sd-…` (a strategy
  definition) instead of a task uuid, or `--definition` got something
  that isn't `sd-…`. Look up the right task with `doyoutrade-cli task
  list` / `task get`, or the definition with `doyoutrade-cli strategy
  definition list`. Do not retry as-is.
- `invalid_config_overrides_json` — `--config-overrides` was a JSON
  string that failed to parse. Fix the JSON.
- `invalid_params_json` — CLI layer detected the `--config-overrides`
  payload isn't a JSON object. Wrap fields in `{}`.
- `backtest_run_already_exists` — the task already has a run. Envelope
  may include `existing_run_ids`, `existing_run_id`,
  `existing_run_status`. Inspect with
  `doyoutrade-cli debug get-run-view <existing_run_ids[0]>` /
  `doyoutrade-cli cycle list <task>` before doing anything. If the existing
  run is still `running`, calling `doyoutrade-cli backtest run --task
  <task_id>` again attaches and waits (or use `doyoutrade-cli backtest
  watch` to stream).
- `backtest_run_failed` — the existing run terminated in `failed`
  state. Envelope carries `existing_run_id` and `existing_run_status:
  "failed"`. Inspect first, fix the underlying cause (strategy code /
  data / config), then `doyoutrade-cli task clone <task_id>` + retry on the
  cloned task_id. Never retry on the original task.

### Recovery rules

1. **Never** retry the same task; the error is permanent for that task.
2. Inspect the existing run with `doyoutrade-cli debug get-run-view <run_id>`
   (using the first id from `error.existing_run_ids`) plus
   `doyoutrade-cli cycle list <task>` to confirm what already happened.
3. To re-run with the same configuration, call `doyoutrade-cli task clone
   <task_id>`; that creates a fresh `configured` task you can backtest
   immediately.

## `doyoutrade-cli backtest summary` payload

One-hop, fixed-schema view of a finished backtest run. The OK envelope
of `doyoutrade-cli backtest run` now also carries this inline under
`data.backtest_summary` when the persisted summary's `backtest_job_id`
matches the run that just completed — no second command needed in the
happy path.

### Minimal valid invocation

```bash
doyoutrade-cli backtest summary btjob-...
doyoutrade-cli backtest summary btjob-... --format json    # programmatic
```

Use the `run_id` from `data.backtest_job.run_id` (the same id
`doyoutrade-cli backtest run` returned). The summary payload includes:

- `starting_equity`, `ending_equity`, `return_pct`, `final_cash`,
  `final_market_value`
- `max_drawdown_pct` (with peak / trough equity + timestamps), `win_rate`,
  `fills_count`, `trade_count_closed` / `trade_count_open`,
  `avg_holding_trading_days`
- `equity_curve` (downsampled timeseries with `equity_curve_meta.raw_length`)
- `final_positions` (per-symbol qty / cost / last_price / market_value)
- `backtest_job_id` (cross-reference key) + legacy `run_id` (final
  cycle_run_id, kept for back-compat)

## `doyoutrade-cli backtest summary` error_code reference

- `backtest_summary_not_found` — no run row for the given `run_id`.
  Verify with `doyoutrade-cli cycle list <task>`, or use the `run_id` the
  `doyoutrade-cli backtest run` response carried back.
- `backtest_summary_not_ready` — the run row exists but no summary has
  been persisted yet. Either the run is still in flight, was aborted
  before finalize, or the summary compute step raised. Envelope carries
  the `run` header so you can inspect `status` / `error_message`; use
  `doyoutrade-cli debug get-run-view <run_id>` to confirm finalize happened.
- `backtest_summary_stale` — a newer backtest on the same task
  overwrote the persisted summary. Envelope carries
  `latest_summary_run_id` so you can recover with `doyoutrade-cli backtest
  summary <latest_summary_run_id>` or rerun via `doyoutrade-cli backtest
  run` for the original range.

## Reacting to `error_type`

`doyoutrade-cli backtest run` and the debug session view always carry
`error_type` plus a non-empty `error.message` (and a `traceback_tail`
on debug_sessions). When you see a structured error:

- Read `error_type` first — it tells you whether the problem is a Python
  exception (e.g. `RuntimeError`, `ValueError`) vs. a logical cycle failure
  (`CycleFailure`) vs. a wait timeout (`BacktestTimeout`).
- Inspect `doyoutrade-cli debug get-run-view <run_id>` for the same run; the
  session record now stores `error_type` / `traceback_tail` so
  debug_session events carry the same payload that the command returned.
- Only retry once you have a hypothesis that explains the error_type.
  Empty or missing `error` text is now a bug, not a normal state —
  surface it instead of retrying blindly.
