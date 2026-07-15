---
name: doyoutrade-stock
description: Resolve Chinese stock names / partials / 6-digit codes to canonical Doyoutrade symbols (`CODE.EXCHANGE`) via `doyoutrade-cli stock lookup`. Use when the user asks "茅台代码是多少 / 这只票的 symbol / 查一下这只股票 / 贵州茅台的代码 / what's the ticker for / resolve this stock /这只票代码多少", or BEFORE writing any stock code into a task / cron / watchlist / strategy universe payload — never pattern-match or guess an exchange suffix yourself. Companion to `doyoutrade-data` (OHLCV / indicators / news / research reports / earnings / `stock screen`), `doyoutrade-watchlist` (curate resolved symbols), and `doyoutrade-task` (universe contract).
category: tool
style: process
---

<!-- Routing:
- All lookup goes through `execute_bash doyoutrade-cli stock lookup ...`;
  reads reach the API server's `/instrument-universe/search` endpoint.
- OHLCV / indicators / news / multi-symbol screening on a resolved symbol
  → `doyoutrade-data` (it owns `stock screen`).
- Add a resolved symbol to the watchlist → `doyoutrade-watchlist`.
- Reference symbols from a task universe → `doyoutrade-task`.
- Read a symbol inside strategy code (`ctx.dp.get_bars`) → `strategy-definition-authoring`.
-->

# doyoutrade-stock

## When to use

Trigger whenever a stock needs to be turned into a canonical symbol
**before** it is used anywhere else — a task `--universe`, a cron payload,
a watchlist add, a strategy DataRequest, or a `data run`. The canonical
form is `CODE.EXCHANGE`:

| Suffix | Exchange | Code shape | Example |
|---|---|---|---|
| `.SH` | Shanghai (沪市 / 上交所) | 6xxxxx (60/68 main, 68 STAR) | `600519.SH` (贵州茅台), `688981.SH` (中芯) |
| `.SZ` | Shenzhen (深市 / 深交所) | 0xxxxx / 3xxxxx (30 ChiNext) | `000001.SZ` (平安银行), `300750.SZ` (宁德) |
| `.BJ` | Beijing (京市 / 北交所) | 4/8开头 | `430047.BJ`, `830799.BJ` |

**Do not guess suffixes.** `600519.SS`, `600519.SHG`, `000001.SZSE` are all
plausible-looking but wrong — the runtime only accepts `CODE.EXCHANGE` with
the `.SH` / `.SZ` / `.BJ` suffixes above. Resolve via `stock lookup` first.

Typical user utterances:

- "茅台代码是多少 / 贵州茅台的 symbol" → `stock lookup 茅台`
- "查一下中天科技" → `stock lookup 中天科技`
- "600519 是什么" → `stock lookup 600519`
- "我要把平安银行加到任务里" → `stock lookup 平安银行` first, then `task ...`

## Commands

### `doyoutrade-cli stock lookup <query> [--limit N] [--source local_catalog|akshare_a]`

```bash
# By Chinese name (most common).
doyoutrade-cli stock lookup 茅台
doyoutrade-cli stock lookup 中天科技 --limit 5

# By 6-digit numeric code.
doyoutrade-cli stock lookup 600519

# By an already-canonical symbol (verifies + returns metadata).
doyoutrade-cli stock lookup 300750.SZ

# Fallback to the live akshare A-share listing (沪/深/京) when the local
# catalog doesn't have it (e.g. a freshly-listed name).
doyoutrade-cli stock lookup 中天科技 --source akshare_a
```

`<query>` accepts a Chinese name, partial name, 6-digit numeric code, or a
canonical `CODE.EXCHANGE` symbol. `data.items[]` — each entry carries
`symbol` (canonical `CODE.EXCHANGE`), `name` (Chinese listing name), and
`market`. Pick the exact `symbol` from the result and use it verbatim
downstream.

| Flag | Required | Notes |
| --- | --- | --- |
| `<query>` | yes | Name / partial / 6-digit code / canonical symbol. |
| `--limit` | no | Max matches (1-50, default 20). |
| `--source` | no | `local_catalog` (default — locally-synced `instrument_catalog` table, zero network round-trip) or `akshare_a` (live akshare A-share listing 沪/深/京). Same result shape either way. |

### When to use which source

- **`local_catalog` (default)** reads the locally-synced catalog table of
  what is tradable in this instance — no network, no akshare progress bar.
  Prefer it for the common case.
- **`akshare_a`** queries the live A-share listings (沪/深/京). Use it as a
  fallback when `local_catalog` returns nothing for a name you can see on a
  quote app (a freshly-listed or recently-renamed name), or to refresh a
  stale local catalog. Both sources return the same `items[]` shape.

### Stock screening is not here

Multi-symbol technical screening (`stock screen --universe-file ... --rsi-max
... --patterns ...`) lives in `doyoutrade-data` — it owns the full
condition-flag vocabulary, code-screen (`--scorer-file` / `--by-strategy`),
and the screener output contract. This skill only resolves a single name →
canonical symbol.

## Reading tool errors

See the main-agent system prompt's "CLI envelope 速读" section for the
general envelope (shape, exit codes, `meta`). Stable `error_code` tokens
for this skill:

| `error_code` | Exit | When | What to do |
| --- | --- | --- | --- |
| `missing_query` | 2 | `<query>` was empty / whitespace. | Pass a name, partial name, or 6-digit code. |
| `unknown_source` | 2 | `--source` is not a registered listing source. | Use `local_catalog` or `akshare_a`. |
| `catalog_unavailable` | 1 | `--source local_catalog` but no instrument catalog is wired for this caller. | Retry with `--source akshare_a`. |
| `lookup_failed` | 1 | Upstream listing source errored (akshare transport / parse). | Read `error.message`; retry, or fall back to the other source. |
| `validation_error` | 2 | Bad input shape (non-string query, out-of-range limit). | Read `error.message`; pass `--limit` in 1-50. |
| `unknown_arguments` | 2 | An unrecognised flag / typo was passed. | Read `error.suggested_path`; only `q` / `limit` / `source` are accepted. |

A query that matches nothing is **not** an error — `data.items` is an empty
list and the envelope text says "no matches". Try a different keyword (a
shorter partial, the 6-digit code, or `--source akshare_a`) before assuming
the stock doesn't exist.

## What this skill does *not* cover

- OHLCV / indicators / news / research reports / earnings / multi-symbol screening — `doyoutrade-data`.
- Curating resolved symbols (tags, notes, live quotes) — `doyoutrade-watchlist`.
- Referencing symbols from a task universe (`@watchlist:<tag>`, the universe
  file contract) — `doyoutrade-task`.
- Reading bars inside strategy code (`ctx.dp.get_bars`) — `strategy-definition-authoring`.
