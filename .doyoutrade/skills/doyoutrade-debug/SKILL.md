---
name: doyoutrade-debug
description: Inspect Doyoutrade runtime observability via `doyoutrade-cli cycle ... / debug ... / route ...` — list / get cycle runs, fetch the full debug view (cycle_runs + spans + model_invocations) by run_id OR by OpenTelemetry trace_id, list model invocations (LLM request/response/tokens/latency) filtered by trace_id / run_id / span_id, and list available model routes. Use when the user asks "看一下这个 run 跑了啥 / why did cycle X fail / show me the debug view / 按 trace_id 查链路 / 看模型调用日志 / 哪些 model route 可用 / list routes". Companion to `doyoutrade-backtest` (streaming watch) and `doyoutrade-task` (lifecycle).
category: tool
style: process
---

<!-- Routing:
- Stream a live backtest's progress → `doyoutrade-backtest` (watch).
- One-shot snapshot of a finished backtest → `backtest summary` (in the
  `doyoutrade-backtest` skill).
- Suggest the next iteration step after inspecting a failed run →
  `doyoutrade-cli backtest suggest-iteration`.
-->

# doyoutrade-debug

## When to use

Trigger this skill whenever the user wants to **read** runtime
artefacts produced by the platform — cycle runs, debug spans, model
invocations, or available routing presets.

Typical utterances:

- "为什么 run-xxx 失败了 / why did this cycle fail"
- "看一下 cycle 历史 / show me the cycle runs"
- "把这个 run 的 trace 拉出来 / dump the debug view"
- "我只有一个 trace_id，按 trace 查链路 / look up by trace_id"
- "看这个 run / trace 的模型调用日志 / show the LLM invocations"
- "有哪些 model route / list model routes"

**Picking the entry by the id you hold:**

| You have… | Command |
| --- | --- |
| `run_id` (cycle / `btjob-` / debug session) | `debug get-run-view <run_id>` |
| OTel `trace_id` (32-hex) | `debug get-trace-view <trace_id>` |
| `trace_id` and want only LLM calls | `debug model-invocations --trace-id <trace_id>` |
| `run_id` and want only LLM calls | `debug model-invocations --run-id <run_id>` |
| `session_id` (asst-/debug) | → `doyoutrade-assistant` (`assistant session get/events/export`) |
| `trace_id` came from a cron fire | → `doyoutrade-cron` (`cron runs by-trace <trace_id>`) |

## Commands

### `doyoutrade-cli cycle list <task_identifier> [filters]`

```bash
doyoutrade-cli cycle list "MR Demo"                              # by name
doyoutrade-cli cycle list 550e8400-...                           # by task_id
doyoutrade-cli cycle list 550e8400-... --status completed
doyoutrade-cli cycle list 550e8400-... --run-kind manual --limit 100
doyoutrade-cli cycle list 550e8400-... --started-after 2026-05-01T00:00:00
```

| Flag | Notes |
| --- | --- |
| `<task_identifier>` | Task `task_id` (UUID) or exact name — same resolver as `task get`. |
| `--limit / --offset` | Pagination. |
| `--status` | `running` / `completed` / `failed` / `cancelled` / ... |
| `--run-kind` | `scheduled` / `manual` / `debug`. |
| `--run-mode` | `paper` / `live` / `backtest`. |
| `--run-id-contains` | Substring match. |
| `--started-after / --started-before` | ISO datetime bounds. |
| `--run-id` | Filter cycle runs to those belonging to a specific backtest job's session. |

### `doyoutrade-cli cycle get <run_id>`

```bash
doyoutrade-cli cycle get run-9b8e2c1a3f5d
```

`data.cycle_run` carries proposals, decisions, fills, phase completion
markers — the full per-cycle state the worker exported.

### `doyoutrade-cli debug get-run-view <run_id> [trimming flags]`

```bash
# Agent-default first look: compact, cheap, preserves signal_timeline_summary
doyoutrade-cli debug get-run-view btjob-2c4e6a1b8f3d \
  --summary-only --no-spans --cycle-runs-limit 5

# Minimal payload — drop spans + model_invocations, just cycle structure
doyoutrade-cli debug get-run-view run-9b8e2c1a3f5d --no-spans --no-model-invocations

# Truncate cycle_runs to the first 20
doyoutrade-cli debug get-run-view btjob-2c4e6a1b8f3d --cycle-runs-limit 20
```

`<run_id>` accepts **three id shapes**: a cycle run id, a backtest job
id (`btjob-...`), or a debug session id (`backtest-...` / `debug-...`).
The platform resolves all three.

| Flag | Default | Notes |
| --- | --- | --- |
| `--summary-only` | off | Add a compact summary block; still returns the trimmed payload. |
| `--no-spans` | off | Drop the OTel spans array. |
| `--no-model-invocations` | off | Drop the model_invocations array. |
| `--cycle-runs-limit N` | unset | Truncate `cycle_runs` to N entries. |

`data.debug_view.signal_timeline_summary` is placed first and is the
recommended starting point. Full `spans` can be very large because data
provider events may include raw bars; only fetch them when the compact view
does not answer the question. Other keys: `cycle_run` (the focal run),
`cycle_runs` (all runs in the session, optionally truncated), `spans`,
`model_invocations`, optional `summary`.

