---
name: doyoutrade-account
description: Manage Doyoutrade QMT accounts with `doyoutrade-cli account ...` for list/get/create/update/delete/set-default. Use for listing accounts, adding live or mock accounts, editing base_url/token/session settings, setting the default account, deleting accounts, or binding a task to `acct-...` via task commands. Accounts are DB-backed and carry their own live|mock mode; the default account supplies market data for account-less paths. There is no in-process account tool; use the CLI, with writes routed through the API server.
category: tool
style: process
---

<!-- Routing:
- All account commands go through `execute_bash doyoutrade-cli account ...`.
  Reads (list/get) and writes (create/update/delete/set-default) all reach the
  running API server's `/accounts` endpoints (server owns validation +
  the at-most-one-default invariant). When the server is down, writes fail
  fast with a structured `api_unavailable` envelope.
- Binding an account to a TASK is a `task` command, not here:
  `doyoutrade-cli task create|update --account <acct-…>` (writes settings.account_id).
-->

# doyoutrade-account

## When to use

- "列出账户 / list my accounts / 看下有哪些 QMT 账户" → `doyoutrade-cli account list`.
- "新建实盘账户 / add a live QMT account" → `doyoutrade-cli account create --mode live ...`.
- "建个 mock 账户做信号测试" → `doyoutrade-cli account create --mode mock ...`.
- "改账户连接 / 换 base_url / token" → `doyoutrade-cli account update <acct-…> ...`.
- "把这个设成默认账户" → `doyoutrade-cli account set-default <acct-…>`.
- "删掉这个账户" → `doyoutrade-cli account delete <acct-…>`.
- "把任务绑到这个账户" → NOT here; `doyoutrade-cli task update <task-…> --account <acct-…>`.

## Account model (what each field means)

- **`mode`** (`live` | `mock`): the account IS one or the other. `live` opens a
  real QMT trading-terminal session (account/positions from the broker); `mock`
  uses the in-memory mock portfolio (no terminal). There is **no per-task
  account_mode** anymore — the mode lives on the account.
- **Connection**: `base_url` (QMT proxy), `token` (proxy API key, plaintext),
  `timeout_seconds`. Even `mock` accounts can carry a base_url so they still pull
  real market data through the proxy.
- **Trading identity**: `qmt_account_id` (the broker account number passed to
  `trading.connect`; null for mock), `session_id` (refreshed automatically on
  connect and written back to the row).
- **Mock portfolio**: `mock_cash` / `mock_equity` / `mock_positions` (used when
  `mode=mock`).
- **`is_default`**: exactly one account is the default. The default account's
  connection feeds account-less market-data paths (backtest / `data run` /
  screening / sector / fundamentals) and is used by any task that doesn't bind
  an explicit `account_id`. Setting a new default clears the flag on all others.
- **`enabled`**: a disabled account is not used as the default and can't be bound.

## Commands

### `doyoutrade-cli account list`

```bash
doyoutrade-cli account list
```
`data.items[]` — each carries `id` (`acct-…`), `name`, `mode`, `base_url`,
`qmt_account_id`, `is_default`, `enabled`, timestamps. `token` / `session_id`
are returned as-is (local single-user, plaintext).

### `doyoutrade-cli account get <acct-…>`

```bash
doyoutrade-cli account get acct-6d685426f9e9
```

### `doyoutrade-cli account create`

`--name` and `--mode` are required.

```bash
# live account (real broker) + make it the default
doyoutrade-cli account create \
  --name "QMT live main" \
  --mode live \
  --base-url http://your-qmt-host:8000 \
  --token YOUR_QMT_TOKEN \
  --qmt-account-id YOUR_BROKER_ACCOUNT \
  --default

# mock account for signal-only testing (no live terminal needed)
doyoutrade-cli account create --name "mock-sandbox" --mode mock
```

