---
name: strategy-iteration
description: Iterate on a Doyoutrade strategy after a backtest — decide whether the next change is parameter-only or definition logic, apply one focused change, and run the next backtest. Use this skill whenever the user says "回测完了下一步怎么改 / 这个策略还能怎么调 / parameter or logic / iterate the strategy / improve the backtest result", or whenever doyoutrade-cli backtest suggest-iteration has been called. Companion to `doyoutrade-debug` (`debug get-run-view`, when the run failed) and `strategy-definition-authoring` (when the change lands in code). Surface the backtest report from doyoutrade-cli backtest run / backtest summary before iterating.
category: strategy
style: process
---

<!-- style: process — iteration discipline. The parameter-before-code rule
and one-layer-at-a-time constraint are part of the contract. -->

# Strategy Iteration

## Purpose

Use this skill after a backtest or debug run when the goal is to improve a
strategy systematically. Random guessing and broad rewrites destroy the
signal-to-noise of the iteration loop; the parameter-first, one-layer-at-a-time
rules keep each change attributable to evidence.

## Evidence Sources

- `doyoutrade-cli backtest run` — primary entry point; on completion carries
  structured metrics in `data.summary`.
- `doyoutrade-cli backtest summary <run_id>` — re-fetches a prior run. Default
  is JSON; use `--format markdown` only for manual human report rendering.
- `doyoutrade-cli cycle get <run_id>` — single-cycle state.
- `doyoutrade-cli debug get-run-view <run_id>` — full debug payload (cycle_runs +
  spans + model_invocations).
- `doyoutrade-cli cycle list <task>` — paginate across a task's runs.
- `doyoutrade-cli backtest suggest-iteration <run_id>` — recommended next step.

Prefer `doyoutrade-cli backtest summary` over recomputing metrics manually —
never derive return / drawdown / win-rate from raw OHLCV.

## Iteration Discipline

- Read run evidence before proposing any change.
- Separate parameter tuning from logic rewrites — two changes in one iteration
  make results unattributable.
- Prefer the smallest change that explains the observed failure.
- Do not start another backtest for the same task while one is active — the
  command will attach to the existing run. Use `doyoutrade-cli backtest watch
  <run_id>` to block cleanly.

## Decision: Parameter vs Code

1. Run `doyoutrade-cli backtest suggest-iteration <run_id>`.
2. If response is `parameter_only`:
   - Update the task's parameter overrides:
     `doyoutrade-cli task update <task-id> --params '{"strategy": {"parameter_overrides": {...}}}'`
   - Or re-run the backtest directly with the new params:
     `doyoutrade-cli backtest run --definition <sd-...> --params '{...}' --universe <...> --range-start ... --range-end ...`
   - Parameters are cheaper to revert; try this before code changes.
3. If evidence points to SDK misuse or invalid runtime assumptions:
   - Open an authoring session (CLI), capture work_dir:
     ```bash
     OPEN=$(doyoutrade-cli strategy authoring open --definition-id <sd-...>)
     SESSION=$(echo "$OPEN" | jq -r .data.session_id)
     WORK_DIR=$(echo "$OPEN" | jq -r .data.work_dir)
     ```
   - Read the current files (in-process file tools — call directly):
     ```
     list_files(directory="$WORK_DIR")
     read_file(file_path="$WORK_DIR/strategy.py")
     ```
   - Make targeted edits (in-process file tools):
     ```
     edit_file(file_path="$WORK_DIR/strategy.py", old_string="...", new_string="...")
     ```
   - Compile — iterate until green (CLI):
     ```bash
     doyoutrade-cli strategy authoring compile --session-id "$SESSION"
     ```
   - Persist (CLI):
     ```bash
     doyoutrade-cli strategy authoring finalize --session-id "$SESSION"
     ```
   - For SDK contract, compile error_codes, indicators — load `strategy-definition-authoring`.

## Recommended Loop

