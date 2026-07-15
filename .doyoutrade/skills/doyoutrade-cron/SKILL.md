---
name: doyoutrade-cron
description: Manage Doyoutrade cron jobs with `doyoutrade-cli cron ...` for list/get jobs, inspecting runs (incl. `runs trace` for a firing's aggregated spans+model_invocations and `runs by-trace <trace_id>` to reverse-resolve which firing a trace_id came from), and create/update/delete/pause/resume/trigger schedules. Use for cron history, run details, debugging a cron fire by trace_id, reminders, delayed actions, recurring tasks, or "X 分钟后 / every N / remind me" intents. There are no in-process cron tools; every cron action goes through the CLI. Never emulate scheduling with `execute_bash sleep`; create a cron job instead.
category: tool
style: process
---

<!-- Routing:
- Read-only inspection → `doyoutrade-cli cron list / get / runs ...`
  (reads direct from the DB; works even when the API server is down).
- Mutate scheduler state (create / update / delete / pause / resume /
  trigger) → `doyoutrade-cli cron create / update / ...` (HTTP to the
  running server — the only path; the in-process tool surface for
  cron was removed on 2026-05-23).
- Any "delay / wait / every / 每隔 / 提醒" intent → `doyoutrade-cli
  cron create`, NOT `execute_bash sleep` (see MEMORY.md note).
-->

# doyoutrade-cron

## When to use

- Read paths: "看一下我有什么 cron / list my crons / 上次这个 job 跑了什么 / 看一下 crun-xxx 的详情".
- Write paths: "起一个 every 5min 的 cron / 暂停这个 cron / 立刻触发一次 / 改一下 cron 表达式 / 删掉这个 cron".

The agent has no in-process cron tools — every interaction goes through
`execute_bash doyoutrade-cli cron ...`. Trace context still propagates
end to end: `execute_bash` injects W3C `TRACEPARENT` into the CLI
subprocess, `cli_trace_scope` starts a child `cli.<tool>` span, and
`emit_debug_event` calls inside the tool surface as span events that
the OTel processor exports to `debug_session_spans`. The debug UI sees
one continuous trace per cron call.

## Architecture quick note

`AgentCronManager` lives **inside the API server process** and owns the
APScheduler that fires jobs. Reads can run anywhere — they only query
the DB. Writes must reach the server because the scheduler only knows
about jobs it created through its own `create_job` path. The CLI's
write commands therefore POST/PUT/DELETE to the server's existing REST
endpoints (`/assistant/agents/<id>/cron/jobs[/<job_id>][/pause|resume|run]`).

When the server isn't running, write commands fail fast with a
structured `api_unavailable` envelope — no silent retry, no fall-back
DB write. Start the server, or set `DOYOUTRADE_API_URL` to a reachable
host.

## API base URL resolution

The CLI resolves which API server to talk to in this order:

1. `DOYOUTRADE_API_URL` env var (e.g. `http://127.0.0.1:8000`)
2. `api.base_url` in `config.yaml`
3. Derived from `server.host` / `server.port` (with `0.0.0.0`
   automatically rewritten to `127.0.0.1` since you can't dial the
   wildcard).

Set option 1 in long-running shell sessions, option 2 for permanent
non-default deploys. Read paths ignore this — they go direct to the DB.

## Commands

### Read — direct DB (no server needed)

#### `doyoutrade-cli cron list [--agent-id <id>]`

```bash
doyoutrade-cli cron list                       # defaults to DOYOUTRADE_AGENT_ID
doyoutrade-cli cron list --agent-id asst-x     # inspect another agent's jobs
```

#### `doyoutrade-cli cron get <job_id>`

```bash
doyoutrade-cli cron get cj-3a9b1f...
```

`data.cron_job` carries `cron_expression`, `timezone`, `task` (kind +
params), `pre_action`, `enabled`, `last_status`, `last_run_at`, etc.

#### `doyoutrade-cli cron runs list <job_id> [--limit N]`

```bash
doyoutrade-cli cron runs list cj-3a9b1f...
doyoutrade-cli cron runs list cj-3a9b1f... --limit 100
```

Newest first. Each row carries `id` (crun-...), `fired_at`, `status`,
`error`, `pre_kind`, `trace_id`, etc.

#### `doyoutrade-cli cron runs get <run_id>`

```bash
doyoutrade-cli cron runs get crun-9e7a2c1b8d...
```

The returned run carries handles into deeper views: `trace_id` (the
`cron.job.fire` OTel trace — feed to `doyoutrade-cli debug get-trace-view`),
`agent_session_id` (the agent's composition session — `doyoutrade-cli
assistant session get / export`), `pre_run_id` (`debug get-run-view`), and
`pre_debug_session_id`.

#### `doyoutrade-cli cron runs trace <run_id>`

```bash
doyoutrade-cli cron runs trace crun-9e7a2c1b8d...
```

Aggregates **spans + model_invocations across every session one cron fire
touched** — the agent session, the pre-action debug session, and any
per-instance cycle-run sessions (e.g. `strategy_signal_alert`). One call to
see the whole firing's trace instead of chasing each session id by hand.

#### `doyoutrade-cli cron runs by-trace <trace_id> [--limit N]`

```bash
doyoutrade-cli cron runs by-trace 184314d705cb687948c603ec508f003c
```

**Reverse lookup**: you have only an OTel `trace_id` (from a log line / span)
and need to know which cron firing produced it. Returns matching runs
(newest first); then drill in with `cron runs trace <crun-...>`. Empty
`items` means no cron fire carried that trace (it may be a non-cron trace —
try `doyoutrade-cli debug get-trace-view <trace_id>`).

### Write — HTTP to the running server

All write commands resolve `--agent-id` from the flag first, then fall
back to the `DOYOUTRADE_AGENT_ID` env var. Missing → fast-fail with
`missing_agent_id`.

#### `doyoutrade-cli cron create`

Three schedule shapes (exactly one required, mutually exclusive):

* `--in <duration>` — **prefer this for any "fire in N seconds/minutes"
  intent.** No cron expression, no timezone math: server computes
  `at_iso` against the host's local clock and stores it.
* `--at <ISO-8601 with offset>` — explicit one-shot at a specific
  instant. The offset eliminates timezone drift.
* `--cron-expression` — recurring 5-field cron. Use only when you
  genuinely need recurrence (e.g. "every weekday 9am").

LLM-friendly default — the "fire in 1 minute" pattern:

```bash
doyoutrade-cli cron create \
  --name "1-min reminder" \
  --in 60s \
  --input-template "时间到啦 :)"
```

Recurring pattern (daily 9am China time):

```bash
doyoutrade-cli cron create \
  --name "morning brief" \
  --cron-expression "0 9 * * *" \
  --timezone Asia/Shanghai \
  --input-template "It's 9am — summary please."
```

Full surface:

| Flag | Required | Default | Notes |
| --- | --- | --- | --- |
| `--agent-id` | env-fallback | `$DOYOUTRADE_AGENT_ID` | Owning agent. |
| `--name` | ✔ | — | Human-readable label. |
| `--in` | one-of | — | Duration: `60s`, `5m`, `2h`, `1d`. Server resolves to `at_iso` against host clock. **LLM-safe path.** |
| `--at` | one-of | — | ISO-8601 with offset, e.g. `2026-05-24T10:23:00+08:00`. |
| `--cron-expression` | one-of | — | 5-field cron, recurring. **See "Timezone trap" and "Weekday trap" below.** |
| `--timezone` |  | host local (`tzlocal`) | IANA tz id. Only meaningful with `--cron-expression`; ignored for `--at` / `--in`. |
| `--delete-after-run / --keep-after-run` |  | true for `--at` / `--in`, false for `--cron-expression` | Whether the job is deleted after fire. |
| `--input-template` | ✔ | — | Jinja2; receives `{{now}}`, `{{job}}`, `{{pre}}`. |
| `--max-concurrency` |  | `1` | APScheduler semaphore. |
| `--timeout-seconds` |  | `120` | Per-run timeout. |
| `--enabled / --disabled` |  | `--enabled` | Whether the job fires immediately. |
| `--pre-action` |  | — | JSON object with a string `kind`, e.g. `'{"kind":"trigger_cycle","params":{"task_id":"task-1"}}'`. |

Returns the server's job record under `data` (id, all fields, plus a
synthetic `next_fire_time` ISO timestamp and `next_fire_in_seconds`
relative int). Server 201 → CLI exit code 0.

**Sanity check the response before claiming the job is set up.** The
`next_fire_in_seconds` field is the LLM-safe field — read it; don't
read the ISO offset and try to mentally subtract. If you intended
"30s later" but the response shows `next_fire_in_seconds: 28800`,
the cron is misconfigured (almost always the Timezone trap below) —
fix it immediately or delete the job.

##### STOP — read this if you reach for `--cron-expression` for a delay

The dominant LLM failure mode here is reaching for cron syntax for
"fire in N seconds" intents (e.g. writing `'59 10 24 5 *'` when the
user says "1 minute later"). Four reasons NOT to do that:

1. **Cron precision is one minute.** "Fire in 60s" submitted at
   `10:58:54` becomes `next minute boundary` = ~6s delay, not 60s.
2. **Boundary miss wraps to next year.** Submit at
   `18:29:00.20` with `'29 18 D M *'` and APScheduler considers
   `18:29:00` "already passed" → next match is one year out. The
   server now detects this and refuses with a specific
   "calendar pin / minute just elapsed" diagnosis.
3. **Calendar pins `D M *` fire again next year** if the row stays
   enabled — easy to leave zombie state in the scheduler.
4. **Timezone math is on you** — you have to remember `--timezone`
   matches the clock you read the time from.

**Use `--in <duration>` instead.** It's a single string, server
resolves the rest:

```bash
doyoutrade-cli cron create \
  --name "1-min reminder" \
  --in 60s \
  --input-template "时间到啦"
```

If you write a calendar-pin cron expression anyway (e.g.
`59 10 24 5 *`) and its next fire is within 24h, **the server now
auto-promotes the row to `schedule_kind="at"` with
`delete_after_run=true`** and returns this notice in the response:

```
"_notice": "Auto-promoted: cron_expression '59 10 24 5 *' (...) is a one-shot calendar pin ... For the next such request, prefer `--in 60s` (...) directly — they skip cron expression / timezone math / minute-boundary rounding entirely and are second-level precise."
```

If you see `_notice` in the response, **the row is safe** — but
**you should switch to `--in` next time**. Don't paste "please
delete me" instructions into the template; the system handles
cleanup automatically.

##### Timezone trap (cron-kind only)

The `--timezone` flag defaults to the host's local TZ (resolved via
`tzlocal`). If you're writing a recurring cron expression and want
to pin the timezone explicitly (e.g. for portable schedules), pass
`--timezone UTC` or any IANA name. The default change closed the
old UTC-vs-local footgun for new rows.

If the response's `next_fire_in_seconds` doesn't match the delay you
intended, the cron is wrong — don't ship it.

##### Weekday trap (cron-kind only)

DoYouTrade registers recurring jobs with APScheduler
``CronTrigger.from_crontab()``. Its **day-of-week field is not Unix
cron**:

| System | Monday | Friday | Saturday |
| --- | --- | --- | --- |
| Unix / Vixie cron | `1` | `5` | `6` |
| APScheduler `from_crontab` | `0` or `mon` | `4` or `fri` | `5` or `sat` |

The dominant LLM mistake for "工作日 / weekday" is writing
``1-5``. In APScheduler that means **Tuesday–Saturday** — **Monday is
skipped**. Symptom: job fires Tue–Fri but never on Monday; on Monday
morning ``next_fire_time`` jumps to Tuesday 09:00.

**Always use APScheduler weekday names for Mon–Fri:**

```bash
# ✅ Mon–Fri, every 5 min during China A-share morning + afternoon sessions
--cron-expression "*/5 9-11,13-14 * * mon-fri"

# ✅ numeric APScheduler form (0=Mon .. 4=Fri) — also fine
--cron-expression "*/5 9-11,13-14 * * 0-4"

# ❌ Unix-style "weekdays" — skips Monday in APScheduler
--cron-expression "*/5 9-11,13-14 * * 1-5"
```

On create/update the server **auto-rewrites** bare ``1-5`` /
``1,2,3,4,5`` in the day-of-week field to ``mon-fri`` and returns a
``_notice`` explaining the fix. Legacy rows still stored as ``1-5`` are
also registered as ``mon-fri`` at server boot (warning log only — run
``cron update`` to persist).

**Sanity check:** after create/update, read ``next_fire_time``. If you
intended "today (Monday) during market hours" but ``next_fire_in_seconds``
points to **tomorrow morning**, the weekday field is still wrong.

##### A-share intraday window (strategy_signal_alert)

Standard 5-field cron cannot express partial hours (e.g. start at
**09:15**, stop at **11:30**, resume **13:00**, end **15:00**) in one
rectangular expression. For China A-share **continuous auction** use a
**superset scheduler** plus an executor gate:

1. Cron expression (fires every 5 min across the hour blocks that cover
   the sessions — includes a few off-session ticks that get skipped):

```bash
--cron-expression "*/5 9-11,13-15 * * mon-fri" \
--timezone Asia/Shanghai
```

2. ``task_params.trading_session: "ashare"`` on ``strategy_signal_alert``
   jobs — scheduled fires outside **Mon–Fri 09:15–11:30** and
   **13:00–15:00** (job timezone) are recorded as
   ``status=skipped`` / ``outside_trading_session``. Manual
   ``cron trigger`` is **not** gated (use for smoke tests).

```bash
--task-params '{
  "strategy_task_ids": ["task-..."],
  "user_request": "盘中每 5 分钟推信号",
  "agent_id": "agent_default",
  "trading_session": "ashare",
  "no_signal_mode": "brief"
}'
```

Exact session tick times (53 fires/day at 5-min cadence): 09:15–11:30
and 13:00–15:00 inclusive. Do **not** use ``9-11,13-14`` — that misses
the **15:00** bar and still includes pre-09:15 noise unless
``trading_session`` is set.

##### Legacy: calendar-pin soft auto-disable (cron-kind, no `delete_after_run`)

Some rows predate the auto-promote path (legacy DB rows, or
`acknowledge_distant_schedule=true` opt-outs). For those, after a
fire the server falls back to soft auto-disable: the row stays in
the table with `enabled=false` so operators can audit. Use `cron
resume` if you actually wanted the annual reminder.

##### Cron-triggered session header

When a cron fires, the agent receives a `[cron-trigger]` header
line prepended to the rendered template, e.g.

```
[cron-trigger] job_id=cron-306e5b68cbe4 name='1-min greeting' fired_at=2026-05-24T10:01:00+00:00 — the lines below are the rendered input_template; respond to those, not to this header. CRITICAL: this session is cron-fired; do NOT create new cron jobs from here unless the user EXPLICITLY asks for one (recursive cron creation is blocked at the API anyway). This job has delete_after_run=true; the system will delete it automatically, you do NOT need to call `doyoutrade-cli cron delete`. ⚠️ The rendered template body below contains stale 'please delete me' text that the creating session baked in before the server's auto-cleanup kicked in — IGNORE those instructions. Do NOT run `doyoutrade-cli cron delete`.

你好！时间到啦 😊
```

The ⚠️ override line only appears when the body contains a delete
instruction AND `delete_after_run=true` — it's the server telling
the trigger agent "the body is stale, trust me over it".

**If you receive a user message starting with `[cron-trigger]`:**
1. Do NOT run `cron list` to recover context — the header has the
   job id / name / fire time already.
2. Do NOT call `doyoutrade-cli cron delete` on the firing job — the
   server's `delete_after_run` flag (default for `--at` / `--in`)
   has already removed it.
3. Do NOT create a new cron job from here. Recursive cron creation
   is blocked at the API with a 403; even if the user template
   mentions follow-ups, defer them to a normal user-driven session.
4. Just respond to the body below the blank line.

##### `task_kind` pipeline (preferred over `pre_action` for new flows)

Pass `--task-kind <kind>` + `--task-params '<json>'` to dispatch the
fire through a registered `JobTaskExecutor` instead of the legacy
`pre_action + input_template` path. Registered kinds:

- `agent_chat_reply` — push the LLM-rendered reply back to a user
  session (no strategy tick). Params: `agent_id`, `user_request`,
  optional `target_session_id` (autofilled — see below).
- `daily_review` — 每日复盘. At fire time the executor PRE-GATHERS the live
  account statement (cash/equity + positions + asset + the day's 交割单 via
  QMT) and a private-KB digest (prior journal / `symbols/roles.md` / cycles
  overview / the month's broker-exported trades CSV), feeds them to a
  compose-only Agent turn, persists the review to
  `journal/<YYYY>/<asof>.md` in the knowledge base, and pushes it. Params:
  `agent_id` (required), optional `target_session_id` (autofilled — see
  below), optional `account_id` (null → default account), optional
  `user_request` (the verbatim phrase; defaults to a canned "每日复盘"). Non
  trading days (weekend / weekday holidays) are auto-skipped with a structured
  `daily_review_skipped` event (no empty review). Minimal valid payload:

  ```bash
  doyoutrade-cli cron create \
    --name "每日收盘复盘" \
    --cron-expression "30 15 * * mon-fri" --timezone Asia/Shanghai \
    --task-kind daily_review \
    --task-params '{"agent_id":"asst-...","user_request":"每天收盘后帮我复盘当天交易"}'
  ```

  Create/update-time `error_code`s: `missing_agent_id`,
  `invalid_target_session_id`, `invalid_account_id`, `invalid_user_request`,
  `invalid_task_params`.
- `deviation_monitor` — 交易纪律提醒. Watch held positions intraday and remind
  the user **only when price deviates from their plan** (破5日线 / 大阴线 /
  连阳被破坏 / 放量下跌 / 跌破成本), recalling the original buy thesis. The
  deviation logic is a **user-authored Strategy SDK strategy** (`sd-…`) whose
  `on_bar` returns `Signal.sell` on a breach and `Signal.hold` otherwise — full
  rule flexibility behind the `sdk validate` safety net. At fire time the
  executor compiles that strategy, fetches the **live quote** (spliced onto
  warehouse history as today's forming bar so `on_bar` sees the 14:50 price),
  reads the **real position cost basis** (so 跌破成本 / `current_profit` are
  accurate), evaluates per symbol, and delivers a reminder — or stays silent
  (`[SILENT]`) when nothing deviates. A symbol no longer held is skipped
  (`deviation_monitor_skipped` / reason `position_closed`); a symbol with no
  usable live quote is skipped (`deviation_monitor_quote_unavailable`). Params:
  `strategy_definition_id` (required `sd-…`), `symbols` (required non-empty
  list), optional `thesis` (string applied to all, or `{symbol: text}` map —
  recalled verbatim in the reminder), optional `target_session_id` (autofilled),
  optional `account_id` (null → default), optional `parameter_overrides`
  (strategy params), optional `require_position` (default `true`; `false`
  monitors even when flat), optional `data_source` (default `auto`). Minimal
  valid payload:

  ```bash
  doyoutrade-cli cron create \
    --name "纪律提醒-600519" \
    --cron-expression "50 14 * * mon-fri" --timezone Asia/Shanghai \
    --task-kind deviation_monitor \
    --task-params '{"strategy_definition_id":"sd-...","symbols":["600519.SH"],"thesis":"连阳、不破5日线，跌破止损就提醒我"}'
  ```

  Create/update-time `error_code`s: `missing_strategy_definition_id`,
  `missing_symbols`, `invalid_symbols`, `invalid_target_session_id`,
  `invalid_thesis`, `invalid_account_id`, `invalid_parameter_overrides`,
  `invalid_require_position`, `invalid_task_params`. Fire-time skips/failures
  surface as debug events: `deviation_monitor_strategy_unavailable` (compile
  failed), `deviation_monitor_data_unavailable` (statement / quote read raised),
  `deviation_monitor_skipped` (not held), `deviation_monitor_quote_unavailable`,
  `deviation_monitor_rule_failed` (the strategy raised), `deviation_monitor_evaluated`.
- `stock_report` — 定时个股研报推送. At fire time the executor fetches ~60
  daily bars per symbol, scores each deterministically (close vs MA20, 5-day
  change, Wilder RSI14 — **no LLM call**), renders a markdown report
  (`doyoutrade/prompts/report/markdown.j2`), optionally converts it to a PNG
  (`as_image: true`; conversion failure emits `md2img_unavailable` and falls
  back to text — never a hard failure), persists it to the knowledge base at
  `reports/<YYYY>/<YYYY-MM-DD>-<slug>.md` (read `data.report_path` from the
  run result), and delivers to `target_session_id` (omit to only persist).
  Params: `symbols` (required non-empty list of canonical symbols — `stock
  lookup` first), optional `title`, `language` (`zh`|`en`, default `zh`),
  `as_image` (bool, default `false`), `target_session_id`. Minimal valid
  payload:

  ```bash
  doyoutrade-cli cron create \
    --name "早盘研报-600519" \
    --cron-expression "0 9 * * mon-fri" --timezone Asia/Shanghai \
    --task-kind stock_report \
    --task-params '{"symbols":["600519.SH"],"language":"zh","as_image":false}'
  ```

  Create/update-time `error_code`s: `invalid_task_params`, `invalid_symbols`,
  `invalid_title`, `invalid_language`, `invalid_as_image`,
  `invalid_target_session_id`. Fire-time events: `stock_report.symbol_failed`
  (per-symbol fetch failure, batch continues), `stock_report.gathered`,
  `stock_report.rendered`, `md2img_unavailable`,
  `stock_report.image_delivery_failed` (falls back to text),
  `stock_report.delivered`, `stock_report.journal_failed` (non-fatal).
- `strategy_signal_alert` — **RETIRED** (kept here for historical cron rows
  only; new `--task-kind strategy_signal_alert` creates are rejected with
  `cron_strategy_kind_retired`). Schedule strategy ticks via a **Task Trigger**
  instead (`doyoutrade-cli task trigger add ...`, see the **doyoutrade-task**
  skill). Original behaviour: tick one or more strategy tasks and
  push a structured signal alert. Params: `strategy_task_ids`
  (non-empty list of `task-...` ids), `user_request` (the verbatim
  phrase that motivated the schedule), `agent_id`, optional
  `target_session_id`, optional `no_signal_mode`.
  `no_signal_mode` ∈ `{silent, brief, full}` (default `brief`) decides what
  happens when a cycle completes normally but yields no actionable signal:
  `silent` → reply `[SILENT]`, push suppressed (avoids intraday spam);
  `brief` → push a one-line "no new signal" note (confirms the job is alive);
  `full` → push the note plus, per symbol, latest price / 涨跌幅 (from
  `instance.market_snapshot`), the strategy decision factors (from
  `instance.signal_diagnostics`: `direction` / `tag` / `rationale` /
  indicator `diagnostics`), and an account/position snapshot. An out-of-set
  value is rejected at create/update time with `invalid_no_signal_mode`.

  **Delivery mode — `card` (deterministic) vs `prose` (Agent-composed):**
  the new task-trigger surface (`doyoutrade-cli task trigger add/update`,
  see the **doyoutrade-task** skill) chooses how the push is rendered via
  `delivery_json.mode`:
  - `card` = **deterministic flash** — the push card is rendered by code
    only, no model call.
  - `prose` = **Agent interpretation** — when a trigger fires, an extra
    *compose-only* Agent round reads this cycle's `market_snapshot` and
    `signal_diagnostics` (incl. `rationale`) and writes a plain-Chinese card
    explaining "为什么触发了某信号 / 为什么本轮没动手". That round is framed to
    **only organize language and is forbidden from calling any tool**. If the
    composer fails or returns empty, the push **visibly falls back** to the
    deterministic card — the push is never silently dropped. The fallback is
    observable on the `trigger.delivery.compose` span: `compose.status` ∈
    `{ok, no_agent, failed, empty}` with the matching span events
    `trigger_compose_no_agent` / `trigger_compose_failed` / `trigger_compose_empty`,
    plus an ERROR log. Inspect it via `cron runs trace` on the firing.
    `delivery_json.composer_agent_id` (optional) picks which agent composes;
    unset → the backend uses the first active agent. `composer_agent_id` is only
    meaningful for `prose`; `card` / `none` ignore it.
  **Hard constraint:** each task must already be cron-ready:
  `mode='signal_only'`, `tick_mode='cron_driven'`, and `status='running'`.
  Non-ready tasks are rejected at cron create/update time with stable
  error codes such as `task_mode_not_signal_only`,
  `task_tick_mode_not_cron_driven`, and
  `task_not_running_for_cron_signal`; runtime drift is surfaced with the
  same per-task statuses in cron run history. Build the monitoring task
  first via `doyoutrade-cli task create --mode signal_only --definition
  sd-... ...`, then `doyoutrade-cli task update <task-...> --tick-mode
  cron_driven`, then `doyoutrade-cli task start <task-...>` before
  scheduling the alert.
- `noop` — for diagnostic / smoke-test cron fires.

`target_session_id` autofill: when the cron-create HTTP request
carries the header `X-DOYOUTRADE-Calling-Session-Id` (which the CLI
sets to the caller's assistant session id) AND `task_params` does not
already contain `target_session_id`, the server fills it in. Explicit
values (including a deliberate `null`) are respected. Net effect: in
the typical "在飞书群里跟 agent 说定时推信号" flow, the agent omits
`target_session_id` and the resulting push lands back in the
originating channel automatically — `_deliver.py` forwards via
`channel.send()` when the target session has a `config.channel`
binding.

Example:

```bash
doyoutrade-cli task create \
  --name "600519 signal monitor" \
  --mode signal_only \
  --definition sd-abc123 \
  --tick-mode cron_driven \
  --params '{"window": 14}'
doyoutrade-cli task start task-9b3d2c1f

doyoutrade-cli cron create \
  --agent-id asst_a1b2 \
  --name "30min signal alert" \
  --cron-expression "*/5 9-11,13-15 * * mon-fri" \
  --timezone Asia/Shanghai \
  --task-kind strategy_signal_alert \
  --task-params '{
    "strategy_task_ids": ["task-9b3d2c1f"],
    "user_request": "盘中每 30 分钟给我推 600519 的信号",
    "agent_id": "asst_a1b2",
    "trading_session": "ashare",
    "no_signal_mode": "silent"
  }'
