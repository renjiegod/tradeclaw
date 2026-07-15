---
name: strategy-authoring
description: End-to-end Doyoutrade strategy workflow — brainstorm → open authoring session → write entry + helpers → compile iteration → finalize → bind definition to task → backtest → review. Load this when the user wants to author a new strategy from scratch, get a lifecycle overview, or needs guidance on the full create-to-backtest flow. Do NOT load for resource tasks ("run the existing X strategy", "backtest using current settings", "show me strategies on this stock", "list strategies") — those are pure CLI flows covered by the main-agent system prompt's "资源任务速查表" + "资源任务起手式" sections. Routes to strategy-definition-authoring for code-shape details, strategy-iteration for post-backtest decisions, and `doyoutrade-debug` (`debug get-run-view`) for failures.
category: strategy
style: process
---

<!-- style: process — high-level workflow.

This skill drives strategy creation through the authoring surface:
- Lifecycle (open / cancel / compile / finalize) → CLI via execute_bash.
- File ops (read_file / write_file / edit_file / list_files) → in-process
  agent tools called DIRECTLY — no CLI subcommand, no execute_bash.
- read_file is unrestricted (can read ANY absolute path, not just work_dir).
- write_file / edit_file are sandbox-enforced (path must be inside work_dir).

The CLI envelope and exit-code contract are documented in the main-agent
system prompt's "CLI envelope 速读" section.

Routing:
- Do NOT load for resource tasks (run / read existing strategies, definitions,
  runs). The main-agent "资源任务速查表" + "资源任务起手式" sections cover
  doyoutrade-cli strategy inspect, backtest run, and the read_file(data.report_path)
  contract. Loading this skill for those flows wastes context.
- Strategy code shape, 6-layer structure, indicators reference, compile
  error_code repair → strategy-definition-authoring.
- Post-backtest decisions → strategy-iteration.
- Diagnosing a failing run → `doyoutrade-debug` (`debug get-run-view` / `cycle get`).
-->

# Strategy Authoring

## Purpose

Use this skill when the user wants to author a new strategy from scratch or
needs a full lifecycle overview (definition → bind definition to task →
backtest → iteration).

For the code-shape details (6-layer class structure, indicators, compile
error_codes), prefer `strategy-definition-authoring` — this skill covers
the lifecycle; that skill covers the SDK contract.

## Authoring Lifecycle (happy path)

Lifecycle commands (open / cancel / compile / finalize) are **shell commands**
invoked via `execute_bash`. File operations use **in-process tools called
directly** — no CLI subcommand, no `execute_bash`.

```bash
# 1. Open a new authoring session (CLI)
OPEN=$(doyoutrade-cli strategy authoring open --name "MyStrategy" --json 2>/dev/null || \
       doyoutrade-cli strategy authoring open --name "MyStrategy")
SESSION=$(echo "$OPEN" | jq -r .data.session_id)
WORK_DIR=$(echo "$OPEN" | jq -r .data.work_dir)
#    → {definition_id, session_id, work_dir, base_version, status:"created"}

# 2. See what's in the session (in-process file tools — call directly)
list_files(directory="$WORK_DIR")
read_file(file_path="$WORK_DIR/strategy.py")

# 3a. Write the entry file (in-process file tool)
write_file(file_path="$WORK_DIR/strategy.py", content="class Strategy(BaseStrategy): ...")
# 3b. Targeted edit (in-process file tool)
edit_file(file_path="$WORK_DIR/strategy.py", old_string="old_code", new_string="new_code")

# 4. Compile (AST + smoke, no persistence) — CLI
doyoutrade-cli strategy authoring compile --session-id "$SESSION"
# → if errors: fix via write_file/edit_file, repeat step 4

# 5. Finalize: promotes to versions/v{N+1}-{hash}/ — CLI
doyoutrade-cli strategy authoring finalize --session-id "$SESSION"
#    → {definition_id (sd-...), version_label, status:"ok"}

# 6. Backtest directly from definition — CLI (tasks bind the definition directly)
doyoutrade-cli backtest run --definition <sd-...> \
  --params '{"window": 14}' \
  --universe 600519.SH \
  --range-start 2024-01-01 --range-end 2024-06-01

# Optional: persist a reusable task with this definition + parameter set
# doyoutrade-cli task create --name <...> --definition <sd-...> --params '{...}' --universe <...>

# 7. Read the report (mandatory — in-process tool)
read_file(file_path=<data.report_path>)    # never summarize from CLI envelope text
```