1. Identify the exact failing or weak behavior from `signal_timeline_summary`.
2. Inspect the run and debug trace.
3. Decide: parameter update vs definition logic change.
4. Apply ONE focused change.
5. Run another backtest.
6. Compare the new run against the previous one.

## Compile Error Codes (iteration-relevant)

| error_code | Cause | Fix |
|---|---|---|
| `session_not_found` | `session_id` expired | Re-open with `doyoutrade-cli strategy authoring open --definition-id ...` |
| `old_string_not_found` | `edit_file` fragment not in current file | `read_file` first to get exact text |
| `old_string_not_unique` | Fragment matches multiple locations | Use longer context or `replace_all=true` |
| `io_error` | Disk IO failed while reading/writing the file (permissions, disk full, target is a directory, etc.) | Surface the error message back to the operator; do not retry blindly |
| `history_check_literal_disallowed` | `rolling(N)` literal exceeds `startup_history` | Raise `startup_history` or reduce window |
| `disallowed_import` | New import not in whitelist | Remove or route data via `ctx.dp.*` |
| `syntax_error` | Python syntax error | Fix the syntax |

Full compile error_code catalog in `strategy-definition-authoring/references/error-codes.md`.

## Failure Patterns

- **No trades**: `top_hold_tags` from `debug get-run-view` — warmup, condition too strict,
  or untagged hold hiding the real reason.
- **Overtrading**: threshold too loose or signal logic too sensitive.
- **Wrong signal shape**: SDK helper misused — check `return_type.fields` with
  `doyoutrade-cli sdk indicators`.
- **Repeated `no_data` / `format_error`**: wrong `ctx.ohlcv()` shape assumption.
- **Position logic wrong**: strategy treating `ctx.position(symbol)` as object
  instead of `Decimal`.

## Diagnosing a Zero-Trade Run

```bash
doyoutrade-cli debug get-run-view <run_id>
# data.debug_view.signal_timeline_summary  ← read this FIRST
#   {top_hold_tags, top_buy_tags, zero_trade, total_cycles}
```

Common patterns:

| `top_hold_tags` | Meaning | Action |
|---|---|---|
| `{"warmup": N}` | `startup_history` too high or bars too few | Adjust `startup_history`; DO NOT extend user's range |
| `{"no_cross": N}` | Indicators valid; condition never fired in window | Try different window/symbol; do not extend range |
| `{"<untagged_hold>": N}` | Strategy returns `Signal.hold()` with no tag | Add explicit tags to all hold branches |
| Mixed buy/sell tags but zero trades | Signals fired; downstream blocked | Look at PositionManager / approval gate spans |

## Priority Rules

- Prefer adjusting parameters (`doyoutrade-cli task update <task-id> --params
  '{"strategy": {"parameter_overrides": {...}}}'`, or `backtest run --definition
  <sd-...> --params '{...}'`) before opening an authoring session when
  `suggest-iteration` returns `parameter_only`. Parameters are cheaper to revert.
- Prefer definition edits when evidence clearly points to SDK misuse or
  wrong-shape access — parameter tweaks cannot paper over structural issues.
- Do not start a second `doyoutrade-cli backtest run` against a task that
  already has an active run.

## Self-Check Before Declaring Done

- [ ] Run evidence read before proposing any change
- [ ] `backtest suggest-iteration` consulted
- [ ] Only one layer changed (parameter OR code — not both)
- [ ] If code changed: files written via in-process `write_file`/`edit_file`; `doyoutrade-cli strategy authoring compile` green; `doyoutrade-cli strategy authoring finalize` called
- [ ] New backtest run started; report read via `read_file <data.report_path>`

## References

- [`references/backtest-tool-reference.md`](references/backtest-tool-reference.md) — full
  payload schema for `doyoutrade-cli backtest run`, one-shot-per-task rule,
  error_code recovery vocabulary. **Read this when drafting a `backtest run`
  payload or recovering from a run/start error_code.**
- `strategy-definition-authoring/references/sdk-surface.md` — Strategy base class,
  ctx.dp methods, field shapes.
