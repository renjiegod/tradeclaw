---
name: doyoutrade-watchlist
description: Manage the user's stock watchlist (自选股) with `doyoutrade-cli watchlist ...` — collect / favorite symbols, tag them into groups, edit notes, and read live quotes (last price / change% / turnover). Use when the user asks "加到自选 / 收藏这只票 / 看我的自选股 / 打个标签分类 / 按标签筛自选 / 看实时行情 / 盯盘 / show my watchlist / add to watchlist / tag this stock / watch these symbols / live quotes". The local K-line库 only syncs watchlist symbols by default, and task universes can reference `@watchlist:<tag>`. Resolve symbols with `doyoutrade-stock` first; companion to `doyoutrade-task` (universe) and `strategy-definition-authoring` (`ctx.dp.watchlist_symbols`).
category: tool
style: process
---

<!-- Routing:
- All watchlist commands go through `execute_bash doyoutrade-cli watchlist ...`;
  reads + writes reach the running API server's `/watchlist` endpoints.
- Resolve a Chinese name / partial / code → canonical `CODE.EXCHANGE`
  BEFORE adding it → `doyoutrade-stock`. The watchlist does not re-derive
  suffixes.
- Reference watchlist tags from a TASK universe (`@watchlist:<tag>`) →
  `doyoutrade-task`.
- Read watchlist symbols inside strategy code (`ctx.dp.watchlist_symbols`)
  → `strategy-definition-authoring`.
- OHLCV / indicators / news / research reports / earnings for a watched symbol → `doyoutrade-data`.
-->

# doyoutrade-watchlist

## When to use

Trigger whenever the user wants to *curate* the stocks they care about:
favorite / collect a symbol, group them with tags, attach a note, or
glance at live quotes (股价 / 涨跌幅 / 成交额). The watchlist is a single
pool — one row per symbol — and **tags are the grouping mechanism**
(a symbol can carry several tags).

Two behaviours make the watchlist more than a bookmark list:

- **Local K-line sync scope**: the local OHLCV库 (used by `data run` /
  backtest / `stock screen`) syncs **only watchlist symbols** by default,
  re-read each cycle. To widen the synced universe, *add the symbol to the
  watchlist*. An empty watchlist syncs zero symbols (the全 A 股名录 behind
  `stock lookup` is refreshed independently and is unaffected).
- **Universe references**: a task `--universe` can contain
  `@watchlist:<tag>` (`@watchlist:*` = all watchlist symbols), expanded to
  concrete symbols at build time (see `doyoutrade-task`).

Typical user utterances:

- "把茅台加到自选 / 收藏这只票" → `watchlist add`
- "看我的自选股 / 列出自选" → `watchlist list`
- "给这几只打个'核心持仓'标签" → `watchlist add ... --tags 核心持仓` / `watchlist update`
- "按'打板'标签筛一下自选" → `watchlist list --tag 打板`
- "我有哪些标签" → `watchlist tags`
- "看自选的实时行情 / 现在多少钱 / 成交额多大" → `watchlist quotes`
- "把这只从自选里删掉" → `watchlist remove`

## Commands

### `doyoutrade-cli watchlist list [--tag <tag>]`

```bash
doyoutrade-cli watchlist list
doyoutrade-cli watchlist list --tag 核心持仓
```

`data.items[]` — each entry carries `id` (`wl-…`), `symbol`
(canonical `CODE.EXCHANGE`), `display_name`, `tags` (list), `note`,
`sort_order`, timestamps. `--tag` filters to entries carrying that tag.

### `doyoutrade-cli watchlist get <wl-id>`

```bash
doyoutrade-cli watchlist get wl-3f1c2a9b8e7d
```

Returns the single entry under `data.entry`. The argument must be a
literal `wl-…` id read from a prior `list` / `add` envelope — never a
name-derived guess. A `task-…` / `sd-…` shape returns
`wrong_identifier_type`.

### `doyoutrade-cli watchlist add <symbol> [--tags a,b] [--note "..."] [--display-name "..."] [--sort-order N]`

```bash
# Minimal valid payload — one resolved symbol.
doyoutrade-cli watchlist add 600519.SH

# Tag + note + display name.
doyoutrade-cli watchlist add 600519.SH \
  --tags 核心持仓,白酒 \
  --note "逢低关注" \
  --display-name "贵州茅台"

# Batch add from a universe file (one canonical CODE.EXCHANGE per line, # comments ok).
doyoutrade-cli watchlist add --universe-file /tmp/u.txt --tags 观察池
```

| Flag | Required | Notes |
| --- | --- | --- |
| `<symbol>` / `--universe-file` | one required | Single canonical symbol, or a file of them. **Resolve via `doyoutrade-cli stock lookup` first** — the watchlist does not invent suffixes. |
| `--tags` | no | Comma list (or JSON array string) of group labels. |
| `--note` | no | Free-text note. |
| `--display-name` | no | Human-readable label; defaults to the catalog name. |
| `--sort-order` | no | Integer ordering hint (default 0). |

Adding a symbol that is already in the watchlist returns
`duplicate_watchlist_symbol` (the pool is unique by `symbol`) — read the
existing entry with `watchlist list` / `get` and `update` it instead of
re-adding.

### `doyoutrade-cli watchlist update <wl-id> [--tags ...] [--note ...] [--display-name ...] [--sort-order N]`