To discard a session without persisting:
```bash
doyoutrade-cli strategy authoring cancel --session-id "$SESSION"
```

## Entry File Convention

The session's entry file is `strategy.py`. It must define a class named
`Strategy` that subclasses `doyoutrade.strategy_sdk.Strategy`.

To avoid the name collision, use the aliased import pattern:

```python
from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

class Strategy(BaseStrategy):
    startup_history = 30   # NOT required_history

    def on_bar(self, df, ctx):
        return Signal.hold(tag="idle")
```

Class attribute is `startup_history` (integer, bars). `on_bar(self, df, ctx)` must
return a real `Signal` object — `Signal.hold()` for no-op, `Signal.buy(tag="...")` /
`Signal.sell(tag="...")` for entries/exits, or
`Signal.target_exposure(target=0.5, tag="grid_l2")` for explicit
post-cycle exposure targets, or `Signal.target_quantity(quantity=300,
tag="grid_l3")` for strict share-inventory targets.

## Helper Files

Place helpers next to `strategy.py` in the session workspace — e.g.
`helpers.py`, `indicators/ma.py`. Import them with relative imports from
within `strategy.py`:

```python
from .helpers import compute_rsi_signal
```

All `.py` files in the workspace are walked by the AST compiler. Helpers
cannot contain `disallowed_import` violations or `history_check_literal`
violations — they are subject to the same whitelist as `strategy.py`.

Allowed imports (enforced by compiler):
- `decimal`, `math`, `numpy`, `pandas`
- `doyoutrade.strategy_sdk` (and submodules)
- stdlib modules: `datetime`, `typing`, `collections`, `itertools`, `functools`

## Compile Error Codes — Most Common

| error_code | Cause | Fix |
|---|---|---|
| `entry_file_missing` | `strategy.py` absent from session | Create it with `write_file` |
| `missing_required_class` | No class named `Strategy` in `strategy.py` | Add `class Strategy(BaseStrategy):` |
| `not_a_class_definition` | `Strategy` exists but is not a `class` statement | Rename or fix the definition |
| `invalid_base_class` | `Strategy` doesn't subclass `doyoutrade.strategy_sdk.Strategy` | Use `from doyoutrade.strategy_sdk import Strategy as BaseStrategy` |
| `disallowed_import` | `import requests` / `import akshare` / network libs | Remove; all data via `ctx.dp.*` |
| `history_check_literal_disallowed` | `rolling(N)` literal with N > `startup_history` | Increase `startup_history` or reduce window |
| `lookahead_access` | `df.iloc[i]` with i >= 0, or `df.shift(-n)` | Use `iloc[-1]`, `iloc[-2]` |
| `silent_exception_swallow` | `except Exception: pass` / silent `continue` | Narrow exception + log |
| `syntax_error` | Python syntax error in any `.py` file | Fix syntax |
| `compile_runtime_error` | Exception during smoke-run | Check the traceback in `message` |

Full catalog in `strategy-definition-authoring/references/error-codes.md`.

## File Tool Error Codes