| Flag | Required | Notes |
| --- | --- | --- |
| `--name` | ✔ | Human-readable label. |
| `--mode` | ✔ | `live` \| `mock`. |
| `--base-url` |  | QMT proxy base URL. **Required for live, and for any account you want to serve as the market-data default.** |
| `--token` |  | QMT proxy API key (proxy-level, shared across broker accounts on the same proxy). |
| `--qmt-account-id` |  | Broker trading account number (live `trading.connect`). |
| `--timeout-seconds` |  | Proxy HTTP timeout (default 5.0). |
| `--mock-cash` / `--mock-equity` |  | mock portfolio cash / equity. |
| `--mock-positions` |  | JSON list, e.g. `'[{"symbol":"600000.SH","quantity":100,"cost_price":10}]'`. |
| `--default / --no-default` |  | Make this the sole default (clears others). |
| `--enabled / --disabled` |  | Default enabled. |

### `doyoutrade-cli account update <acct-…>`

Partial — only the flags you pass change. Same flag surface as `create`.

```bash
doyoutrade-cli account update acct-6d685426f9e9 --base-url http://newhost:8000 --token new-key
doyoutrade-cli account update acct-6d685426f9e9 --disabled
```

### `doyoutrade-cli account set-default <acct-…>`

```bash
doyoutrade-cli account set-default acct-b3f90fd859e2
```
Makes this account the sole default; clears `is_default` on every other row.

### `doyoutrade-cli account delete <acct-…>`

```bash
doyoutrade-cli account delete acct-ca6bfd00ed2a
```
Refused with `account_in_use` (HTTP 409) if a task binds it — rebind / delete
those tasks first.

## Binding an account to a task

```bash
# create a signal-only task on a specific account
doyoutrade-cli task create --name "..." --mode signal_only --tick-mode cron_driven --definition sd-... --account acct-b3f90fd859e2
doyoutrade-cli task start <task_id>
# or rebind an existing task
doyoutrade-cli task update <task-…> --account acct-b3f90fd859e2
```
Omit `--account` to fall back to the default account. A **live** task that
can't resolve any enabled account fails its cycle with `account_resolution_failed`
(visible in the debug session / cron run — never silently downgraded to mock).

For `strategy_signal_alert` cron jobs, the bound monitoring task must be
`signal_only + cron_driven + running` before you create the cron; otherwise
cron create/update is rejected with readiness errors such as
`task_tick_mode_not_cron_driven` or `task_not_running_for_cron_signal`.

## Reading tool errors

Branch on `error_code` (stable tokens), not on `message`:

| `error_code` | When | Repair |
| --- | --- | --- |
| `account_not_found` | `acct-…` doesn't exist (get/update/delete/set-default, or a task binding it) | Verify via `doyoutrade-cli account list`. |
| `account_in_use` | `delete` refused — a task binds this account (HTTP 409) | Rebind / delete those tasks, then retry. |
| `account_disabled` | A bound account is disabled (surfaced at task create / cycle run) | `account update <id> --enabled` or bind a different account. |
| `account_resolution_failed` | A live cycle couldn't resolve an enabled account (debug event, not a CLI envelope) | Bind an enabled account or `account set-default`. |
| `validation_error` | Bad payload (missing `--name` / `--mode`, bad `--mock-positions` JSON, mode ∉ {live,mock}) | Read `error.message`. |
| `api_unavailable` | Server unreachable (write commands) | Start the server (`uv run doyoutrade`) or fix `DOYOUTRADE_API_URL`. |

API base URL resolution is shared with the cron skill: env `DOYOUTRADE_API_URL`
→ `api.base_url` in config → derived from `server.host`/`server.port`.

## Combining with bash

```bash
# default account id
doyoutrade-cli account list | jq -r '.data.items[] | select(.is_default) | .id'
# all live accounts
doyoutrade-cli account list | jq -r '.data.items[] | select(.mode=="live") | "\(.id) \(.qmt_account_id)"'
```

See the main-agent system prompt's "CLI envelope 速读" for the general envelope +
exit-code contract.
