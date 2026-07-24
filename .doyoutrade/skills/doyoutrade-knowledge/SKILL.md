---
name: doyoutrade-knowledge
description: Read and, only when explicitly asked, write the user's private trading knowledge base at `~/.doyoutrade/knowledge`. Use for symbol history or role notes, sentiment cycles, themes and leaders, the user's holdings or trade history, and review/复盘 journals. This is long-term private memory, not strategy code or market data. Resolve symbols with `doyoutrade-stock`; use `doyoutrade-data` for OHLCV, indicators, news, brokerage research reports (券商研报), or earnings (业绩预告/业绩快报). Writes require an explicit user request.
category: reference
style: process
license: proprietary
---

<!-- Routing:
- Resolve a canonical symbol before looking it up here → doyoutrade-stock.
- Pull OHLCV / indicators / news / research reports / earnings for a symbol → doyoutrade-data.
- This skill is about the *private memory layer*, not market data and not
  strategy source code.
-->

# doyoutrade-knowledge

The user's private knowledge base lives at **`~/.doyoutrade/knowledge/`**. It is
long-term trading memory that does **not** belong in strategy code: which
symbols lead a sentiment cycle, what role a name plays, the user's own fills,
and review journals. It never enters git, backtest reports, session exports, or
any outward-facing artifact.

## When to use

Load this skill (and then read the base) when the conversation touches any of:

- a **specific symbol's** prior history, role, or your past read on it;
- **sentiment cycles / themes / leaders** ("当前情绪周期", "AI 算力这波龙头是谁");
- the user's **own holdings or trade history** ("我上次怎么操作的", "我现在拿着什么");
- **review / 复盘** notes for a day, week, or event;
- the user explicitly asking to **record something** into the knowledge base.

Pure CLI resource tasks (run a backtest, list strategies, fetch OHLCV) do **not**
need this skill.

## Layout — six partitions

```
~/.doyoutrade/knowledge/
├── cycles/     情绪周期 / 题材 / 龙头标的笔记（散文式 .md），按月归档；
│               另含每日机读情绪日志 cycles/<YYYY-MM>/_sentiment.jsonl
├── symbols/    标的角色标签（龙头 / 补涨 / 龙二 / 事件型）+ 策略匹配建议：
│               散文式 roles.md（叙事索引）+ 机读结构化角色卡 roles.jsonl
├── trades/     个人交割单（券商导出的 .csv / .xlsx，原样保留）
├── journal/    复盘日记 / 操作记录，按日期归档
├── backtests/  策略回测的结构化结果与分析（逐只区间/收益/统计 + 机器可读 .csv）
└── playbook/   打板模式库 / 战法总结：哪种打法在什么情绪阶段有效、
                最赚钱模式 vs 要规避的错误（散文式 .md）
```

### Naming conventions (soft — documented here, not code-enforced)

