---
name: doyoutrade-strategy
description: Manage Doyoutrade strategy resources via `doyoutrade-cli strategy ...` — inspect strategy definitions (sd-…) from authored source files, update definition metadata, and bind / promote a definition into a trading task (with `parameter_overrides`). Use when the user asks "把策略绑到任务上 / promote to live / 把策略放到这个任务里 / 把这份代码注册成 strategy definition / 查看 sd- 源码". Companion to `doyoutrade-task` (the task lifecycle skill) and to `strategy-definition-authoring` for authoring the Python `source_code`. The cross-CLI envelope contract (shape, exit codes, env vars) is documented in the main-agent system prompt under "CLI envelope 速读" — no skill load needed for that.
category: tool
style: process
---

<!-- Routing:
- Writing or editing a strategy's source code → `strategy-definition-authoring`.
  Source versions are persisted only through `doyoutrade-cli strategy authoring
  open|compile|finalize` plus the in-process file tools documented there.
- Need to look up a stock symbol before referencing one in parameters →
  `doyoutrade-stock`.
- After binding/promoting, want to run a backtest → `doyoutrade-cli backtest run`.
-->

# doyoutrade-strategy

## When to use

Trigger this skill whenever the user asks the agent to:

- 查看 strategy definition / "show me sd-… source code"
- 查看 / 修改 strategy definition 元数据
- 把 definition 绑到 task / "bind this strategy to task X"
- promote to live / 上线 / "promote sd-… into live task Y with approval policy"

`strategy-definition-authoring` is the companion when you're *writing the
Python code itself* (Strategy subclass, indicators, etc.). Once a draft is
finalized, this skill covers definition inspection, metadata-only updates, and
binding / promoting a definition (plus `parameter_overrides`) into a task.

There is **no strategy-instance layer** — tasks bind a definition (`sd-…`)
directly and carry their own `parameter_overrides`. Per-task parameter
variants live on the task, not on a separate persisted resource.

## Contract First

Before using a command whose flags are not already in this skill, inspect the
machine-readable CLI contract:

```bash
doyoutrade-cli schema strategy.bind
doyoutrade-cli schema backtest.run
doyoutrade-cli schema strategy.authoring.open
```

Use `data.cli_contract.flags[].name` as the shell flag spelling and
`maps_to` / `semantic` / `accepts_prefix` to understand how that flag maps to
tool kwargs and resource IDs. Do not infer flag names from returned JSON field
names.

## Commands

### `doyoutrade-cli strategy definition get <sd-id>`

```bash
doyoutrade-cli strategy definition get sd-3f1c2a9b8e7d
```

`data` carries the full snapshot: `source_code`, `parameter_schema`,
`capabilities`, `code_hash`, `status`, and the `authoring_contract` the
agent must respect when editing. `recommended_next_steps` is a short
list of "what to do next" hints — feed it back to the user verbatim
when they ask "now what?".

### Strategy definition creation / source updates

Do not create or update strategy source through `strategy definition create` /
`strategy definition update`. Source authoring goes through:

```bash
doyoutrade-cli strategy authoring open --name "MACD Trend"
doyoutrade-cli strategy authoring compile --session-id <sess-...>
doyoutrade-cli strategy authoring finalize --session-id <sess-...>
```

Between `open` and `compile`, write files only with the in-process file tools
inside the returned `work_dir`. Load `strategy-definition-authoring` for the
full workflow and SDK rules.

### `doyoutrade-cli strategy definition update <sd-id>`

```bash
doyoutrade-cli strategy definition update sd-3f1c2a9b8e7d --name "MACD Trend v2"
doyoutrade-cli strategy definition update sd-3f1c2a9b8e7d --status deprecated
```

Patch semantics: only the supplied metadata flags are written. Source code and
current version are changed only by `strategy authoring finalize`.

### Binding a definition to a task

Tasks bind a definition (`sd-…`) directly and carry their own
`parameter_overrides` — there is no separate persisted instance resource.
Set the parameters with `--params` on `task create` / `backtest run`, or
patch `settings.strategy.parameter_overrides` later via `task update`:

```bash
doyoutrade-cli task create \
  --name "MR Demo" \
  --definition sd-3f1c2a9b8e7d \
  --params '{"window": 14}' \
  --universe 600519.SH

doyoutrade-cli backtest run \
  --definition sd-3f1c2a9b8e7d \
  --params '{"window": 14}' \
  --universe 600519.SH \
  --range-start 2024-01-01 --range-end 2024-06-01
```

To reuse the same definition with a different parameter set, create another
task (or clone an existing one) and set its `parameter_overrides` — the
variant lives on the task.

### `doyoutrade-cli strategy inspect [--query <keywords>]`