| error_code | Meaning | Fix |
|---|---|---|
| `session_not_found` | `session_id` invalid or expired | Re-open with `doyoutrade-cli strategy authoring open` |
| `session_disappeared` | The draft was cancelled by another session between `compile` and `finalize` | Call `doyoutrade-cli strategy authoring open` again and replay your edits |
| `definition_not_found` | `definition_id` does not exist | Verify the id with `doyoutrade-cli strategy definition list` (or `strategy inspect`) |
| `name_required_for_new_definition` | `strategy authoring open` called without `--definition-id` AND without `--name` | Provide `--name "..."` for new definitions |
| `strategy_no_current_version` | Strategy has metadata but no finalized version; cycles cannot run yet | Complete the authoring lifecycle (open → write → finalize) before starting a cycle |
| `strategy_version_not_pinned` | Internal: signal generation invoked without prior pin | Report as a bug (this should only happen in test fixtures) |
| `path_outside_workspace` | `write_file` / `edit_file` `file_path` escapes `work_dir` (read_file has no sandbox) | Use relative paths only for write/edit |
| `file_not_found` | `read_file` on absent file | Check `list_files` first |
| `io_error` | Disk IO failed while reading/writing the file (permissions, disk full, target is a directory, etc.) | Surface the error message back to the operator; do not retry blindly |
| `old_string_not_found` | `edit_file` — `old_string` not in file | Read the file, pick a unique fragment |
| `old_string_not_unique` | `edit_file` — `old_string` matches multiple locations | Use a longer fragment or set `replace_all=true` |
| `no_op_edit` | `old_string == new_string` | Change differs from original |

## Authoring Workflow (step by step)

1. Clarify goal, market, bar frequency, holding style, and constraints.
2. Inspect existing strategy resources before creating duplicates:
   `doyoutrade-cli strategy inspect [--query <keywords>]`.
   - `duplicate_definition_groups` + `recommended_reuse_id` signal reuse opportunities.
3. Load `strategy-definition-authoring` so you have the SDK surface,
   indicators, and 6-layer class structure in context.
4. Open a session:
   ```bash
   SESSION=$(doyoutrade-cli strategy authoring open --name "<display name>" | jq -r .data.session_id)
   ```
5. Inspect the workspace (in-process file tools):
   ```
   list_files(directory="$WORK_DIR")
   read_file(file_path="$WORK_DIR/strategy.py")
   ```
6. Draft the strategy in `strategy.py` via `write_file`. Add helpers in
   separate files if needed (all paths must be inside `WORK_DIR`):
   ```
   write_file(file_path="$WORK_DIR/strategy.py", content="...")
   ```
7. Compile — iterate until green (CLI):
   ```bash
   doyoutrade-cli strategy authoring compile --session-id "$SESSION"
   ```
8. Finalize — promotes draft, returns `definition_id` (CLI):
   ```bash
   doyoutrade-cli strategy authoring finalize --session-id "$SESSION"
   ```
9. Update definition metadata if needed:
   `doyoutrade-cli strategy definition update <sd-...> --name "..." [--status active]`
   (metadata only — source code only changes via the authoring lifecycle).
10. Backtest directly from the definition:
    `doyoutrade-cli backtest run --definition <sd-...> --params '{...}' --universe <...> --range-start ... --range-end ...`
11. (Optional) Persist a task that binds the definition + parameter set:
    `doyoutrade-cli task create --name "..." --definition <sd-...> --params '{...}' --universe <...>`
    then `doyoutrade-cli strategy bind <task_id> <sd-...>` / `strategy promote` for live.
12. Read report: `read_file <data.report_path>` — mandatory.

## Parameters (universe-agnostic)