| Partition | Path pattern | Notes |
|---|---|---|
| `symbols/` | `symbols/roles.md` | Central **prose** index: role → symbol → recommended strategy. **Check before any new-symbol backtest or strategy match.** |
| `symbols/` | `symbols/roles.jsonl` | **Machine-readable** 角色卡 sidecar coexisting with `roles.md` (one JSON row per role write: `symbol` / `name` / `role` / `note` / `strategy_hint` / `updated_at`). Append-only; the read side de-dups by `symbol` **last-wins**, so appending a fresh line *is* an update (no need to rewrite/delete). The frontend reads it via `GET /knowledge/symbol-roles`. See the "个股角色卡" writing recipe below. |
| `cycles/` | `cycles/<YYYY-MM>/<symbol-or-theme>.md` | `_overview.md` per month (leading `_` sorts first) holds the month's dominant cycle / sentiment / theme rotation. Per-stock: `cycles/2026-05/600519.SH.md`. Per-theme: `cycles/2026-05/AI-infra.md`. **`_sentiment.jsonl`** per month is a machine-readable daily 情绪 log (one JSON row per trading day: `date` / `label` / `limit_up_count` / `limit_down_count` / `broken_board_count` / `broken_board_rate` / `max_streak`) auto-appended by the daily review; the frontend reads it via `GET /knowledge/sentiment-timeline`. Do not hand-edit it. **`_strong_timeline.csv`**（规范名；遗留名 `强势股时间线.csv` 亦可）是强势股主升波次表，由 `graph-sync` 投影进图谱（角色/题材/活跃周期）；启动/高点/退潮细节在边 attrs + `source_ref`，不要对整表逐事件 `propose`。 |
| `trades/` | `trades/<broker>/<YYYY-MM>.csv` or `trades/<YYYY-MM>.csv` | Keep the broker's raw export — **do not hand-edit columns**; parse on read. The 归因看板 reads these via `GET /knowledge/trade-attribution`: columns are normalised across broker formats (华泰/国君/银河/东财/中信 …), then FIFO-paired into round-trips (build-to-flat) for realised P&L / 胜率 / profit factor / 持仓天数 / 最赚亏回合. See "交割单归因" below. |
| `journal/` | `journal/<YYYY>/<YYYY-MM-DD>.md` | Weekly: `journal/<YYYY>/<YYYY-Www>.md`. Event: `journal/<YYYY>/events/<YYYY-MM-DD>-<slug>.md`. |
| `backtests/` | `backtests/<策略slug>-<definition_id>/<version>-<sample>.md` (+ `.csv`) | Structured per-symbol results / stats / methodology. Narrative belongs in `journal/`, not here. |
| `playbook/` | `playbook/<战法slug>.md` | 打板模式库 / 战法总结: which pattern works in which sentiment phase, the most-profitable setups vs the mistakes to avoid. YAML front-matter `pattern`（打法名）/ `stage`（适用情绪阶段：冰点 / 退潮 / 发酵 / 高潮 / 分歧 / 全周期）/ `tags`（数组）/ `summary`（一句话）; body 写"适用阶段 / 信号 / 最赚钱模式 / 要规避的错误". Cross-reference the sentiment phase names used in `cycles/` overviews so a playbook entry ties back to a real cycle. The frontend reads it via `GET /knowledge/playbook`. See the "打板模式库" writing recipe below. |

Cross-month symbols still tracked: copy into the new month and continue; the old
file stays as a historical snapshot (no symlink needed).

## Reading contract — two-step retrieval

The base is read **opportunistically** — it is not loaded automatically. It grows
over time (cycles by month, trades by month, journal by day — often 100+ files),
so **do not** discover it with `list_files` + blind per-file `read_file`; that
burns tokens and misses the file you need. Use **two-step retrieval** instead:

### Step 1 — `knowledge_index` (the navigation map)

Call the in-process `knowledge_index` tool first. It returns a compact,
always-fresh index: every partition, grouped by month / year / strategy, with a
**one-line title per file** (extracted from the first heading or YAML `summary:`
front-matter — no content dump). Reason over that map to locate the specific file
you need. Optional `partition` scopes to one of `cycles` / `symbols` / `trades` /
`journal` / `playbook` / `backtests`.

```
knowledge_index()                          # whole-base map
knowledge_index(partition="cycles")        # just cycles, by month
```

The map marks each `_overview.md` with ⭐ — those are the partition / group entry
points the steps below prefer.

### Step 2 — `read_file` the one file you located

Only now read the full content of the specific file the map pointed you to:

```
read_file(file_path="<KB>/cycles/2026-05/_overview.md")
```

`<KB>` is the **absolute** knowledge-base root injected as `# knowledgeBase` in
the `<system-reminder>` — use it verbatim. The file tools require an absolute
path and do **not** expand `~`, so never guess `/root` / `/home`. (If for some
reason the reminder is absent, `echo ~/.doyoutrade/knowledge` via `execute_bash`
resolves it; honour `DOYOUTRADE_HOME` if set.)

### Retrieval priorities (after you have the map)

1. **Prefer the partition overview first**: `cycles/<month>/_overview.md` (⭐),
   and the most recent `journal/` entry, before drilling into a single name.
2. **Always consult the symbol's role** before recommending a strategy for a
   symbol or kicking off a fresh backtest on a new name — prefer the
   **structured** `symbols/roles.jsonl` for that symbol's *current* role card
   (latest last-wins row), then the prose `symbols/roles.md` for narrative.
3. **Trades on demand only**: do not pull `trades/*.csv` unless the user
   explicitly wants their trade history analysed (CSVs can be large). `.xlsx`
   exports come back as extracted text via `read_file`.

> When `knowledge_index` is unavailable for some reason, fall back to
> `list_files(directory="<KB>/<partition>")` then `read_file` — but the index is
> the default and cheaper path.