**Fast-mode backtests have no trace.** A backtest started with `--no-debug`
(see `doyoutrade-backtest`) persists no debug session / spans / cycle runs /
model invocations. For such a run, `get-run-view` returns `debug_enabled:
false`, a `debug_unavailable_reason` of `debug_disabled`, an explanatory
`note`, and empty `spans` / `cycle_runs` / `model_invocations`. This is the
expected shape — do not treat it as a lookup failure or report it as broken.
The run status and report are still available via `doyoutrade-cli backtest
summary <btjob-...>`. To get a full trace, re-run the backtest with `--debug`.

### `doyoutrade-cli debug get-trace-view <trace_id>`

```bash
doyoutrade-cli debug get-trace-view 184314d705cb687948c603ec508f003c
```

Enters from the **OpenTelemetry trace_id** itself (32-char lowercase hex —
the first half of a `traceparent` header) instead of a run/session id. Use
this when a log line or span hands you a trace_id but no run_id. Returns the
same payload shape as `get-run-view` with
`resolved_from.identifier_type == "trace"`: all spans, cycle runs, and model
invocations carrying that trace, aggregated. An ill-formed trace_id (not
32-hex) → `trace_not_found`; a well-formed but never-recorded trace → also
`trace_not_found` (message distinguishes "invalid" vs "no records").

### `doyoutrade-cli debug model-invocations [--trace-id | --run-id | --span-id] [--limit / --offset]`

```bash
# Every LLM call in one trace (request, response, tokens, latency, ok/error)
doyoutrade-cli debug model-invocations --trace-id 184314d705cb687948c603ec508f003c

# All model calls a specific run made
doyoutrade-cli debug model-invocations --run-id run-9b8e2c1a3f5d

# Pinpoint the call on one span
doyoutrade-cli debug model-invocations --span-id 7f3a...

# Most recent calls across everything (no filter)
doyoutrade-cli debug model-invocations --limit 20
```

| Flag | Notes |
| --- | --- |
| `--trace-id` | Exact OTel trace_id (32-hex). |
| `--run-id` | Exact run_id (cycle_run / backtest job). |
| `--span-id` | Exact span_id. |
| `--limit / --offset` | Pagination (limit 1–500, default 20). |

Filters combine (AND). Each `data.items[*]` carries `trace_id` / `span_id` /
`run_id` so you can pivot back to `get-trace-view` / `get-run-view` for the
surrounding spans. This is the direct way to read the LLM `request` /
`response` payloads and token/latency without first pulling a whole debug view.

### `doyoutrade-cli route list`

```bash
doyoutrade-cli route list
```

`data.items[*].route_name` is the value to pass under
`settings.model_route_name` in `task create`. The list is small — use
`--format pretty` for a quick eyeball:

```bash
doyoutrade --format pretty route list
```

## Reading tool errors

See the main-agent system prompt's "CLI envelope 速读" section for the general envelope. Stable
`error_code` tokens specific to this skill:

| `error_code` | Exit | Where | Cause |
| --- | --- | --- | --- |
| `wrong_identifier_type` | 2 | `cycle list <identifier>` | Looks like sd-; pass a task id or exact name. |
| `task_not_found` | 3 | `cycle list` | No task matched. |
| `get_cycle_run_failed` | 1 | `cycle get` | Upstream raised; see message. |
| `service_unavailable` | 1 | `debug get-run-view` | Platform service not wired. |
| `run_not_found` | 3 | `debug get-run-view` | No cycle run / backtest job / debug session matched the id. |
| `trace_not_found` | 3 | `debug get-trace-view` | trace_id is not 32-hex, or no spans/cycle_runs/invocations carry it. |
| `model_route_repository_unavailable` | 1 | `route list` | Repo not configured. |
| `list_model_routes_failed` | 1 | `route list` | Upstream raised. |

## Combining with bash

```bash
# Pull the last failed cycle's id from a task, then dump its trace
TASK_ID=$(doyoutrade-cli task get "MR Demo" | jq -r '.data.task.task_id')
LAST_FAIL=$(doyoutrade-cli cycle list "$TASK_ID" --status failed --limit 1 \
              | jq -r '.data.items[0].run_id')
doyoutrade-cli debug get-run-view "$LAST_FAIL" --summary-only \
  | jq '.data.debug_view.summary'

# Quick "what model invocations did this run trigger?"
doyoutrade-cli debug get-run-view "$RUN_ID" --no-spans \
  | jq '.data.debug_view.model_invocations[] | {model, tokens, kind}'

# Choose a model route by name
ROUTE=$(doyoutrade-cli route list | jq -r '.data.items[] | select(.target_model | contains("opus")) | .route_name' | head -1)
doyoutrade-cli task update "$TASK_ID" --params "{\"settings\": {\"model_route_name\": \"$ROUTE\"}}"
```

## What this skill does *not* cover

- Live streaming of an in-flight run → `doyoutrade-backtest watch`.
- Suggesting the next iteration step from a failed run → CLI
  `doyoutrade-cli backtest suggest-iteration` (in `doyoutrade-backtest`).
- Editing the model route — no CLI command yet; lives in the API
  server's REST surface (`/model-routes`).