A definition's parameters are universe-agnostic. The symbol(s) come from
`doyoutrade-cli backtest run --universe ...` (or the task's `--universe`),
not from the definition or its `parameter_overrides`.

Do NOT bake a symbol or stock name into a definition name or a task name:

```text
✗  MACD中天科技                ← tied to one stock
✗  macd-600522-april           ← bakes symbol and window
✓  macd_12_26_9_default       ← params only
✓  macd_fast8_slow22_signal9  ← descriptive of the parameter set
```

## Reusing Existing Definitions

`doyoutrade-cli strategy inspect` returns `duplicate_definition_groups` and
`recommended_reuse_id`. Prefer the definition whose `definition_id` matches its
`recommended_reuse_id`, and reuse it (binding it to a fresh task with the
parameter set you need) before calling `doyoutrade-cli strategy authoring open` again.

## Diagnosing a Zero-Trade Run

When a backtest finishes with `trade_count_closed == 0 AND trade_count_open == 0`,
read the debug view before changing code:

```bash
doyoutrade-cli debug get-run-view <run_id>
# data.debug_view.signal_timeline_summary  ← read this FIRST
#   {total_cycles, top_hold_tags, top_buy_tags, top_sell_tags, zero_trade}
# data.debug_view.signal_timeline[*]       ← per-cycle detail
```

Common patterns in `top_hold_tags`:

- `{"warmup": N}` — `startup_history` too high or bars too few; check the value.
- `{"no_cross": N}` — indicators valid but entry condition never fired; don't extend range.
- `{"<untagged_hold>": N}` — strategy returns `Signal.hold()` with no tag; add explicit tags.
- Mixed `golden_cross` / `dead_cross_no_pos` in buy/sell tags — signals fired;
  look at `PositionManager` / approval gate spans.

## Backtest Review

After each backtest:
1. Check summary metrics and latest cycle runs.
2. Read debug view for runtime output.
3. Decide: parameter-only vs definition logic change. One layer at a time.

Load `strategy-iteration` for the next-step decision framework.

## Forbidden Shortcuts

- Do not use `generate_strategy_definition`, `create_strategy_definition_from_source`,
  or any `--source-file` / `--class-name` CLI flags — those were removed.
- Do not use `cat`, `echo`, `tee`, or any other shell command to write files
  directly into `work_dir`. Always use the in-process `write_file` /
  `edit_file` tools — they enforce sandbox rules via the registry.
- Do not invoke lifecycle tools (open / cancel / compile / finalize) as
  in-process tools — they are CLI-only:
  `doyoutrade-cli strategy authoring open|cancel|compile|finalize`.
- Do not invoke `doyoutrade-cli strategy authoring read|write|edit|list` — those
  subcommands were removed. Use the in-process file tools directly instead.
- Do not write to `settings.generated_*` or `settings.factor_*`.
- Do not branch on `ctx.mode` (backtest vs paper vs live) — one path only.
- Do not start a second backtest while one is active for the same task.

## Self-Check Before Declaring Done

- [ ] `strategy inspect` checked for duplicates before creating a new definition
- [ ] `doyoutrade-cli strategy authoring open` called; `session_id` and `work_dir` captured
- [ ] Files written via in-process `write_file` / `edit_file` (not CLI subcommands)
- [ ] `doyoutrade-cli strategy authoring compile` returned no errors
- [ ] `doyoutrade-cli strategy authoring finalize` called; `definition_id` (sd-...) captured
- [ ] Backtest run started from the definition (`backtest run --definition sd-... --params ... --universe ...`)
- [ ] Backtest report read via `read_file(file_path=<data.report_path>)` — not synthesized

## References

Deep-dive files in the sibling skill's `references/` folder. Load the file that
matches the gap rather than reading everything up front.

- `strategy-definition-authoring/references/sdk-surface.md` — Strategy base class,
  ctx.dp methods, DataRequest factories.
- `strategy-definition-authoring/references/error-codes.md` — full error_code
  vocabulary with repair recipes.
- `strategy-definition-authoring/references/indicators.md` — doyoutrade.strategy_sdk.indicators
  reference.
- `strategy-authoring/references/create-task-payload.md` — exact field shape for
  doyoutrade-cli task create / task update.
- `strategy-authoring/references/run-backtest-modes.md` — task vs definition entry
  shapes, one-shot-per-task rule, polling discipline.