### Step 0（可选加速）— `knowledge_graph`（实体关系图谱）

When the question is **relational** — "这只票什么来头 / 我做过它几次、各赚亏多少 /
那轮周期我怎么操作的 / 龙头都当过谁" — query the graph **before** the two-step
file retrieval. It returns compact facts (with time windows + provenance +
`source_ref`) instead of prose, then you drill into the referenced file via
`knowledge_index` + `read_file`:

```
knowledge_graph(entity="300059")                        # 代码 / 名称 / 角色词 / YYYY-MM / 信号 id
knowledge_graph(entity="龙头", hops=2)                   # 2 跳：谁当过龙头 + 它们的交易
knowledge_graph(entity="2026-03", include_expired=true) # 周期月视角 + 已失效历史认知
knowledge_graph(                                        # Agent 只能提案，不能直接写图
  action="propose",
  summary="补充东方财富题材关系",
  operations=[{
    "op": "create_relation",
    "source": {"type": "symbol", "name": "300059"},
    "relation": "belongs_to_theme",
    "target": {"type": "theme", "name": "券商"},
    "fact": "东方财富属于券商题材。"
  }]
)
```

**`create_relation` 契约（硬约束，勿臆造字段/关系名）**：

- `source` / `target` 必须是 `_EntityRef` 对象：`{"type":"symbol","name":"300059"}`，
  **不是**裸字符串，也没有 `from_entity` / `to_entity` / `relation_type`。
- 受保护 `relation` 仅：`belongs_to_theme` / `has_role` / `leads_theme` /
  `linked_with` / `observed_in` / `signals` / `traded_in` / `uses_playbook`
  （或已批准的 `custom.*`）。**禁止**臆造 `lifecycle_event` /
  `active_in_cycle` / `thematic_role` / `role` 等。
- 可选：`confidence`（0..1）、`valid_at` / `invalid_at`（ISO datetime）、`attrs`。
  顶层没有 `valid_from` / `valid_to` / `source_ref`。
- 启动日 / 高点日 / 退潮日这类**时间线事件不是图谱 relation**——写进
  `cycles/_strong_timeline.csv`（或遗留 `cycles/强势股时间线.csv`），由
  `graph-sync` 投影为 `has_role` + `belongs_to_theme` + `traded_in`（活跃周期），
  事件细节进边 `attrs` + `source_ref` 回 CSV 行。**不要**对整份 CSV 逐事件
  `propose`。

Facts are **bi-temporal**: a superseded judgment (角色从 龙头 变 杂毛) is kept as
an *expired* edge, so `include_expired=true` answers "我当时怎么看" without
polluting the current view. Facts carry a `provenance` grade — `deterministic`
edges are hard-data projections of `roles.jsonl` / `_sentiment.jsonl` /
`trades/` attribution / `cycles/_strong_timeline.csv`（或 `强势股时间线.csv`）/
`decision_signals`; `llm` edges (题材归属 / 龙头判断 /
战法使用 / 个股联动, with a `confidence`) are extracted automatically from each
day's 复盘 journal by the `daily_review` cron (content-hash watermarked, so an
unchanged journal is never re-extracted). Treat `llm` edges as **观点候选**,
not hard facts — weight them by confidence and verify via their `source_ref`
journal when it matters. `manual` edges are local-user edits or individually
approved Agent proposals. Agent proposals are durable but never mutate graph
facts before approval, and there is no approve-always mode.
The graph never replaces reading the source file for
detail; follow `source_ref` (`kb:...` → the KB file,
`db:decision_signals/<id>` → CLI `decision-signal`).
`entity_not_found` right after new data is written usually just means the
projection is stale — ask the local user to run “同步投影” in the graph UI
or `doyoutrade-cli knowledge graph-sync`. Agent **cannot** `action=sync`.
Do not invoke CLI graph-sync to bypass the Agent approval boundary for
manual/propose edges; deterministic sources (roles / trades / timeline /
signals) are the intended sync path.

### 交割单归因 (trade attribution over `trades/`)

The `trades/` CSVs feed a **归因看板** (attribution dashboard) served at
`GET /knowledge/trade-attribution?months=N` — a read-only, sandboxed surface (no
write). It normalises broker column layouts (华泰/国君/银河/东财/中信 …), then
**FIFO-pairs** each symbol's buys/sells into *round-trips* (a round-trip =
build-to-flat: first buy after flat → the sell that returns the position to
zero) and returns `{summary, round_trips, by_symbol, unparsed}`:

