---
name: doyoutrade-task
description: Manage Doyoutrade trading tasks via `doyoutrade-cli task ...` — fetch / list / create / update / start / pause / stop / delete / clone tasks. Use when the user asks "查看任务 / show me task X / list my tasks / 哪些任务在跑 / 新建任务 / create a task / 改一下任务的 universe / 启动任务 / 暂停任务 / 停止任务 / 把任务删掉 / 克隆这个回测". Companion to `doyoutrade-strategy` (strategy binding lives there) and `doyoutrade-stock` (symbol lookup before naming a universe). The cross-CLI envelope contract (shape, exit codes, `_notice`) is documented in the main-agent system prompt under "CLI envelope 速读" — no skill load needed for that.
category: tool
style: process
---

<!-- Routing:
- Bind / promote a strategy definition to this task →
  `doyoutrade-strategy` (`strategy bind` / `strategy promote`).
- Resolve a stock symbol before referencing it in `--universe` →
  `doyoutrade-stock`.
- Run / watch a backtest on this task → `doyoutrade-cli backtest run`
  + `doyoutrade-cli backtest watch <run_id>` (see `doyoutrade-backtest`).
- Edit strategy source_code → `strategy-definition-authoring`.
-->

# doyoutrade-task

## When to use

Trigger this skill whenever the user (or another skill) needs to
*identify*, *inspect*, or *mutate* a trading task. Both read and write
ops are now CLI-served.

Typical user utterances:

- "今天有哪些任务 / which tasks are running today"
- "看一下 task `<uuid>`" / "show me task `<uuid>`"
- "新建一个 paper task 跑 sd-…" / "create a paper task bound to sd-…"
- "把这个任务的 universe 改成 600519.SH" / "swap the universe to …"
- "启动 / 暂停 / 停止这个 live 任务"
- "克隆这个回测任务 / clone this backtest task to re-run"
- "删掉这个任务 / delete the task"

If the user names a task instead of giving a UUID, `task get` / `task
update` / `task delete` / `task clone` all accept the exact name and
resolve it for you. Multiple matches return `ambiguous_task_name` with
candidates.

Lifecycle commands (`task start|pause|stop`) intentionally require a task id
and do not resolve exact names. Use `task list --q ...` first, then call the
lifecycle command with the returned `task_id`.

## Read commands

### `doyoutrade-cli task get <identifier>`

```bash
doyoutrade-cli task get 550e8400-e29b-41d4-a716-446655440000
doyoutrade-cli task get "Daily Mean Reversion"   # exact name match also accepted
```

`data.task` carries the full snapshot (`task_id`, `name`, `status`,
`mode`, `config`, `created_at`, `updated_at`). When the argument was a
name, `data.resolved_from_name` echoes it.

### `doyoutrade-cli task list [filters] [pagination]`

```bash
doyoutrade-cli task list                                   # default page (20 items)
doyoutrade-cli task list --status running
doyoutrade-cli task list --definition sd-3f1c2a9b8e7d       # bound strategy filter
doyoutrade-cli task list --q "mean reversion" --limit 5
doyoutrade-cli task list --offset 20 --limit 20            # next page
```

`data.items` is the list, `data.total` is the unpaginated count, and
`data.limit` / `data.offset` echo the applied values. Walk pages by
re-running with `--offset` += `--limit` until `offset + len(items) >=
total`.

## Write commands

### `doyoutrade-cli task create`

```bash
# Minimal valid payload — bind a definition directly
doyoutrade-cli task create \
    --name "MR Demo" \
    --definition sd-3f1c2a9b8e7d \
    --params '{"window": 14, "z_threshold": 1.5}' \
    --universe 600519.SH

# Paper task across a multi-symbol universe
doyoutrade-cli task create \
    --name "MR Demo" \
    --definition sd-3f1c2a9b8e7d \
    --mode paper \
    --universe 600519.SH,000001.SZ

# Explicit data-cache / backfill / continuity policy (optional `data_cache` block).
# Omit entirely to use defaults: local-first read, auto-backfill on miss,
# calendar continuity, and fail-on-unverifiable-gap.
doyoutrade-cli task create \
    --name "MR Demo" \
    --definition sd-3f1c2a9b8e7d \
    --params '{"data_cache": {"source_priority": ["baostock", "qmt"], "continuity": {"on_unverifiable_gap": "fail"}}}'
```