Surveys all strategy definitions and groups definitions
sharing the same source-code fingerprint under
`duplicate_definition_groups` — so the agent can reuse an existing
definition instead of creating another copy.

```bash
# List everything
doyoutrade-cli strategy inspect

# Fuzzy search (whitespace-separated tokens AND-matched)
doyoutrade-cli strategy inspect --query "mean reversion daily"
doyoutrade-cli strategy inspect --query "sd-3f1c"            # by id prefix
```

Matched rows surface a `match_reasons` field naming which fields hit.
Use this as the first step when the user says "find me an existing
strategy that does X" before falling back to
`doyoutrade-cli strategy definition create`.

### `doyoutrade-cli strategy bind <task_id> <sd-id>`

```bash
doyoutrade-cli strategy bind 550e8400-e29b-41d4-a716-446655440000 sd-3f1c2a9b8e7d
```

Writes `settings.strategy.definition_id` on the task. Reverts any custom
`approval_policy` / `risk_overrides` on the task only if explicitly
overridden — `bind` is the minimal one — for richer binding plus policy
patching, use `strategy promote` below.

### `doyoutrade-cli strategy promote <task_id> <sd-id>`

```bash
# minimal — same effect as bind, plus the "promoted-to-live" semantic
doyoutrade-cli strategy promote 550e8400-... sd-3f1c2a9b8e7d

# with approval policy and risk overrides
doyoutrade-cli strategy promote 550e8400-... sd-3f1c2a9b8e7d \
    --approval-policy '{"min_notional_for_approval": 100000, "timeout_seconds": 600}' \
    --risk-overrides '{"max_position_ratio": 0.05}'
```

`--approval-policy` / `--risk-overrides` are patch fields: omit to
preserve any existing values on the task. Supplying an empty object
`{}` is *not* a no-op — it writes an empty object. To remove a policy,
use `task update --params` directly.

## Reading tool errors

See the main-agent system prompt's "CLI envelope 速读" section for the general error envelope. Stable
`error_code` tokens specific to this skill:

| `error_code` | Exit | Where | Cause / repair |
| --- | --- | --- | --- |
| `wrong_identifier_type` | 2 | Any command with `task_id` / `definition_id` | `error.expected_kind` and `error.actual_kind` tell you which shape was needed. Use `task get` / `strategy definition get` to resolve. |
| `invalid_params_json` | 2 | `task create/update --params`, `backtest run --params` | The JSON failed to parse or wasn't an object. The hint shows a sample valid payload. |
| `invalid_params_schema_json` / `invalid_default_params_json` / `invalid_capabilities_json` / `invalid_provenance_json` | 2 | `definition create/update` JSON flags | The named flag's value failed to parse or wasn't an object. Send a JSON object literal. |
| `invalid_approval_policy_json` | 2 | `strategy promote --approval-policy` | The flag value must be a JSON object. |
| `invalid_risk_overrides_json` | 2 | `strategy promote --risk-overrides` | The flag value must be a JSON object. |
| `runtime_smoke_failed` / `smoke_output_invalid` | 1 | `strategy authoring compile/finalize` | Smoke gate caught a runtime fault in the authored code. Inspect `error.message` (e.g. `NameError: name 'pd' is not defined` → missing `import pandas as pd`); fix the draft and re-run compile. |
| `invalid_strategy_definition` (+ compiler-specific tokens) | 1 | `strategy authoring compile/finalize` | Strict authoring contract violation (missing `Signal.buy(tag=…)`, lookahead access, etc.). Payload carries `validation_errors` and `expected_contract.authoring_contract`. |
| `repository_unavailable` / `service_unavailable` | 1 | Any | Backend service was not wired during runtime construction. Usually a deployment issue; surface to the operator. |

## Combining with bash

```bash
# Resolve a definition id, then promote it onto a task with a custom policy
SD_ID=$(doyoutrade-cli strategy inspect --query "mean reversion" \
    | jq -r '.data.definitions[0].definition_id')
echo "$SD_ID"

TASK_ID=$(doyoutrade-cli task get "MR Demo" | jq -r '.data.task.task_id')
doyoutrade-cli strategy promote "$TASK_ID" "$SD_ID" \
    --approval-policy '{"min_notional_for_approval": 50000}'
```

## What this skill does *not* cover

- Authoring the Python `source_code` itself (Strategy subclass,
  `populate_indicators`, `on_bar`, indicator helpers) — that's
  `strategy-definition-authoring`. This skill picks up after the draft is
  on disk.
- Backtest execution / debug-view inspection — `doyoutrade-cli backtest
  run|watch|summary` and `doyoutrade-cli debug get-run-view` (covered by
  `doyoutrade-backtest` and `doyoutrade-debug`).