- `summary` — `round_trips` / `win_count` / `loss_count` / `win_rate` /
  `total_realized_pnl` / `avg_win` / `avg_loss` / `profit_factor` (总盈/总亏绝对值)
  / `avg_hold_days` / `best` / `worst` (单回合最大盈/亏) / `open_positions` (未平仓
  symbol 数). Money fields are **decimal strings**; empty book → zero counts +
  `None` (never a fabricated 0% win rate).
- `round_trips` — per-round-trip detail (open/close date, qty, avg buy/sell,
  realised P&L, return %, hold days), `close_date` descending.
- `by_symbol` — per-symbol rollups (round-trip count, realised P&L, win rate).
- `unparsed` — every file / row that could not be paired, with a `reason`:
  `core_columns_unmapped` (header layout unrecognised — no bogus P&L produced),
  `orphan_sell` (卖超买 — no phantom negative position), `non_trade_side`
  (红利/申购/费用 — excluded from pairing), `bad_row_values`, `short_row`,
  `read_failed`. Nothing is silently dropped.

In review / 复盘 you may cite the dashboard's win rate, profit factor, and the
best/worst round-trip patterns. Un-flattened tail positions are **not** counted
as realised P&L (only reflected in `open_positions`). Keep the broker's raw CSV
untouched — parsing happens on read, never by rewriting columns.

## Writing — only when the user explicitly asks

Default is **read-only**. Write only when the user says something like "帮我把 X
记到 knowledge 里" / "记一下今天的复盘" / "更新一下这只票的角色". Do **not**
proactively persist your own analysis.

The knowledge root is a **permanent write sandbox**, so the in-process file
primitives work directly on it — no `cat` / `echo` / `tee`, and no CLI:

(`<KB>` below = the absolute `# knowledgeBase` root from the `<system-reminder>`.)

- **New file / overwrite** → `write_file` (parents auto-created):
  ```
  write_file(
    file_path="<KB>/journal/2026/2026-05-30.md",
    content="# 2026-05-30 复盘\n...",
  )
  ```
- **Surgical edit of an existing file** → `edit_file` (read it first so
  `old_string` is exact and unique):
  ```
  read_file(file_path="<KB>/symbols/roles.md")
  edit_file(
    file_path="<KB>/symbols/roles.md",
    old_string="...",
    new_string="...",
  )
  ```
- **Append to a journal/cycle note**: read it, then `edit_file` to splice new
  text onto the end (no append primitive — use a unique trailing anchor).

Follow the naming patterns above so the base stays grep-able. When you create a
file in a new month directory, mirror the existing `_overview.md` shape.

### 个股角色卡 — 记 / 更新一只票的角色 (`symbols/roles.jsonl`)

When the user explicitly says something like "记一下 X 是龙头 / 更新这只票的角色
/ 把 X 归到中军" — update the **structured** role store `symbols/roles.jsonl`
(the prose `symbols/roles.md` narrative stays a separate free-form note; you may
also add a line there, but the card store is the machine-readable source):

1. **Resolve the symbol first** with `doyoutrade-cli stock lookup` so `symbol` is
   the canonical form (`600519.SH`, not "茅台"). Never guess a code.
2. **Append one JSON line** with the file primitive — no read-modify-write, no
   delete of any old line. The reader de-dups by `symbol` **last-wins**, so an
   append supersedes the previous card for that symbol:
   ```
   read_file(file_path="<KB>/symbols/roles.jsonl")   # if it exists, to append after
   ```
   Then append a single minimal-valid row (one object, no trailing keys):
   ```json
   {"symbol":"600519.SH","name":"贵州茅台","role":"龙头","note":"AI 算力主线核心","strategy_hint":"低吸不追高","updated_at":"2026-07-03"}
   ```
   - `role` is a free string; suggested vocabulary: `龙头 / 龙二 / 中军 / 补涨 / 杂毛 / 事件型`.
   - `updated_at` = today's date (`# currentDate` from the `<system-reminder>`).
   - Omit fields you don't know (or set them `null`) — leave them unknown, do not invent.
   - If the file is new, `write_file` it with just that one line; otherwise
     `edit_file` to splice the new line onto the end (unique trailing anchor).