| Flag | Required | Notes |
| --- | --- | --- |
| `--name` | yes | Human-readable task name. |
| `--definition` | yes | Strategy definition (`sd-...`) the task binds. |
| `--params` | no | A flat JSON object like `'{"window":14}'` becomes `strategy.parameter_overrides`. Also accepts nested `agent` / full `strategy` blocks. |
| `--mode` | no | `paper` / `backtest` / `live` / `signal_only`. Default `paper`. `signal_only` runs the cycle through `generate_signals` and stops — no review / dispatch / execution; `cycle_runs.run_mode='signal_only'`, `details.position_intents` populated, `details.fills=[]`, `submitted_count=0`. Use for monitoring-only tasks paired with `task_kind=strategy_signal_alert` cron jobs. |
| `--tick-mode` | no | `interval` / `cron_driven`. Use `cron_driven` for `strategy_signal_alert` monitoring tasks so the runtime loop does not double-fire alongside cron. |
| `--description` | no | Free-text description. |
| `--data-provider` | no | `auto` / `qmt` / `mock` / `akshare`. Default `auto`. |
| `--account` | no | Account id (`acct-...`) this task runs against. Omit to use the **default** account. The account record carries `live` / `mock` mode and the QMT / mock portfolio connection — there is no per-task `account_mode` flag. Create a mock account with `doyoutrade-cli account create --mode mock` before binding. |
| `--universe` | no | Comma-separated symbols. Resolve with `doyoutrade-cli stock lookup` first — never invent suffixes. Also accepts watchlist-tag tokens: `@watchlist:<tag>` (or `@watchlist:*` for every watchlist symbol), expanded to concrete symbols at build time (see "Watchlist universe references" below). |
| `--params` | no | Native JSON object. Use for `agent`, full `strategy` block (`parameter_overrides`, `execution_profile`), `strategy_preferences`, and the optional `data_cache` block. Explicit flags win over `--params` keys. |

`data_cache` (optional) controls the local-DB-first read, the upstream
gap-backfill source order, and the **write-time continuity guarantee** — any
backfill that would persist a discontinuous series (a missing trading day that
is not a suspension/holiday) fails the whole write rather than landing dirty
data. Fields: `source_priority` (array of `qmt`/`baostock`/`akshare`/`tushare`/`mock`),
`local_first` (bool), `auto_backfill` (bool), `continuity.on_unverifiable_gap`
(`fail`/`degrade`). A malformed value is rejected at create/update time with
`validation_error` (e.g. an unknown provider id). The cache interval always
follows the bar interval the task actually requests, and continuity is always
judged against the served provider's authoritative calendar — neither is
task-configurable. Only `qmt`/`baostock` provide an authoritative calendar;
with a non-authoritative served source the continuity check degrades to an
internal-gap-only check and emits `continuity_degraded`.

The underlying tool has `additionalProperties: false`. Unknown top-level
keys land in `--params` get rejected with `unknown_arguments` (see
`error.suggested_path` for the canonical nesting).

#### Watchlist universe references (`@watchlist:<tag>`)

`--universe` accepts watchlist-tag tokens mixed with literal symbols:

```bash
# Whole "核心持仓" tag from the watchlist.
doyoutrade-cli task create --name "核心池监控" \
  --definition sd-3f1c2a9b8e7d --universe '@watchlist:核心持仓'

# Every watchlist symbol.
doyoutrade-cli task update "MR Demo" --universe '@watchlist:*'

# Mix tags with literal symbols.
doyoutrade-cli task update "MR Demo" --universe '@watchlist:白酒,300750.SZ'
```

`@watchlist:<tag>` (and `@watchlist:*` = all watchlist symbols) is stored
on the task verbatim and **expanded eagerly at build time** into the
concrete symbols under that tag (emitting a `watchlist_universe_resolved`
observability event). A tag resolving to zero symbols stays visible in
that event rather than being silently dropped. Curate the tags via
`doyoutrade-cli watchlist ...` (see `doyoutrade-watchlist`); discover tag
names with `doyoutrade-cli watchlist tags`.

### `doyoutrade-cli task update <identifier>`

Patch semantics: only supplied flags are written; omit a flag to leave
its current value untouched.

```bash
doyoutrade-cli task update 550e8400-... --universe 600519.SH
doyoutrade-cli task update "MR Demo" --mode backtest
doyoutrade-cli task update 550e8400-... --params '{"agent": {"react_max_turns": 5}}'

# Rebind to a mock account (after account create --mode mock)
doyoutrade-cli task update 550e8400-... --account acct-mock-001

# Clear explicit binding — runtime falls back to the default account
doyoutrade-cli task update 550e8400-... --account ''
```

