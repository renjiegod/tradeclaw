# Tool Error Codes — Quick Reference

Read this file when a strategy / backtest command (CLI or in-process)
has returned a structured error and you need to decide what to do next.

All commands return non-empty `error.message` plus an `error_type`
discriminator. Treat `error_type: "WrongIdentifierType"` as a wrong-field
signal; do not retry as-is. Empty `error` text is a bug — surface it
instead of guessing.

The same `error_code` tokens appear whether you reach the underlying
behaviour via `doyoutrade-cli` or the in-process tool — the CLI envelope
just normalizes the shape (see the main-agent system prompt's "CLI envelope 速读" section).

## Strategy / backtest

| `error_code` | Cause | Recovery |
|---|---|---|
| `wrong_identifier_type` | `--task` got an `sd-…`; or `--definition` got something that isn't `sd-…` | Fix the identifier; do not retry. Look up the right task via `doyoutrade-cli task list` / `task get`, or the definition via `doyoutrade-cli strategy definition list`. |
| `backtest_run_already_exists` | The platform reports an existing run for this task but could not classify its status | Inspect with `doyoutrade-cli debug get-run-view <existing_run_ids[0]>` / `doyoutrade-cli cycle list <task>`. If unrecoverable, `doyoutrade-cli task clone <task_id>` then run on the clone. **`cloned_task_id` is never auto-populated.** |
| `backtest_run_failed` | Existing run for this task ended in `failed` | Payload carries `existing_run_id` and the run's `error_message`. Inspect with `doyoutrade-cli debug get-run-view <run_id>`; address the underlying cause (strategy code, data provider, config) before retrying; only then `doyoutrade-cli task clone` and run on the clone. |
| `backtest_validation_error` | Input invalid (date range, universe, etc.) | Fix input and retry. |
| `backtest_start_failed` | Platform threw before the run was created | Inspect `traceback_tail`. Treat as infrastructure (data provider unreachable, missing worker dep). Do not retry blindly; surface to user. |
| `backtest_wait_timeout` | The wait timeout fired but the run is still running | Calling `doyoutrade-cli backtest run --task <task_id>` again resumes waiting on the same run. The envelope carries `backtest_job` with progress (`bars_completed` / `bars_total`) and `attached_to_existing_run: true` when already attached. You can also stream via `doyoutrade-cli backtest watch <run_id>` or one-shot `doyoutrade-cli debug get-run-view <run_id>`. **Never react by cloning.** |
| `missing_task_or_definition_id` / `conflicting_backtest_entry_mode` | `doyoutrade-cli backtest run` needs exactly one of `--task` or `--definition` | Pick one. Prefer `--definition` after authoring. |
| `missing_universe_for_auto_create_mode` | Definition auto-create requires non-empty `--universe` | Pass `--universe SYM1,SYM2`. |
| `auto_create_task_failed` | `platform_service.create_task` raised inside auto-create mode | Verify the `sd-…` exists and universe symbols are valid. |
| `unknown_arguments` | 顶层传了未声明的 key（包括把 `settings` 当顶层） | 按 `error.unknown` 列出来的字段名挪到正确位置即可。详见 `create-task-payload.md`。 |
| `invalid_strategy_json` / `invalid_agent_json` / `invalid_universe_json` | 对应字段送了 JSON 字符串或形状错误 | 按 `error.hint` 改成原生 object / array 再试。 |
| `missing_name` | `doyoutrade-cli task create --name` 缺失或空白 | 补上非空 `--name`。 |
| `missing_strategy_binding` | `doyoutrade-cli task create` 没传 `--definition`，`--params.strategy` 里也没有 `definition_id` | 补上 `--definition sd-… --params '{...}'`；参数走 `--params` → `parameter_overrides`。 |
| `invalid_params_json` | CLI 层的 `--params` 不是合法 JSON 对象 | 修一下 JSON；hint 里有最小有效样本。 |

## Auto-smoke gate inside `strategy definition` CLI commands

`doyoutrade-cli strategy definition create` and
`doyoutrade-cli strategy definition update` embed the same compile +
single-cycle smoke that `doyoutrade-cli sdk validate` runs, and they
run it **before** persisting. You do not need to call
`doyoutrade-cli sdk validate` between a rewrite and the registry write.
On smoke failure both commands return `stage: "smoke"` and
`persisted: false` — the registry snapshot is unchanged. The standalone
`doyoutrade-cli sdk validate` command stays available as an opt-in
zero-registry-side-effect dry-run for exploring drafts.

Smoke-specific codes are identical across all three entry points:

| `error_code` | Cause | Recovery |
|---|---|---|
| `runtime_smoke_failed` | The compiled class raised during `__init__` (stage=`__init__`) or `on_bar(df, ctx)` (stage=`on_bar[...]`). Most common: `AttributeError` on a hallucinated SDK helper (`self.get_position_qty`, `ctx.portfolio`, `ctx.cash`, `ctx.equity`, `ctx.ohlcv`). | `error_type` names the Python exception; `traceback_excerpt` carries the last frames. Remove the invented helper — `on_bar` is position-naive; return a `Signal` and let `PositionManager` size. Market data lives on the `df` DataFrame (lowercase OHLCV columns) passed to `on_bar` / `populate_indicators`, never on `ctx`. |
| `smoke_output_invalid` | `on_bar` returned a value that isn't a `Signal` (subtype `on_bar_returned_non_signal`). | Return a `Signal` via `Signal.buy(tag=...)` / `Signal.sell(tag=...)` / `Signal.hold()` / `Signal.target_exposure(...)` / `Signal.target_quantity(...)`. `tag` is mandatory on every actionable signal. |

The smoke gate is zero-side-effect — no DB writes, no debug session, no
`model_invocation` row. **Always re-run `doyoutrade-cli sdk validate` after
fixing a backtest `AttributeError` / `TypeError`** before calling
`doyoutrade-cli strategy definition update` — the smoke is the only layer
that catches hallucinated SDK methods without paying for another full
backtest.

## Reading `error_type`

- `WrongIdentifierType` — the identifier kind doesn't match the field's
  declared kind. Switch to the right command or correct the flag; do
  not retry as-is.
- A Python-style name (`RuntimeError`, `ValueError`, etc.) — actual
  exception from the runtime; inspect `traceback_tail`.
- `CycleFailure` — logical cycle failure (data missing, signal raised).
- `BacktestTimeout` — wait-side, not run-side; the job is still going.

Inspect `doyoutrade-cli debug get-run-view <run_id>` for the same run; the
session record stores `error_type` / `traceback_tail` so
`debug_session_events` carry the same payload the command returned.
Only retry once you have a hypothesis explaining the `error_type`.