Still **default read-only**: only write when the user explicitly asks. Do not
proactively persist role guesses.

### 打板模式库 — 记 / 更新一个战法 (`playbook/<战法slug>.md`)

When the user explicitly says something like "把这个打法记进模式库 / 记一下这个
战法 / 总结一下这种打板模式" — write one markdown file per pattern to
`playbook/<战法slug>.md` (flat partition, no month/dir grouping). Give it a YAML
front-matter block **plus** a descriptive `# ` heading, then the body:

```
---
pattern: 首板低吸
stage: 发酵
tags: [打板, 低吸, 龙头]
summary: 情绪发酵期首板龙头次日低吸，回避高位分歧板
---
# 首板低吸战法 — 发酵期龙头

## 适用阶段
情绪发酵 / 上升期；冰点 / 退潮期禁用。

## 信号
...

## 最赚钱模式
...

## 要规避的错误
...
```

- `stage` uses the same sentiment-phase vocabulary as the `cycles/` overviews:
  `冰点 / 退潮 / 发酵 / 高潮 / 分歧 / 全周期` — so a playbook entry ties back to a
  real cycle phase.
- `pattern` / `stage` / `summary` are scalars; `tags` is a YAML list. Omit any
  field you don't have (or leave it out of the block) — do not invent one. Bad
  front-matter is loud-skipped by the reader (the file still shows up, only its
  structured fields fall back to empty), but keep it valid so the 前端 cards
  render.
- Every playbook `.md` **MUST** start with a `# ` heading (or `summary:`
  front-matter) — same index-friendly rule as every other partition. The
  frontend reads structured entries via `GET /knowledge/playbook`
  (`{path, title, summary, pattern, stage, tags, updated_at}`, sorted newest
  `updated_at` first).
- Write with the file primitive: `write_file(file_path="<KB>/playbook/first-board-lowbuy.md", content=...)`.

Still **default read-only**: only write when the user explicitly asks.

### Index-friendly writing — keep the navigation map useful

The `knowledge_index` map shows **one title line per file** (extracted from the
first `# ` heading or YAML `summary:` front-matter). A vague or missing title
degrades the map for every future navigation, so writing is also an act of
**index maintenance**. These constraints are mandatory on every write:

1. **Every `.md` MUST start with a descriptive `# ` heading.** That heading is
   the index title. A heading-less file shows up as a bare filename in the map,
   which makes it unsearchable by reasoning. Never `write_file` a `.md` whose
   first non-empty line is not a `# ` heading.
2. **Headings must be self-describing** — carry the symbol + judgment/status, or
   topic + period. The map is read blind (titles only); a generic title is as
   bad as no title.
   - ❌ Bad: `# target`, `# 笔记`, `# 记录`, `# 临时`, `# 复盘`.
   - ✅ Good: `# 圣阳股份 002580.SZ — 见顶 / 中期下跌确认`,
     `# 2026-05-30 复盘（圣阳见顶 + 中天分化）`,
     `# 2026-05 周期总览`.
3. **Prefer `summary:` front-matter for a tighter one-liner** when the heading
   is long or you want the map to show something crisper than the heading:
   ```
   ---
   summary: 圣阳股份 5/14 长上影见顶，5/18 跌破颈线确认中期下跌
   ---
   # 圣阳股份 002580.SZ
   ```
   The index prefers `summary:` over the heading. Use it for cycle notes and
   journals where the date-stamped heading is generic but the takeaway is sharp.
4. **Place files in the correct group directory** so the index groups them:
   `cycles/<YYYY-MM>/`, `trades/<YYYY-MM>/`, `journal/<YYYY>/`,
   `backtests/<slug>-<sd>/`. A file in the wrong dir lands in "（根目录散文件）"
   and breaks the month/year grouping the map relies on.
5. **Maintain `_overview.md` per group** (the ⭐ entry point the map surfaces
   first). When you create a **new month dir** or a **new strategy dir** in
   `backtests/`, also create / update its `_overview.md` so the index points
   somewhere useful instead of a bare file list.
6. **Cross-reference new symbol notes** in `symbols/roles.md` or the relevant
   cycle `_overview.md`. A standalone per-stock note that nothing points to is
   an island — the map can still find it, but the curated indexes should know
   it exists.

### Index snapshot (`_index.md`) refresh discipline