`--account` patch semantics: omit to leave unchanged; pass `acct-...` to
rebind; pass empty string to clear binding. There is **no** `--unbind-account`
flag.

Flags mirror `task create` (without the `--name` requirement). Reaching
back to the underlying tool: providing the same identifier as a `sd-…`
(a strategy definition) returns `wrong_identifier_type`; providing a name
with multiple matches returns `ambiguous_task_name` with `error.candidates`.

### `doyoutrade-cli task start <task_id>`

```bash
doyoutrade-cli task start 550e8400-e29b-41d4-a716-446655440000
```

Starts a configured / paused task. Lifecycle commands currently accept
`task_id` only — not exact task names.

### `doyoutrade-cli task pause <task_id>`

```bash
doyoutrade-cli task pause 550e8400-e29b-41d4-a716-446655440000
```

Pauses a running task without deleting it.

### `doyoutrade-cli task stop <task_id>`

```bash
doyoutrade-cli task stop 550e8400-e29b-41d4-a716-446655440000
```

Stops a task and leaves it in a terminal stopped state until restarted.

### Strategy signal alert readiness recipe

```bash
doyoutrade-cli task create \
  --name "600519 signal monitor" \
  --mode signal_only \
  --tick-mode cron_driven \
  --definition sd-3f1c2a9b8e7d

doyoutrade-cli task start <task_id>
doyoutrade-cli cron create --task-kind strategy_signal_alert ...
```

For `strategy_signal_alert`, `signal_only` alone is not enough. The task
must be:
- `mode='signal_only'`
- `tick_mode='cron_driven'`
- `status='running'`

Otherwise cron create/update fails with
`task_mode_not_signal_only`, `task_tick_mode_not_cron_driven`, or
`task_not_running_for_cron_signal`.

### `doyoutrade-cli task trigger add/update <task_identifier>`

Schedule + execution intent + delivery now live on a task's own
**trigger** child object (the preferred path; the legacy
`strategy_signal_alert` cron pipeline above is retired). Each trigger
carries one schedule (`--cron` / `--every` / `--at`), one intent
(`--intent signal_only|trade`), and one delivery mode.

```bash
# card delivery (deterministic flash, no model call) — pushes to the current session
doyoutrade-cli task trigger add <task_identifier> \
  --name "收盘信号推送" \
  --cron "50 14 * * mon-fri" --timezone Asia/Shanghai --trading-session ashare \
  --intent signal_only --deliver card

# prose delivery (Agent interpretation) — minimal valid payload + optional composer
doyoutrade-cli task trigger add <task_identifier> \
  --name "收盘信号解读" \
  --cron "50 14 * * mon-fri" --timezone Asia/Shanghai --trading-session ashare \
  --intent signal_only --deliver prose \
  --composer-agent-id <agent-id>
```

`--deliver` (default `card`):
- `card` = **deterministic flash** — the push card is rendered by code
  only, no model call.
- `prose` = **Agent interpretation** — on each fire an extra
  *compose-only* Agent round reads this cycle's `market_snapshot` and
  `signal_diagnostics` (incl. `rationale`) and writes a plain-Chinese
  card explaining "为什么触发了某信号 / 为什么本轮没动手". That round is
  framed to **only organize language and is forbidden from calling any
  tool**. If the composer fails or returns empty, the push **visibly
  falls back** to the deterministic card (ERROR log + `trigger.delivery.compose`
  span with `compose.status` ∈ `{no_agent, failed, empty}` and the matching
  `trigger_compose_*` span event) — the push is never silently dropped.
- `none` = run the cycle but push nothing.

`--composer-agent-id <agent-id>` (optional) is meaningful **only for
`prose`**: it picks which agent composes the push text; unset → the
backend uses the first active agent. `card` / `none` ignore it. It lands
in `delivery_json.composer_agent_id`.

Other delivery flags: `--no-signal-mode silent|brief|full` (default
`brief`), and a fixed-Feishu-group push needs both `--target-channel-id`
(the registered bot channel record id) and `--target-chat-id` (`oc_…`);
omit both to push back to the originating session.

Manage triggers with `doyoutrade-cli task trigger list <task_identifier>`
/ `get` / `update` / `pause` / `resume` / `run` (manual one-shot fire) /
`delete`. Deleting a task cascades its triggers.

### `doyoutrade-cli task delete <identifier>`

```bash
doyoutrade-cli task delete 550e8400-e29b-41d4-a716-446655440000
doyoutrade-cli task delete "MR Demo"   # exact name accepted
```