Patch semantics: only supplied flags are written; omit a flag to leave
its current value untouched. `--tags` **replaces** the entry's tag list
(it is not merged) — pass the full intended set.

```bash
doyoutrade-cli watchlist update wl-3f1c2a9b8e7d --tags 核心持仓,白酒,长持
doyoutrade-cli watchlist update wl-3f1c2a9b8e7d --note "已建仓 1/3"
```

A `wl-…` that doesn't exist returns `watchlist_not_found`; a `sd-…` /
`task-…` shape returns `wrong_identifier_type`.

### `doyoutrade-cli watchlist remove <wl-id>`

```bash
doyoutrade-cli watchlist remove wl-3f1c2a9b8e7d
```

Confirmation-style: `data` carries the removed `id`. Use
`watchlist get` to verify it's gone (expect exit 3 `watchlist_not_found`).

### `doyoutrade-cli watchlist tags`

```bash
doyoutrade-cli watchlist tags
```

`data.tags[]` — distinct tags with a `count` of how many entries carry
each. Use this to discover the group labels before `list --tag` or before
referencing `@watchlist:<tag>` in a task universe.

### `doyoutrade-cli watchlist quotes [--tag <tag> | --symbols A.SH,B.SZ | --universe-file <path>]`

```bash
# All watchlist symbols (no selector).
doyoutrade-cli watchlist quotes

# Only one tag's symbols.
doyoutrade-cli watchlist quotes --tag 核心持仓

# Ad-hoc symbols (need not be in the watchlist).
doyoutrade-cli watchlist quotes --symbols 600519.SH,000001.SZ
```

One-shot live snapshot via **qmt-proxy only**. `data.items[]` carries
per-symbol `last_price` (股价), `change_pct` (涨跌幅), `amount` (成交额,
turnover), plus `prev_close` / `open` / `high` / `low` / `volume` /
`timestamp` / `status`. When qmt is not connected (no default account
with a connection), the values come back `null` (display `—`) and
`status="qmt_disconnected"` — this is expected, not a fetch error. Money
fields are decimal strings.

The selectors are mutually exclusive — passing more than one is rejected
with `validation_error`.

## `@watchlist:<tag>` in a task universe

A task `--universe` accepts watchlist-tag tokens alongside concrete
symbols; they are expanded **eagerly at build time** into the symbols
under that tag (emitting a `watchlist_universe_resolved` observability
event), so "回测我'核心持仓'标签那批" doesn't need manual transcription:

```bash
doyoutrade-cli task create --name "核心池监控" \
  --definition sd-3f1c2a9b8e7d \
  --universe '@watchlist:核心持仓'      # @watchlist:* = every watchlist symbol
```

Mix freely with literal symbols: `--universe '@watchlist:白酒,300750.SZ'`.
A tag that resolves to zero symbols stays visible in the event (not
silently dropped). See `doyoutrade-task` for the full universe contract.

## Reading the watchlist inside strategy code

Strategy `on_bar` / `populate_indicators` can read the watchlist as a
frozen per-cycle snapshot via `ctx.dp.watchlist_symbols(tag=None)` (omit
`tag` for all watchlist symbols). It's a read-only metadata query — no
live DB, deterministic within a cycle. Confirm the method shape with
`doyoutrade-cli sdk dp-methods`; the authoring contract lives in
`strategy-definition-authoring`. (Watchlist membership is metadata, not
bars — to *prefetch* bars still declare `DataRequest.bars`.)

## Reading tool errors

See the main-agent system prompt's "CLI envelope 速读" section for the
general envelope (shape, exit codes, `meta`). Stable `error_code` tokens
for this skill:

| `error_code` | Exit | When | What to do |
| --- | --- | --- | --- |
| `watchlist_not_found` | 3 | No watchlist entry matched the `wl-…` id (`get` / `update` / `remove`). | Run `doyoutrade-cli watchlist list` to discover live ids; don't retry the same id. |
| `duplicate_watchlist_symbol` | 1 | `watchlist add` with a symbol already in the pool (unique by `symbol`). | `watchlist list` to find the existing `wl-…`, then `watchlist update` it instead of re-adding. |
| `wrong_identifier_type` | 2 | A `wl-…` slot got a `sd-…` / `task-…` shape (or vice versa). | Read `error.expected_kind` / `error.actual_kind`; pass the right id (`watchlist list/get` for `wl-…`). |
| `validation_error` | 2 | Bad input — non-canonical symbol, conflicting `quotes` selectors, or a malformed flag. | Read `error.message`; resolve symbols via `stock lookup`, pass exactly one `quotes` selector. |
| `invalid_tags_json` | 2 | `--tags` was neither a comma list nor a parseable JSON array. | Pass `a,b,c` or a valid JSON array string. |

## What this skill does *not* cover

- Symbol resolution (Chinese name → CODE.EXCHANGE) — use `doyoutrade-stock`.
- OHLCV / indicators / news / research reports / earnings / screening on a watched symbol — use
  `doyoutrade-data`.
- Referencing tags from a task universe / the universe contract — see
  `doyoutrade-task`.
- Calling `ctx.dp.watchlist_symbols(...)` from strategy code — see
  `strategy-definition-authoring`.
- Accounts / qmt connection setup (the quotes source) — see
  `doyoutrade-account`.