- **The `knowledge_index` tool is always fresh** — it regenerates on every call,
  so the agent never sees a stale map regardless of what is on disk. **Never
  read `_index.md` directly** to reason about the base; always call
  `knowledge_index`.
- **`_index.md` is a human / grep / frontend snapshot**, non-authoritative for
  the agent. After **bulk** changes only (importing months of trades,
  restructuring partitions, a big 复盘 session), offer to refresh it via
  `execute_bash`:
  ```
  execute_bash("doyoutrade-cli knowledge index --refresh")
  ```
  Do **not** refresh after every single edit — that is write amplification with
  no agent benefit (the tool already sees the new content live).

## Privacy boundary

Knowledge-base content is the user's private memory. **Never** copy it into
backtest reports, strategy `source_code`, git commits / diffs, session exports,
or any outward-facing message beyond directly answering the user in this chat.
Don't push the directory to a remote even if `~/.doyoutrade` is symlinked.

## Reading tool errors

`knowledge_index` (the navigation map):

| `error_code` | Meaning | Fix |
|---|---|---|
| `unknown_arguments` | Kwarg outside `partition`. | Only `partition` is accepted (optional). |
| `unknown_partition` | `partition` not one of the six. | Use `cycles` / `symbols` / `trades` / `journal` / `playbook` / `backtests`. |
| `knowledge_root_missing` | `~/.doyoutrade/knowledge` does not exist (fresh env). | Not a hard error — the tool returns guidance. Create partition dirs with `write_file` as needed. |
| `index_build_failed` | Could not walk the base. | Check that `~/.doyoutrade/knowledge` is readable. |

`knowledge_graph` (the entity-relation graph):

| `error_code` | Meaning | Fix |
|---|---|---|
| `unknown_arguments` | Kwarg outside `action` / `entity` / `hops` / `include_expired` / `operations` / `summary`. | Stick to the declared schema. |
| `validation_error` | Bad value (e.g. `hops` outside 1..3, unknown `action`). | Fix the value; `hops` is 1-3. |
| `missing_entity` | `action="query"` without a non-empty `entity`. | Pass 代码 / 名称 / 角色词 / `YYYY-MM` / 信号 id. |
| `entity_not_found` | No graph node matches. | Ask the local user to sync from the graph UI, then retry with the canonical symbol or full name. |
| `graph_schema_validation_error` | Proposed relation violates the protected Schema. | 读 Hint：用 `_EntityRef` 对象作 source/target；只用受保护 relation 名；批量时间线改走 CSV + graph-sync。 |
| `knowledge_graph_unwired` | Runtime has no graph repository wired. | Not fixable in-session; use the two-step file retrieval instead. |
| `knowledge_graph_failed` | Underlying read/write failed (type + message included). | Read the message; typically DB / KB filesystem issues. |

`write_file` / `edit_file` share the file-primitive error codes:

| `error_code` | Meaning | Fix |
|---|---|---|
| `path_outside_workspace` | Path is not under `~/.doyoutrade/knowledge` (nor an open authoring work_dir). | Use an absolute path rooted at the knowledge dir; resolve `$HOME` if unsure. |
| `file_not_found` | `edit_file` / `read_file` target doesn't exist. | `knowledge_index` / `list_files` first; `write_file` to create it instead. |
| `old_string_not_found` / `old_string_not_unique` | `edit_file` anchor missing or ambiguous. | Re-`read_file` and copy an exact, unique snippet (add surrounding context). |
| `io_error` | OS-level failure. | Check the path / permissions and retry. |

## Visualizing the graph in chat

To **draw** a subgraph for the user (not just narrate it), call the in-process
`render_panel` tool with a `kgraph` block — reference-style
`{"type":"kgraph","entity":"贵州茅台","hops":2}` (the frontend fetches the same
`GET /knowledge/graph` neighborhood and renders it as an SVG graph), or inline
`{"type":"kgraph","nodes":[{id,name,node_type?}],"edges":[{id,src_id,dst_id,relation?}]}`.
Full `render_panel` contract + `error_code`s live in `doyoutrade-data`.

## Notes

- The directory ships with a `README.md` describing this same contract — read it
  if you need the canonical source.
- Reads (`read_file` / `list_files`) are unsandboxed and also reachable via
  `execute_bash` (`ls` / `grep` / `find`) for discovery across partitions.