Confirmation-style: `data` carries only `task_id` and (when applicable)
`resolved_from_name`. Use `task get` to verify it's gone (expect exit 3
`task_not_found`).

### `doyoutrade-cli task clone <source_identifier> [...]`

```bash
doyoutrade-cli task clone 550e8400-...
doyoutrade-cli task clone "MR Demo" --name "MR Demo v2"
doyoutrade-cli task clone 550e8400-... --description "Re-run with wider universe"
```

Backtest tasks are one-shot — once a run exists, the task cannot be
re-run, so the standard pattern after a backtest is
`doyoutrade-cli task clone` → `doyoutrade-cli task update` (to tune the
clone) → `doyoutrade-cli backtest run` on the clone.

## Reading tool errors

See the main-agent system prompt's "CLI envelope 速读" section for the general error envelope. Stable
`error_code` tokens for this skill:

| `error_code` | Exit | When | What to do |
| --- | --- | --- | --- |
| `wrong_identifier_type` | 2 | `identifier` looks like `sd-...` (a strategy definition), not a task. | Read `error.actual_kind` / `error.expected_kind`. Use `strategy definition get` for the resource, or pick the right `task_id`. |
| `task_not_found` | 3 | No task matched by id or exact name. | Run `doyoutrade-cli task list --q <hint>` to discover candidates. |
| `ambiguous_task_name` | 1 | Multiple tasks share the name. | `error.candidates` lists each match with `task_id` / `status` / `mode`. Pick one and retry with the explicit `task_id`. |
| `missing_strategy_binding` | 2 | `task create` without `--definition` and without a valid `strategy` block in `--params`. | Add `--definition sd-… --params '{...}'`. |
| `missing_name` | 2 | `task create --name ""` or absent. | Click usually catches this first (required), but lower-cased empty strings can slip through. |
| `unknown_arguments` | 2 | Top-level key in `--params` that the tool's schema doesn't accept. | `error.allowed_top_level` lists the legal set; `error.suggested_path` maps offenders to their canonical nested location. |
| `invalid_params_json` | 2 | `--params` failed to parse, or wasn't a JSON object. | Fix the JSON; see the hint for a minimal sample. |
| `invalid_agent_json` / `invalid_strategy_json` / `invalid_universe_json` | 2 | The tool's coercion accepted a JSON-string fallback for an object/array field but the string failed to parse. | Pass the field as native JSON inside `--params`, not as a stringified blob. |
| `task_tick_mode_not_cron_driven` | 1 | A `strategy_signal_alert` cron was pointed at a task still using the default interval loop. | `task update <task_id> --tick-mode cron_driven`, then retry the cron create/update. |
| `task_not_running_for_cron_signal` | 1 | A `strategy_signal_alert` cron was pointed at a task that exists but is not `running`. | `task start <task_id>` before creating/updating the cron. |
| `validation_error` | 2 | Generic input validation. | Read `error.message`. |
| `list_tasks_failed` | 1 | Upstream `list_tasks_summary` raised. | Read `error.message` for the upstream exception. |

## Combining with bash

```bash
# Find one running task by name pattern and capture its id
TASK_ID=$(doyoutrade-cli task list --status running --q "Mean Reversion" --limit 1 \
            | jq -r '.data.items[0].task_id')

# Update it
doyoutrade-cli task update "$TASK_ID" --universe 600519.SH,000001.SZ

# All failed task ids today
doyoutrade-cli task list --status failed --limit 200 | jq -r '.data.items[].task_id'

# Quick existence check (exit code 3 ⇒ not found)
doyoutrade-cli task get "$NAME" >/dev/null
case $? in
  0) echo "exists" ;;
  3) echo "missing" ;;
  *) echo "error" ;;
esac

# Create → bind a custom approval policy on top of bind sugar
TASK_ID=$(doyoutrade-cli task create --name "MR Live" --definition sd-… --mode live \
            | jq -r '.data.task.task_id')
doyoutrade-cli strategy promote "$TASK_ID" sd-… \
    --approval-policy '{"min_notional_for_approval": 50000}'
```

## What this skill does *not* cover

- Strategy definitions — see `doyoutrade-strategy`.
- Backtest runs / debug views — see `doyoutrade-cli backtest run /
  summary / suggest-iteration` and `doyoutrade-cli debug get-run-view`.
- Cron jobs — see `doyoutrade-cron` (CLI is the only path; the agent
  has no in-process cron tools as of 2026-05-23).
- Symbol lookup — see `doyoutrade-stock`.