```

(`no_signal_mode: "silent"` here keeps intraday pushes quiet unless a real
signal fires; omit it — or set `"brief"` / `"full"` — when the user wants a
heartbeat even on no-signal cycles.)

(`target_session_id` deliberately omitted — server autofills from the
calling session.)

#### `doyoutrade-cli cron update <job_id>`

Partial update — only sends fields you explicitly pass. Examples:

```bash
# pause via the "disabled" toggle (idempotent re-write)
doyoutrade-cli cron update cj-3a9b1f --disabled

# change schedule & rename
doyoutrade-cli cron update cj-3a9b1f \
  --cron-expression "0 9 * * mon-fri" --name "weekday 9am"

# clear any existing pre_action (sends pre_action: null)
doyoutrade-cli cron update cj-3a9b1f --clear-pre-action

# replace pre_action wholesale
doyoutrade-cli cron update cj-3a9b1f \
  --pre-action '{"kind":"trigger_cycle","params":{"task_id":"task-2"}}'
```

Notes:
- `--enabled / --disabled` is tri-state: omit to leave unchanged.
- `--pre-action` and `--clear-pre-action` are mutually exclusive
  (rejected with `validation_error`).
- An empty patch (no fields supplied) is a noop success — no HTTP call
  is issued.

#### `doyoutrade-cli cron delete <job_id>`

```bash
doyoutrade-cli cron delete cj-3a9b1f
```

Server returns 204 → CLI returns an empty success envelope.

#### `doyoutrade-cli cron pause <job_id>`

```bash
doyoutrade-cli cron pause cj-3a9b1f
```

Deregisters from the scheduler but keeps the DB row (`enabled=false`).
Resumable.

#### `doyoutrade-cli cron resume <job_id>`

```bash
doyoutrade-cli cron resume cj-3a9b1f
```

Sets `enabled=true` and re-registers with APScheduler.

#### `doyoutrade-cli cron trigger <job_id>`

```bash
doyoutrade-cli cron trigger cj-3a9b1f
```

Fires the job once now, fire-and-forget. Returns
`data.cron_job_run_id` (a fresh `crun-...`) so you can poll history via
`cron runs get`.

## Reading tool errors

These error_codes (stable contract tokens — branch on them, not on
`message`) are specific to this skill:

| `error_code` | Exit | When | Repair |
| --- | --- | --- | --- |
| `missing_agent_id` | 2 | No `--agent-id` flag and `DOYOUTRADE_AGENT_ID` unset | Pass `--agent-id <asst_...>` or export the env var. |
| `invalid_pre_action_json` | 2 | `--pre-action` body wasn't valid JSON or lacked a string `kind` field | Fix the JSON; minimum shape `{"kind":"...","params":{...}}`. |
| `validation_error` | 2 | Server rejected the payload (bad cron expression, mutually-exclusive flags, missing required field) | Read `error.message`; common case is APScheduler rejecting the cron expression. |
| `cron_job_not_found` | 3 | `job_id` doesn't exist (or doesn't belong to the resolved `--agent-id`) | Verify via `doyoutrade-cli cron list`. |
| `cron_job_run_not_found` | 3 | `runs get` / `runs trace` — no `crun-...` matched | Verify via `doyoutrade-cli cron runs list <job_id>`. |
| `agent_not_found` | 3 | `--agent-id` doesn't match any agent row | Verify via the agent-list tool. |
| `api_unavailable` | 1 | Could not reach the API server (connection refused / DNS / TLS) | Start the server (`uvicorn doyoutrade.api.app:app`) or fix `DOYOUTRADE_API_URL`. |
| `task_mode_not_signal_only` | per-task / create-time | `strategy_signal_alert` target task is not `mode='signal_only'`. | Create or rebind a `--mode signal_only` monitoring task, then retry cron create/update. |
| `task_tick_mode_not_cron_driven` | per-task / create-time | Target task is still `tick_mode='interval'`. | `doyoutrade-cli task update <task-...> --tick-mode cron_driven`, then retry. |
| `task_not_running_for_cron_signal` | per-task / create-time | Target task is not `running`. | `doyoutrade-cli task start <task-...>` before creating/updating the cron. |
| `no_cron_ready_tasks` | per-job | Every listed `strategy_task_id` failed the cron-ready precheck, so no cycle was dispatched. | Fix mode / tick_mode / running state and retrigger via `doyoutrade-cli cron trigger`. |
| `task_lookup_failed` | per-task | The executor could not fetch the task from the repository while pre-checking readiness (transient DB issue or stale task pointer). | Verify the task via `doyoutrade-cli task get`; if missing, prune the id from cron params. |
| `invalid_no_signal_mode` | 2 | `task_params.no_signal_mode` is not one of `silent` / `brief` / `full`. | Drop the field (defaults to `brief`) or set one of the three values. |
| `invalid_trading_session` | 2 | `task_params.trading_session` is set but not `ashare`. | Omit the field or set `"ashare"`. |
| `invalid_account_id` | create-time | `daily_review` `task_params.account_id` is set but not a non-empty string. | Omit it (uses the default account) or pass a valid `acct-...` id. |
| `invalid_user_request` | create-time | `daily_review` `task_params.user_request` is set but not a string. | Omit it (defaults to a canned phrase) or pass a string. |
| `missing_strategy_definition_id` | create-time | `deviation_monitor` `task_params.strategy_definition_id` missing/blank. | Pass the `sd-...` id of your deviation strategy (author it first via strategy-authoring). |
| `missing_symbols` | create-time | `deviation_monitor` `task_params.symbols` is not a non-empty array. | Pass `"symbols":["600519.SH", ...]` of the held stocks to watch. |
| `invalid_symbols` | create-time | `deviation_monitor` `task_params.symbols` contains a non-string / empty entry. | Make every entry a non-empty canonical symbol. |
| `invalid_thesis` | create-time | `deviation_monitor` `task_params.thesis` is not a string / `{symbol: text}` object / null. | Pass a string, a symbol→text map, or omit it. |
| `invalid_parameter_overrides` | create-time | `deviation_monitor` `task_params.parameter_overrides` is not an object/null. | Pass an object of strategy params or omit. |
| `invalid_require_position` | create-time | `deviation_monitor` `task_params.require_position` is not a boolean/null. | Pass `true`/`false` or omit (defaults `true`). |
| `cron_strategy_kind_retired` | create-time | Tried to create a `strategy_signal_alert` / `strategy_cycle` cron (retired). | Schedule the strategy tick via a Task Trigger (`doyoutrade-cli task trigger add ...`). |
| `api_timeout` | 1 | Request timed out (default 15s) | Server is up but slow; retry or investigate. |
| `api_transport_error` | 1 | Other transport-layer error (TLS handshake, invalid URL, …) | Check `error.message` for the underlying httpx exception. |
| `server_unavailable` | 1 | HTTP 503 from server (e.g. `cron_run_repo` not configured) | Check server logs; bootstrap mis-wired. |
| `server_error` | 1 | HTTP 5xx with no more specific code | Read server logs; usually an APScheduler / DB exception. |
| `conflict` | 1 | HTTP 409 (rare for cron) | Reserved; current routes don't emit this. |

All write commands additionally honor the cross-CLI envelope contract
defined in the main-agent system prompt's "CLI envelope 速读" section —
exit codes, `meta` block, and `_notice` work identically here.

## Combining with bash

```bash
# Find a job by name pattern, capture its id
JOB_ID=$(doyoutrade-cli cron list | jq -r '.data.items[] | select(.name | contains("daily")) | .id' | head -1)

# Pull its last 5 runs
doyoutrade-cli cron runs list "$JOB_ID" --limit 5 | jq '.data.items[] | {id, status, fired_at, error}'

# Quick "any failures in the last 20 runs?" probe
doyoutrade-cli cron runs list "$JOB_ID" --limit 20 | jq '.data.items[] | select(.status == "failed") | .id'

# Pause a noisy job, capture exit code so the pipeline halts on api_unavailable
if ! doyoutrade-cli cron pause "$JOB_ID" >/tmp/cron-pause.json; then
    code=$(jq -r '.error.error_code' /tmp/cron-pause.json)
    echo "pause failed: $code" >&2
    exit 1
fi
```

See the main-agent system prompt's "CLI envelope 速读" section for the general envelope + exit-code
rules.
