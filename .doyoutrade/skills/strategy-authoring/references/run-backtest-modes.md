# `doyoutrade-cli backtest run` Modes & Polling

Read this file when you're about to run `doyoutrade-cli backtest run`, need
to decide between task vs definition mode, or are facing a wait / timeout
question.

## Two entry shapes

Single command, two ways to enter:

- **Task mode** — `doyoutrade-cli backtest run --task <task_id> --range-start
  YYYY-MM-DD --range-end YYYY-MM-DD`. Runs the existing backtest task
  as-is (it already carries its strategy binding + universe). Use when
  iterating on the same task.
- **Definition mode** (default post-authoring) — `doyoutrade-cli backtest run
  --definition sd-… --params '{"window": 14}' --universe
  600519.SH,000001.SZ --range-start … --range-end …` (and optionally
  `--name` / `--data-provider`). The CLI **auto-creates a backtest
  task** bound to that definition (`mode=backtest`) with the supplied
  `--params` as `parameter_overrides`, runs it, and returns the result.
  The auto-created task id appears in the response as
  `data.auto_created_task_id`. Use when you just authored a definition
  and want a closed-loop result in one call.

The command **waits for terminal status by default** (`--timeout 120`).
Set `--timeout 0` to return immediately after the run is queued
(fire-and-forget). Pair fire-and-forget with `doyoutrade-cli backtest watch
<run_id>` instead of polling manually. Don't mix wait + watch in one
workflow — pick a mode and stick.

## Backtest tasks are one-shot

A backtest task can carry **only one** run. When `doyoutrade-cli backtest
run` encounters a task that already has a run, the command **inspects
the existing run and dispatches by its status**:

| Existing run status     | What the command does                                                                            | Envelope shape |
| ----------------------- | ------------------------------------------------------------------------------------------------ | -------------- |
| `running` / `queued`    | **Attaches** and polls until terminal or timeout (or returns immediately when `--timeout 0`)     | `ok: true` + `data.attached_to_existing_run: true` on success / timeout-while-attached → `ok: false`, `error.error_code: backtest_wait_timeout`, `error.attached_to_existing_run: true` |
| `completed` / `finished`| Returns the existing terminal result directly                                                    | `ok: true` + `data.attached_to_existing_run: true` |
| `failed`                | Returns a structured failure with the run's `error_message`                                      | `ok: false`, `error.error_code: backtest_run_failed`, `error.existing_run_id`, `error.existing_run_ids: [...]` |
| Cannot classify         | Falls back to legacy error                                                                       | `ok: false`, `error.error_code: backtest_run_already_exists`, `error.existing_run_ids` |

## Recovery rules

- Never react to these errors by retrying blindly. Inspect first via
  `doyoutrade-cli debug get-run-view <run_id>` or `doyoutrade-cli cycle get
  <run_id>` using the run id from `error.existing_run_id` /
  `error.existing_run_ids[0]`.
- If the existing run failed because of fixable code or config, address
  the cause, then `doyoutrade-cli task clone <task_id>` and run on the cloned
  task. **`cloned_task_id` is never automatically populated** — cloning
  is a deliberate, agent-initiated action.

## Polling discipline

`doyoutrade-cli backtest run` waits synchronously by default. Treat its
timeout as "I stopped waiting", never as "the job failed":

1. `error_code: backtest_wait_timeout` means the run is still executing
   in the background. Calling `doyoutrade-cli backtest run` again with the
   same `--task <task_id>` **resumes waiting on the same run** — it
   does not start a new one. You can also stream it via
   `doyoutrade-cli backtest watch <run_id>` or one-shot
   `doyoutrade-cli debug get-run-view <run_id>`.
2. Pick `--timeout` from a rough cost estimate: ~4–8 s per bar per
   symbol for a typical strategy. For 262 daily bars on a single symbol
   that is roughly 1500 s; pick at least that, or accept that the first
   call will time out and you'll need a second `backtest run` call
   (with the same `--task`) to resume.
3. The fresh-run path also emits resumable hints on timeout (without
   `attached_to_existing_run`). Same recovery: call again with the same
   `--task` to keep waiting, switch to `doyoutrade-cli backtest watch`, or
   inspect the run directly.

## Identifier hygiene

`task_id` is a uuid. `definition_id` looks like `sd-...`. There is no
strategy-instance layer — a task binds a definition directly via
`task.settings.strategy.definition_id` + `parameter_overrides`.
`doyoutrade-cli backtest run` declares the expected kind per flag —
`--task` rejects `sd-` shapes with `error_code: wrong_identifier_type`,
and `--definition` rejects anything that isn't `sd-...`. Treat that
error as a "wrong flag" signal, not as a missing record.
