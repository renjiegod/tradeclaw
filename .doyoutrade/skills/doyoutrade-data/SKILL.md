---
name: doyoutrade-data
description: Fetch and inspect market data with `doyoutrade-cli data`, `analysis`, and `stock screen`. Use when the user asks to 拉日线/K线, get OHLCV bars, fetch per-symbol stock news, fetch brokerage research reports (券商个股研报/分析师评级/EPS·PE 盈利预测), fetch earnings preannouncements or express reports (业绩预告/业绩快报), compute indicators such as RSI/MACD/KDJ/CCI/Bollinger/SuperTrend/limit_up_approx/limit_down_approx, detect candlestick or trend patterns, run factor IC/IR/quantile analysis, or screen symbols for oversold RSI, MA crosses, breakouts, volume spikes, approximate limit-up/limit-down days, and pattern matches. Prefer `doyoutrade-stock` for symbol lookup first; use `strategy-definition-authoring` when writing strategy code that consumes these data.
category: tool
style: process
---

<!-- Routing:
- Look up a stock symbol before fetching OHLCV → `doyoutrade-stock`.
- Use the cached OHLCV inside a strategy → write data_requests in
  `strategy-definition-authoring`.
-->

# doyoutrade-data

## When to use

Ad-hoc market / research probes outside the strategy runtime — OHLCV, indicators,
`stock screen`, news, brokerage reports (`data reports`), earnings
(`data earnings`). Inside a running strategy use `DataProvider.get_data(...)`
(`doyoutrade-sdk dp-methods`). Prefer `doyoutrade-stock` for symbol lookup first.

## Quick checklist

Read this before any `data` / `analysis` / `stock screen` call:

1. **`stock lookup` first** — never invent `.SH` / `.SZ` / `.BJ`.
2. **Symbol args** — exactly one of: positional `<code>`, `--symbols A.SH,B.SZ`,
   or `--universe-file`. **There is no `--symbol`** (singular) on `data run`.
3. **Unsure of a flag?** → `doyoutrade-cli schema data.run` (or the subcommand)
   before guessing. Trust `did_you_mean` / `repair_hints` on failure.
4. **Intervals** — `--interval 1d|5m|15m|30m|60m` (default `1d`). Index minute
   bars (e.g. `000001.SH` + `60m`) may need `tushare` / `auto`; some providers
   return `interval_not_supported_for_instrument_type`.
5. **「看 / 画 K 线」** → after lookup, call in-process `render_panel` with a
   `kline` block (`symbol` + optional `interval`/`start`/`end`). Do not dump
   hundreds of CSV rows as the primary answer.
6. **Truncated `read_file` / omitted chars** → only report numbers you actually
   saw; never invent max/min/volume peaks from a truncated artifact. Prefer
   envelope `symbols[i].latest` / `ohlcv_rows`, or `render_panel`.

## Commands

### `doyoutrade-cli data run [<code>|--symbols ...|--universe-file ...] [window] [--indicators ...] [--script|--script-file ...] [--warmup-bars N] [--script-timeout S]`

Fetch OHLCV and compute built-in and/or custom Python indicators across
one or many symbols in a single call. This is the single entry point
for market-data probes — the legacy `data ohlcv` command has been
removed. `data run` covers OHLCV-only fetches (omit `--indicators`),
built-in indicator computation, and AST-sandboxed custom Python factors.

**Symbol-input modes — exactly one of:**

| Flag | Use when |
| --- | --- |
| positional `<code>` | Single symbol probe. |
| `--symbols A.SH,B.SZ` | Short comma list (or JSON array string). |
| `--universe-file path.txt` | One canonical `CODE.EXCHANGE` per line (`#` comments allowed). |

Passing more than one input mode is rejected with
`conflicting_symbol_args`.

**Watchlist note**: the local OHLCV库 these commands read syncs **only
watchlist (自选股) symbols** by default — a symbol not in the watchlist may
have no cached bars. To probe a new symbol, either it's already watched or
add it first (`doyoutrade-cli watchlist add <symbol>` — see
`doyoutrade-watchlist`). You can also pull the symbols under a watchlist tag
to build a `--universe-file` / `--symbols` list
(`doyoutrade-cli watchlist list --tag <tag>` → take the `symbol` column).

**Local-cache speedup for `stock screen`**: `stock screen` now reads the local
`market_bars` warehouse first and only falls back to the network for symbols not
yet synced (zero behaviour change for un-synced symbols — they fetch over the
network exactly as before). So a slow / timeout-prone full-market screen usually
means *the warehouse lacks that data*, not a screen bug. Two ways to warm it:
- **Full market** — an operator sets `market_data.sync_full_market: true` (deployment
  config; default `false`, so existing deployments keep their watchlist-scoped sync
  load). When on, the background sync covers the whole A-share daily catalog.
- **One symbol / range** — `doyoutrade-cli data sync <code> --start … --end …` (see
  below). Use this for targeted warming; do **not** loop it over thousands of symbols —
  that's what `sync_full_market` is for.

```bash
# Single symbol — fetch + built-ins with auto warm-up.
doyoutrade-cli data run 600519.SH \
  --start 2026-04-01 --end 2026-05-23 \
  --indicators rsi,macd,limit_up_approx \
  --indicator-params '{"rsi":{"period":21}}' \
  --tail 5

# Multi-symbol via short list.
doyoutrade-cli data run --symbols 600519.SH,000001.SZ \
  --period 6m --indicators rsi

# Multi-symbol via universe file with a custom script.
doyoutrade-cli data run --universe-file /tmp/u.txt --period 6m \
  --script-file ./factor.py \
  --script-params '{"window":20}'

# Inline script — REQUIRED_HISTORY auto-sizes warm-up.
doyoutrade-cli data run 600519.SH --period 3m \
  --script 'REQUIRED_HISTORY = 20
result = {"close_to_sma20": target_df["close"] / df["close"].rolling(20).mean().reindex(target_df.index)}'
```

| Flag | Default | Notes |
| --- | --- | --- |
| `<code>` / `--symbols` / `--universe-file` | one required | Mutually exclusive. Resolve canonical symbols via `stock lookup` first. |
| `--period` | none | Relative requested output window. Mutually exclusive with `--start` / `--end`. |
| `--start` / `--range-start` | — | Requested output start. Provider fetch may start earlier for warm-up. |
| `--end` / `--range-end` | — | Requested output end. |
| `--indicators` | **none** (data_run is opt-in) | Comma list, JSON array, or `all`. Built-ins auto-size warm-up if `--warmup-bars` is omitted. |
| `--indicator-params` | `{}` | JSON object keyed by indicator name. Bad JSON → `invalid_indicator_params_json`. |
| `--script` | — | Inline Python; AST-sandboxed. Must define `compute(df, target_df, params)` (exactly that signature) or assign a `result` global. Mutually exclusive with `--script-file`. |
| `--script-file` | — | Local `.py` file with the same contract as `--script`. |
| `--script-params` | `{}` | JSON object passed to `compute(..., params)`. |
| `--script-timeout` | `10` (seconds) | Per-symbol script execution timeout. Worker thread; orphan threads on timeout are acknowledged but not killed. |
| `--warmup-bars` | auto-sized | Explicit prefetch bars. Auto-sizing takes max(selected built-ins, script `REQUIRED_HISTORY` literal). A pure-script run with neither is rejected with `script_warmup_unspecified`. |
| `--tail` | `1` | Trailing rows returned per indicator column under `symbols[i].latest`. |

Note: unlike `analysis indicators`, `data run` defaults to **no** indicators
when `--indicators` is omitted — useful for OHLCV-only probes and pure
custom-factor pipelines. Pass `--indicators all` to opt in to every
built-in.

**Custom script environment** (AST-validated sandbox):

- Imports restricted to: `numpy`, `pandas`, `decimal`, `math`, `typing`,
  `doyoutrade.strategy_sdk` (+ `__future__`). Disallowed → `script_disallowed_import`.
- Injected names: `df` (fetch window with warm-up), `target_df` (requested
  window), `params` (parsed `--script-params`), `pd`, `np`, `indicators`
  (from `doyoutrade.strategy_sdk.indicators`).
- Optional top-level `REQUIRED_HISTORY = <int literal>` declares the
  script's lookback need so the orchestrator can prepend warm-up bars.
- `compute(df, target_df, params)` signature is enforced — exactly those
  three positional-or-keyword names; anything else → `script_compute_signature_invalid`.
- Return shape: `pandas.Series`, `pandas.DataFrame`, or
  `dict[str, Series|list]`. **Scalars inside dicts are rejected** with
  `script_output_scalar_broadcast` (used to be silently broadcast — common
  "forgot to return a Series" bug).
- `df.shift(-N)` and broad `except Exception: pass` and silent
  `isinstance` fallbacks are rejected at AST validate time.

**Envelope shape:**

```json
{
  "status": "ok",
  "interval": "1d",
  "requested_start": "2026-04-01",
  "requested_end": "2026-05-23",
  "warmup_bars_default": 120,
  "warmup_bars_explicit": false,
  "script_timeout": 10.0,
  "symbols_total": 2,
  "symbols_succeeded": 2,
  "symbols_failed": 0,
  "indicators": ["rsi", "macd"],
  "manifest_path": "~/.doyoutrade/assistant/artifacts/data_run_manifest_<hash>.json",
  "symbols": [
    {
      "code": "600519.SH",
      "status": "ok",
      "data_source": "qmt",
      "requested_start": "2026-04-01",
      "requested_end": "2026-05-23",
      "fetch_start": "2025-12-04",
      "warmup_bars": 120,
      "ohlcv_rows": 38,
      "ohlcv_path": "~/.doyoutrade/assistant/artifacts/ohlcv_600519.SH.csv",
      "indicator_path": "~/.doyoutrade/assistant/artifacts/data_run_indicators_600519.SH.csv",
      "indicator_columns": ["rsi", "macd.macd", "macd.signal", "macd.hist", "custom.my_factor"],
      "latest": {"rsi": [55.2], "custom.my_factor": [1.03]}
    },
    {"code": "000001.SZ", "status": "ok", "...": "..."}
  ],
  "script_source": {
    "kind": "inline",
    "sha256": "a351c9baff68d717",
    "bytes": 124,
    "required_history": 20,
    "persisted_path": "~/.doyoutrade/assistant/artifacts/data_run_script_<sha>.py"
  }
}
```

Always iterate `symbols[]`. Per-symbol failures appear as
`symbols[i].status == "failed"` with an `error_code`; the top-level
envelope stays `is_error: false` so the array is always readable.
`script_source` is metadata-only — never the raw code. The full script
body is persisted to `script_source.persisted_path` for inspection.
`status` at the top level is `"ok"` (all succeeded), `"partial"` (some
failed), or `"failed"` (all failed).

`symbols[i].ohlcv_path` is trimmed to the requested window and remains
compatible with `analysis pattern` and `analysis indicators`. Built-in
indicator columns use dotted names for multi-output indicators
(`macd.hist`, `kdj.k`); custom script columns are prefixed with
`custom.`. Warm-up values are used for computation but not written into
the requested-window OHLCV artifact.

### `doyoutrade-cli data sync <code> --start YYYY-MM-DD --end YYYY-MM-DD [--interval 1d|5m] [--mode fill_gap|force_refresh] [--provider ...] [--adjust ...]`

Warm the local `market_bars` warehouse for **one** symbol's range so a later
`stock screen` (and backtests / live cycles) reads it locally instead of issuing
a network round-trip. `--mode fill_gap` (default) fetches only trading days
missing locally; `--mode force_refresh` re-fetches and overwrites the whole
range. `--provider` / `--adjust` default to `market_data.default_provider` and
that provider's default adjust — leave them unset so the cached rows land on the
key `stock screen` reads (overriding them caches under a different key that the
screen won't hit).

Short ranges run synchronously and return `status: ok` with `fetched_segments`
and `upserted_count`; large ranges run as a background job and return
`status: accepted` with a `job_id`.

```bash
doyoutrade-cli data sync 600519.SH --start 2026-01-01 --end 2026-06-20
doyoutrade-cli data sync 300750.SZ --start 2026-01-01 --end 2026-06-20 --mode force_refresh
```

For **full-market** warming, don't loop this over thousands of symbols — an
operator turns on `market_data.sync_full_market: true` so the background sync
covers the whole A-share daily catalog.

### `doyoutrade-cli data news [<code>|--symbols ...|--universe-file ...] [window] [--data-source akshare] [--limit N]`

Fetch recent news for one or many symbols and persist each symbol's
articles to a local CSV. News is a **separate data shape from OHLCV** —
it has no interval / indicator / warm-up axis, and the output CSV is
**not** a valid input to `analysis pattern` / `analysis indicators`.

**Symbol-input modes — exactly one of** (same as `data run`):

| Flag | Use when |
| --- | --- |
| positional `<code>` | Single symbol. |
| `--symbols A.SH,B.SZ` | Short comma list (or JSON array string). |
| `--universe-file path.txt` | One canonical `CODE.EXCHANGE` per line. |

```bash
# Single symbol — most recent news (defaults: data_source auto→akshare, limit 50, 1y window).
doyoutrade-cli data news 600519.SH

# Filter by publish date + cap to 20 most-recent.
doyoutrade-cli data news 600519.SH --start 2026-04-01 --end 2026-05-29 --limit 20

# Multi-symbol via short list.
doyoutrade-cli data news --symbols 600519.SH,000001.SZ --period 1mo
```

| Flag | Default | Notes |
| --- | --- | --- |
| `<code>` / `--symbols` / `--universe-file` | one required | Mutually exclusive. Resolve canonical symbols via `stock lookup` first. |
| `--period` | none | Relative window, e.g. `7d` / `1mo`. Mutually exclusive with `--start` / `--end`; both omitted → 1y. |
| `--start` / `--end` | — | Inclusive `YYYY-MM-DD`; filters articles by **publish date**. |
| `--data-source` | `auto` | `auto` and `akshare` both resolve to akshare (only news source today). |
| `--limit` | `50` | Max most-recent articles per symbol. `0` returns none. |

**Akshare caveat**: the upstream `stock_news_em` endpoint only returns a
fixed window of *recent* news and has no date parameter — the provider
filters to the requested window client-side. A window with no matching
articles returns `news_empty` for that symbol (not a fetch error). A
persistent upstream failure returns `news_fetch_failed` with `error_type`.

**Envelope shape:**

```json
{
  "status": "ok",
  "requested_start": "2026-04-01",
  "requested_end": "2026-05-29",
  "limit": 20,
  "symbols_total": 1,
  "symbols_succeeded": 1,
  "symbols_failed": 0,
  "manifest_path": "~/.doyoutrade/assistant/artifacts/data_news_manifest.json",
  "symbols": [
    {
      "code": "600519.SH",
      "status": "ok",
      "data_source": "akshare",
      "requested_start": "2026-04-01",
      "requested_end": "2026-05-29",
      "article_count": 8,
      "news_path": "~/.doyoutrade/assistant/artifacts/news_600519.SH.csv",
      "latest": [
        {"publish_time": "2026-05-28 09:30:00", "title": "…", "source": "界面新闻"}
      ]
    }
  ]
}
```

Always iterate `symbols[]`. Per-symbol failures appear as
`symbols[i].status == "failed"` with an `error_code`; the top-level
envelope stays `is_error: false`. The full article set (columns
`publish_time,title,source,url,keyword,content`, most-recent first) lives
in `news_path`; `latest` is just the first 5 rows for a quick preview.

### `doyoutrade-cli data reports [<code>|--symbols ...|--universe-file ...] [window] [--data-source akshare] [--limit N]`

Fetch brokerage research reports (券商个股研报) for one or many symbols and
persist each symbol's reports to a local CSV. Research reports are a
**separate data shape from OHLCV and from news** — they surface analyst
opinion (rating / institution / EPS & PE forecasts by year) rather than
market prices or media articles. The output CSV is **not** a valid input
to `analysis pattern` / `analysis indicators`.

**Symbol-input modes — exactly one of** (same as `data run` / `data news`):

| Flag | Use when |
| --- | --- |
| positional `<code>` | Single symbol. |
| `--symbols A.SH,B.SZ` | Short comma list (or JSON array string). |
| `--universe-file path.txt` | One canonical `CODE.EXCHANGE` per line. |

```bash
# Single symbol — most recent reports (defaults: data_source auto→akshare, limit 50, 1y window).
doyoutrade-cli data reports 600519.SH

# Filter by report date + cap to 20 most-recent.
doyoutrade-cli data reports 600519.SH --start 2026-01-01 --end 2026-06-30 --limit 20

# Multi-symbol via short list.
doyoutrade-cli data reports --symbols 600519.SH,000001.SZ --period 3mo
```

| Flag | Default | Notes |
| --- | --- | --- |
| `<code>` / `--symbols` / `--universe-file` | one required | Mutually exclusive. Resolve canonical symbols via `stock lookup` first. |
| `--period` | none | Relative window, e.g. `7d` / `3mo`. Mutually exclusive with `--start` / `--end`; both omitted → 1y. |
| `--start` / `--end` | — | Inclusive `YYYY-MM-DD`; filters reports by **report date**. |
| `--data-source` | `auto` | `auto` and `akshare` both resolve to akshare (only research source today). |
| `--limit` | `50` | Max most-recent reports per symbol. `0` returns none. |

**Akshare caveat**: the upstream `stock_research_report_em` endpoint
returns *every* report it holds for the symbol (no date parameter) — the
provider filters to the requested window client-side on the report
`日期` column. A window with no matching reports returns
`research_reports_empty` for that symbol (not a fetch error). A
persistent upstream failure returns `research_reports_fetch_failed` with
`error_type`. The forecast year columns (`<year>-盈利预测-收益` /
`<year>-盈利预测-市盈率`) are dynamic — they shift forward over time —
so they are parsed into year-keyed dicts; a year absent upstream is
simply absent from `eps_forecasts` / `pe_forecasts`.

**Envelope shape:**

```json
{
  "status": "ok",
  "requested_start": "2026-01-01",
  "requested_end": "2026-06-30",
  "limit": 20,
  "symbols_total": 1,
  "symbols_succeeded": 1,
  "symbols_failed": 0,
  "manifest_path": "~/.doyoutrade/assistant/artifacts/data_research_reports_manifest.json",
  "symbols": [
    {
      "code": "600519.SH",
      "status": "ok",
      "data_source": "akshare",
      "requested_start": "2026-01-01",
      "requested_end": "2026-06-30",
      "report_count": 12,
      "reports_path": "~/.doyoutrade/assistant/artifacts/research_reports_600519.SH.csv",
      "latest": [
        {"report_date": "2026-05-25", "title": "年报点评：稳健前行", "rating": "买入", "institution": "诚通证券"}
      ]
    }
  ]
}
```

Always iterate `symbols[]`. Per-symbol failures appear as
`symbols[i].status == "failed"` with an `error_code`; the top-level
envelope stays `is_error: false`. The full report set (columns
`report_date,title,rating,institution,industry,recent_report_count,eps_forecasts,pe_forecasts,pdf_url`,
most-recent first) lives in `reports_path`; `eps_forecasts` /
`pe_forecasts` are JSON strings mapping a forecast year to the analyst
consensus EPS / PE. `latest` is just the first 5 rows for a quick preview.

### `doyoutrade-cli data breadth [--date YYYY-MM-DD] [--data-source auto|akshare]`

Fetch the A-share limit-up (涨停) / limit-down (跌停) / broken-board (炸板)
pools for **one trading day** and aggregate a market limit-up panel, a
consecutive-limit ladder (连板梯队), and a rule-based sentiment thermometer
(情绪温度计). This is the core data axis for short-term 打板 traders.

Unlike `data news` / `data reports` / `data earnings` this is a
**market-wide single-day** operation — there is **no per-symbol fan-out,
no `--symbols`, and no time window**. It takes a single optional `--date`
(default today, Asia/Shanghai) plus a `--data-source`.

```bash
# Today (Asia/Shanghai) —涨停面板 + 连板梯队 + 情绪温度计.
doyoutrade-cli data breadth

# A specific trading day.
doyoutrade-cli data breadth --date 2026-07-03
```

| Flag | Default | Notes |
| --- | --- | --- |
| `--date` | today (Asia/Shanghai) | Trading day `YYYY-MM-DD`. Must be a real trading day; a non-trading day / pre-snapshot day returns `market_breadth_empty`. No trading-calendar is built client-side. |
| `--data-source` | `auto` | `auto` and `akshare` both resolve to akshare (only breadth source today). |

**Akshare caveat**: the three upstream pool functions
(`stock_zt_pool_em` / `stock_zt_pool_dtgc_em` / `stock_zt_pool_zbgc_em`)
each require an explicit `date` and only serve the after-hours snapshot,
so a day whose data hasn't updated (or a non-trading day) returns empty
pools → `market_breadth_empty`. `max_streak` is the tallest 连板数 in the
limit-up pool; `ladder` maps each consecutive-limit height to a count;
`broken_board_rate` = 炸板 / (涨停 + 炸板).

**Sentiment is rule-based and non-predictive** — the `label` only
describes the current day's state from explicit thresholds and is **never
a prediction or buy/sell advice**. The raw `inputs` are echoed so you can
judge for yourself. Report the numbers + label + disclaimer; do not turn
it into a forecast.

**Envelope shape:**

```json
{
  "status": "ok",
  "trade_date": "20260703",
  "data_source": "akshare",
  "limit_up_count": 92,
  "limit_down_count": 8,
  "broken_board_count": 13,
  "broken_board_rate": 0.1238,
  "max_streak": 7,
  "ladder": {"1": 60, "2": 20, "3": 8, "7": 1},
  "sentiment": {
    "label": "高潮/亢奋",
    "reason": "涨停 92 家、跌停 8 家、炸板 13 家、最高 7 连板、炸板率 12%",
    "disclaimer": "本标签基于当日涨跌停/连板/炸板的规则描述，是单日快照，非预测、非投资建议；完整情绪周期需结合多日趋势。",
    "inputs": {"limit_up_count": 92, "limit_down_count": 8, "broken_board_count": 13, "max_streak": 7, "broken_board_rate": 0.1238}
  },
  "limit_up_path": "~/.doyoutrade/assistant/artifacts/limit_up_pool_20260703.csv",
  "limit_down_path": "~/.doyoutrade/assistant/artifacts/limit_down_pool_20260703.csv",
  "broken_board_path": "~/.doyoutrade/assistant/artifacts/broken_board_pool_20260703.csv",
  "manifest_path": "~/.doyoutrade/assistant/artifacts/data_market_breadth_manifest_20260703.json",
  "pool_errors": {}
}
```

Each pool CSV columns:
`symbol,code,name,change_pct,latest_price,turnover,circulating_mv,total_mv,turnover_rate,industry,streak,broken_board_count,first_seal_time,last_seal_time`
(`symbol` is the canonical `CODE.EXCHANGE`; `streak` = 连板数, only in the
limit-up pool). When one pool fails but others succeed the run returns
`status: partial` and names the failed pool in `pool_errors` — never a
whole-run failure.

### `doyoutrade-cli data lhb [--symbol CODE.EXCHANGE] [--date YYYY-MM-DD | --start YYYY-MM-DD --end YYYY-MM-DD] [--data-source auto|akshare]`

Fetch the A-share 龙虎榜 (dragon-tiger board) in one of **two modes**, selected
by whether `--symbol` is passed:

* **Market mode** (no `--symbol`): the exchange's daily large-order /
  abnormal-move disclosure list (`stock_lhb_detail_em`) for a single day or a
  date range. Like `data breadth` this is a **market-wide** per-day list,
  **not** a per-symbol series (no `--symbols`, no fan-out). `data.mode` is
  `"market"`.
* **Seat mode** (`--symbol` given): one name's per-营业部 (trading desk)
  买入/卖出 席位明细 for a single day (`stock_lhb_stock_detail_em`). `data.mode`
  is `"seats"`.

```bash
# Market mode — today (Asia/Shanghai), everyone on today's board.
doyoutrade-cli data lhb

# Market mode — a specific trading day / an inclusive range.
doyoutrade-cli data lhb --date 2026-07-03
doyoutrade-cli data lhb --start 2026-06-30 --end 2026-07-03

# Seat mode — one name's 席位/游资 detail on a single day.
doyoutrade-cli data lhb --symbol 600519.SH --date 2026-07-03
```

| Flag | Default | Notes |
| --- | --- | --- |
| `--symbol` | — | Canonical `CODE.EXCHANGE`. Given → **seat mode** for that name on a single `--date`. The `.SH`/`.SZ`/`.BJ` suffix is stripped before the upstream call. |
| `--date` | today (Asia/Shanghai) | Single trading day `YYYY-MM-DD`. Market mode: mutually exclusive with `--start` / `--end`. Seat mode: the only date input (a range → `invalid_date`). |
| `--start` / `--end` | — | Market mode only. Inclusive range `YYYY-MM-DD`; pass **both**. No client-side trading-calendar; a non-trading window returns `lhb_empty`. In seat mode a range is rejected with `invalid_date`. |
| `--data-source` | `auto` | `auto` and `akshare` both resolve to akshare (only 龙虎榜 source today). |

**Akshare caveats**: market mode's `stock_lhb_detail_em` only serves the
after-hours snapshot, so a window whose data hasn't updated (or a non-trading
window) returns `lhb_empty`. Seat mode's `stock_lhb_stock_detail_em` internally
raises when the name did NOT make the board that day — this is caught and
surfaced as the **distinct** `lhb_no_seat_data` (confirm the name actually
上榜), separate from the transport-failure `lhb_fetch_failed`.

**游资席位标签 (hot_money)**: in seat mode each seat's `交易营业部名称` is matched
(by **substring**) against a **static, hand-maintained starter library**
(`doyoutrade/data/hot_money_seats.yaml`, mapping 游资名 → 营业部关键词). A hit sets
`hot_money` to the 游资名 (e.g. `赵老哥` / `章盟主`); no hit leaves it `null`. The
library is **可扩展的非权威起步集** — a `null` label means only "not in our
list", NOT "not a 游资"; extend the YAML to improve coverage (no code change
needed). Seats whose 类型/名称 contains `机构专用` are flagged `is_institution:
true` independently of any 游资 tag.

**Market-mode envelope shape:**

```json
{
  "mode": "market",
  "status": "ok",
  "start_date": "20260703",
  "end_date": "20260703",
  "data_source": "akshare",
  "count": 42,
  "lhb_path": "~/.doyoutrade/assistant/artifacts/lhb_20260703_20260703.csv",
  "manifest_path": "~/.doyoutrade/assistant/artifacts/data_lhb_manifest_20260703_20260703.json",
  "latest": [
    {"symbol": "600519.SH", "code": "600519", "name": "贵州茅台", "on_date": "2026-07-03", "reason": "日涨幅偏离值达7%的证券", "change_pct": 9.98, "net_buy_amount": 123456789.0, "turnover_rate": 3.2, "circulating_mv": 2.2e12}
  ]
}
```

Market-mode CSV columns:
`symbol,code,name,on_date,reason,interpretation,change_pct,close_price,net_buy_amount,buy_amount,sell_amount,turnover_rate,circulating_mv`
(`symbol` is the canonical `CODE.EXCHANGE`; amounts in 元, `change_pct` /
`turnover_rate` in %). `latest` is the first rows for a quick preview.

**Seat-mode envelope shape:**

```json
{
  "mode": "seats",
  "status": "ok",
  "symbol": "600519.SH",
  "date": "20260703",
  "data_source": "akshare",
  "buy_count": 5,
  "sell_count": 5,
  "buy_seats": [
    {"seat_name": "华鑫证券有限责任公司上海分公司", "seat_type": "买一", "hot_money": "赵老哥", "is_institution": false, "buy_amount": 50000000.0, "sell_amount": 0.0, "net_amount": 50000000.0, "buy_pct": 3.1, "sell_pct": 0.0}
  ],
  "sell_seats": [
    {"seat_name": "机构专用", "seat_type": "机构专用", "hot_money": null, "is_institution": true, "buy_amount": 0.0, "sell_amount": 40000000.0, "net_amount": -40000000.0, "buy_pct": 0.0, "sell_pct": 2.5}
  ],
  "seats_path": "~/.doyoutrade/assistant/artifacts/lhb_seats_600519.SH_20260703.csv",
  "manifest_path": "~/.doyoutrade/assistant/artifacts/data_lhb_seats_manifest_600519.SH_20260703.json"
}
```

Seat-mode CSV columns:
`symbol,date,side,seat_name,seat_type,hot_money,is_institution,buy_amount,sell_amount,net_amount,buy_pct,sell_pct,provider`
(`side` ∈ {`买入`, `卖出`}; amounts in 元, `buy_pct` / `sell_pct` in %;
`hot_money` is the best-effort 游资名 or empty). `buy_seats` / `sell_seats`
carry all seats for each side.

### `doyoutrade-cli data chips --symbol CODE.EXCHANGE [--days N] [--data-source auto|akshare]`

Fetch A-share 筹码分布 (chip distribution / 筹码集中度) for **one symbol**
(`stock_cyq_em`): 获利比例 (profit ratio), 平均成本 (avg cost), and the 90%/70%
cost-band concentration akshare computes from OHLCV + turnover. A-share
individual stocks only — ETFs / indices / non-A-share names return the
distinct `chip_distribution_empty` (never a fabricated snapshot). Defaults to
the single latest trading day; pass `--days` > 1 for a short trend window
(oldest first).

```bash
# Latest day only (default).
doyoutrade-cli data chips --symbol 000636.SZ

# Short trend window — did concentration tighten over the last 2 weeks?
doyoutrade-cli data chips --symbol 000636.SZ --days 10
```

| Flag | Default | Notes |
| --- | --- | --- |
| `--symbol` | — (required) | Canonical `CODE.EXCHANGE`. Empty/blank → `invalid_symbol`. The `.SH`/`.SZ`/`.BJ` suffix is stripped before the upstream call. |
| `--days` | `1` | Most recent N trading days, 1-90. Out of range or non-integer → `invalid_days`. |
| `--data-source` | `auto` | `auto` and `akshare` both resolve to akshare (only 筹码分布 source today). |

**Akshare caveat**: `stock_cyq_em` is an A-share-individual-stock-only signal
— ETFs, indices, and non-A-share names return an empty frame (→
`chip_distribution_empty`), not an error. A persistent upstream failure (all
retries exhausted) surfaces the **distinct** `chip_distribution_fetch_failed`
with `error_type` carrying the exception class.

**py-mini-racer note**: `stock_cyq_em` runs an embedded JS calculation via
`py_mini_racer`. The pinned `py-mini-racer==0.6.0` ships a bundled dylib that
no longer matches its own default (legacy) `MiniRacer` wiring — a packaging
inconsistency, not a platform issue. `chip_distribution_akshare._ensure_working_mini_racer()`
detects and retargets it to the package's working implementation before every
call; this is transparent to callers and only logs once per process the
first time it actually has to repair the wiring.

**Envelope shape:**

```json
{
  "status": "ok",
  "symbol": "000636.SZ",
  "days": 1,
  "data_source": "akshare",
  "count": 1,
  "chips_path": "~/.doyoutrade/assistant/artifacts/chips_000636.SZ.csv",
  "manifest_path": "~/.doyoutrade/assistant/artifacts/data_chips_manifest_000636.SZ.json",
  "latest": [
    {"symbol": "000636.SZ", "date": "2026-07-14", "profit_ratio": 0.61, "avg_cost": 57.3, "cost_90_low": 50.23, "cost_90_high": 66.28, "concentration_90": 0.20, "cost_70_low": 52.12, "cost_70_high": 59.41, "concentration_70": 0.10, "provider": "akshare"}
  ]
}
```

CSV columns:
`symbol,date,profit_ratio,avg_cost,cost_90_low,cost_90_high,concentration_90,cost_70_low,cost_70_high,concentration_70,provider`
(`profit_ratio` / `concentration_90` / `concentration_70` are fractions 0-1;
cost fields are the same unit as price). `latest` carries every returned row
(oldest first) for a quick preview — not just the newest one.

### `doyoutrade-cli data fund-flow [--scope individual|sector] [--period 今日|3日|5日|10日] [--sector-type 行业|概念|地域] [--top N] [--data-source auto|akshare]`

Fetch A-share 资金流排名 (fund-flow ranking) — main / super-large / large /
medium / small net inflow — for individual stocks or for sector boards over a
rolling window. **No date**: the window is the rolling `--period`.

```bash
# Top-30 individual stocks by today's main net inflow.
doyoutrade-cli data fund-flow

# 5-day individual ranking, top 50.
doyoutrade-cli data fund-flow --period 5日 --top 50

# Concept-board fund flow today.
doyoutrade-cli data fund-flow --scope sector --sector-type 概念
```

| Flag | Default | Notes |
| --- | --- | --- |
| `--scope` | `individual` | `individual` = per-stock (`stock_individual_fund_flow_rank`); `sector` = per-board (`stock_sector_fund_flow_rank`). |
| `--period` | `今日` | `individual` allows {今日,3日,5日,10日}; `sector` allows only {今日,5日,10日} (**no 3日**). A period outside the scope's set → `invalid_period`. |
| `--sector-type` | `概念` | `sector` scope only; maps to akshare `行业资金流` / `概念资金流` / `地域资金流`. Ignored for `individual`. |
| `--top` | `30` | Rows returned, ranked by main net inflow (净额) descending (None sorts last). CSV holds the full ranking; `latest` holds the top N. |
| `--data-source` | `auto` | `auto` and `akshare` both resolve to akshare. |

**Akshare caveats**: the individual endpoint's columns are **period-prefixed**
(e.g. `今日主力净流入-净额`), so the provider matches columns by **substring**,
not exact name. The `sector` endpoint's exact columns were not confirmed
online — the provider matches by substring and tolerates missing columns
(missing → `None`). The `今日` endpoint intermittently `RemoteDisconnected`;
the provider retries.

**Envelope shape (individual):**

```json
{
  "status": "ok",
  "scope": "individual",
  "period": "今日",
  "count": 5000,
  "top": 30,
  "fund_flow_path": "~/.doyoutrade/assistant/artifacts/fund_flow_individual_今日.csv",
  "manifest_path": "~/.doyoutrade/assistant/artifacts/data_fund_flow_manifest_individual_今日.json",
  "latest": [
    {"symbol": "600519.SH", "code": "600519", "name": "贵州茅台", "latest_price": 1800.0, "change_pct": 9.98, "main_net_amount": 5.0e8, "main_net_pct": 12.3, "super_large_net_amount": 3.0e8, "large_net_amount": 2.0e8, "medium_net_amount": -1.0e8, "small_net_amount": -1.0e8}
  ]
}
```

For `--scope sector` the envelope also carries `sector_type` and each row has
`name` (board) + `main_net_amount` / `main_net_pct` + `lead_stock` (领涨股, when
supplied) and empty `code` / `symbol`. CSV columns:
`scope,symbol,code,name,latest_price,change_pct,main_net_amount,main_net_pct,super_large_net_amount,large_net_amount,medium_net_amount,small_net_amount,lead_stock`.

### `doyoutrade-cli data sector-heat [--sector-type concept|industry] [--top N] [--data-source auto|akshare]`

Fetch the A-share 题材 / 板块热度榜 (sector-heat ranking) — the whole-board
snapshot the akshare board-name endpoints return (板块 涨跌幅 / 总市值 / 换手率 /
上涨·下跌家数 / 领涨股 + 领涨股涨跌幅) for one board family, ranked by 涨跌幅
descending. **No date** and **no per-symbol fan-out** — this is a market-wide
board snapshot. The 板块涨幅榜 is a first-order read of where the day's 主线
(dominant theme) heat sits.

```bash
# Top-30 concept boards by today's board change.
doyoutrade-cli data sector-heat

# Top-15 industry boards.
doyoutrade-cli data sector-heat --sector-type industry --top 15
```

| Flag | Default | Notes |
| --- | --- | --- |
| `--sector-type` | `concept` | `concept` = 概念板块 (`stock_board_concept_name_em`); `industry` = 行业板块 (`stock_board_industry_name_em`). Heat is per family, never merged. |
| `--top` | `30` | Boards returned, ranked by 涨跌幅 (change_pct) descending (None sorts last). CSV holds the full ranking; `latest` holds the top N. |
| `--data-source` | `auto` | `auto` and `akshare` both resolve to akshare. |

**Akshare caveats**: this reuses the *same* board-name endpoints as
`data sectors` / `data sector-members`, but keeps the heat columns those
membership methods drop. The board列名 follow the documented 东方财富 schema
(`排名,板块名称,板块代码,最新价,涨跌额,涨跌幅,总市值,换手率,上涨家数,下跌家数,领涨股票,领涨股票-涨跌幅`);
columns are matched by name and a missing column becomes `None` on the row
(never coerced to 0). The eastmoney board endpoints are occasionally
rate-limited / `RemoteDisconnected`; the provider retries.

**Envelope shape:**

```json
{
  "status": "ok",
  "sector_type": "concept",
  "count": 350,
  "top": 30,
  "sector_heat_path": "~/.doyoutrade/assistant/artifacts/sector_heat_concept.csv",
  "manifest_path": "~/.doyoutrade/assistant/artifacts/data_sector_heat_manifest_concept.json",
  "latest": [
    {"board_name": "半导体", "board_code": "BK1036", "sector_type": "concept", "change_pct": 2.5, "total_mv": 4.0e12, "turnover_rate": 3.1, "up_count": 80, "down_count": 5, "leader_stock": "中芯国际", "leader_change_pct": 9.98, "provider": "akshare"}
  ]
}
```

CSV columns:
`board_name,board_code,sector_type,change_pct,total_mv,turnover_rate,up_count,down_count,leader_stock,leader_change_pct,provider`.
Empty board list → `sector_heat_empty`; persistent upstream failure →
`sector_heat_fetch_failed` (`error_type` carries the exception class).

### `doyoutrade-cli data earnings [<code>|--symbols ...|--universe-file ...] [window] [--kind forecast|express|both] [--data-source akshare]`

Fetch earnings preannouncements (业绩预告) and/or express reports (业绩快报)
for one or many symbols. Earnings data is a **separate shape and a
separate fetch model** from news / research reports — the upstream
(`stock_yjyg_em` / `stock_yjkb_em`) serves a **full-market snapshot per
fiscal quarter-end** (report period), so this command is **batch /
period-scoped**: it pulls each report period once for the whole market,
then filters to the requested symbols in memory (multi-symbol shares one
fetch per period, never re-pulls the market per symbol).

**Symbol-input modes — exactly one of** (same as `data run` / `data news`):

| Flag | Use when |
| --- | --- |
| positional `<code>` | Single symbol. |
| `--symbols A.SH,B.SZ` | Short comma list (or JSON array string). |
| `--universe-file path.txt` | One canonical `CODE.EXCHANGE` per line. |

**The window selects report periods, not rows.** Every fiscal quarter-end
(03-31 / 06-30 / 09-30 / 12-31) that falls inside `[start, end]` becomes
one `YYYYMMDD` report-period token. The default 1y window therefore covers
the trailing four quarters. `announce_date` is returned as a field but is
NOT used for window filtering (a quarter's preannouncement is filed after
quarter-end).

```bash
# Single symbol — both 业绩预告 + 业绩快报 across the trailing 4 quarters.
doyoutrade-cli data earnings 600519.SH

# Only 业绩预告 (forecast); 业绩快报 would be --kind express.
doyoutrade-cli data earnings 600519.SH --kind forecast --period 1y

# Explicit report-period window (covers 2024 Q1+Q2+Q3+Q4).
doyoutrade-cli data earnings --symbols 600519.SH,000001.SZ --start 2024-01-01 --end 2024-12-31
```

| Flag | Default | Notes |
| --- | --- | --- |
| `<code>` / `--symbols` / `--universe-file` | one required | Mutually exclusive. Resolve canonical symbols via `stock lookup` first. |
| `--period` | `1y` | Relative report-period window. Mutually exclusive with `--start` / `--end`. |
| `--start` / `--end` | — | Inclusive `YYYY-MM-DD`; selects quarter-ends inside the window. |
| `--kind` | `both` | `forecast` (业绩预告) / `express` (业绩快报) / `both`. |
| `--data-source` | `auto` | `auto` and `akshare` both resolve to akshare (only source today). |

**A symbol is `ok` if it has any row across the requested kinds; it is
`earnings_empty` only when every kind × period returned nothing for it.**
A per-period upstream failure does NOT abort the batch — other periods
still resolve, but the failure is recorded (not swallowed) and surfaced.
A window containing no quarter-end (e.g. Jan 1 – Mar 30) returns
`no_report_periods`.

**Envelope shape:**

```json
{
  "status": "ok",
  "kind": "both",
  "requested_start": "2025-06-28",
  "requested_end": "2026-06-28",
  "report_periods": ["20250930", "20251231", "20260331", "20260630"],
  "symbols_total": 1,
  "symbols_succeeded": 1,
  "symbols_failed": 0,
  "manifest_path": "~/.doyoutrade/assistant/artifacts/data_earnings_manifest.json",
  "symbols": [
    {
      "code": "600519.SH",
      "status": "ok",
      "data_source": "akshare",
      "forecast": {
        "count": 4,
        "path": "~/.doyoutrade/assistant/artifacts/earnings_forecast_600519.SH.csv",
        "report_periods": ["20251231", "20250930", "20250630", "20250331"],
        "latest": [
          {"report_period": "20251231", "announce_date": "2026-01-28", "preannounce_type": "预增", "forecast_indicator": "净利润", "change_pct": 15.2}
        ]
      },
      "express": {
        "count": 2,
        "path": "~/.doyoutrade/assistant/artifacts/earnings_express_600519.SH.csv",
        "report_periods": ["20251231", "20250930"],
        "latest": [
          {"report_period": "20251231", "announce_date": "2026-02-28", "eps": 66.0, "net_profit": 8.5e10, "net_profit_prev_yoy": 15.2, "roe": 30.0}
        ]
      }
    }
  ]
}
```

The forecast CSV columns are `report_period,announce_date,preannounce_type,
forecast_indicator,forecast_value,change_pct,prev_year_value,
change_description,reason`. The express CSV columns are `report_period,
announce_date,eps,revenue,revenue_prev_yoy,revenue_qoq,net_profit,
net_profit_prev_yoy,net_profit_qoq,navs_per_share,roe,industry`. Within
each (symbol, kind) file, rows are sorted most-recent report-period first.

### `doyoutrade-cli data sectors [--sector-type industry|concept] [--data-source auto|akshare|qmt] [--limit N]`

List the available board names so you can discover what's screenable.
`--data-source auto` walks an akshare-first → qmt fallback chain (same
multi-source pattern as OHLCV). envelope `data.sectors` holds the names;
`data.sector_count` the total. Use this before `data sector-members` when
unsure of the exact board name.

### `doyoutrade-cli data sector-members <names> [--sector-type industry|concept] [--data-source auto|akshare|qmt] [--limit N] [--output u.csv]`

Fetch one or more boards' constituents and write a **screenable universe
CSV**. `<names>` is comma-separated (e.g. `白酒` or `白酒,半导体`). Each
board's members are written to `sector_<name>.csv` (`code,name`); the
de-duplicated union of all member codes is written to a universe file
(`data.universe_path`, one canonical symbol per line) — feed it straight
to `stock screen --universe-file`:

```bash
doyoutrade-cli data sector-members "白酒,半导体" --output /tmp/u.csv
doyoutrade-cli stock screen --universe-file /tmp/u.csv \
  --ma-above-ma 20,60 --avg-amount-lookback 10 --avg-amount-min 1e9 \
  --rank-by rsi --top-k 20
```

envelope: `data.status` ∈ `{ok, partial, failed}`; `data.universe_size` is
the unique-symbol count; `data.sectors[]` has per-board `status` /
`member_count` / `members_path` (and `error_code` on failure). A board that
resolves but has no constituents fails with `sector_empty`; a provider
error fails with `sector_fetch_failed` — per-board failures don't collapse
the run. `--data-source auto` falls back akshare → qmt and emits a
`sector_provider_fallback` debug event when it does.

### `doyoutrade-cli data fundamentals [<code>|--symbols ...|--universe-file ...] [--data-source auto|akshare|qmt] [--output f.csv]`

Fetch float / total market cap + PE / PB for one or many symbols and write
`fundamentals.csv` (`code,float_mv,total_mv,pe,pb,price`). Market-cap values
are in 元 (100亿 = `1e10`). `--data-source auto` walks akshare (whole-market
snapshot — one call serves a whole universe, and carries PE/PB) → qmt
(float-cap only, `pe`/`pb` null). Symbol input is `code` / `--symbols` /
`--universe-file` (mutually exclusive, same shapes as `data run`).
`data.symbols_matched` / `data.missing` report coverage; a provider error
surfaces `fundamentals_fetch_failed`. This is the standalone "see the
numbers" companion to `stock screen --min-float-mv`, which pulls the same
axis inline.

### `doyoutrade-cli data events [<code>|--symbols ...|--universe-file ...] [--asof YYYY-MM-DD] [--data-source auto|akshare] [--output e.csv]`

Fetch calendar / status events (currently **suspension 停牌** via the akshare
停复牌 snapshot for `--asof`) and write `events.csv`
(`code,event_type,event_date,detail`). `data.symbols_with_events` counts how
many of the requested symbols are halted; `data.event_count` the total rows.
The standalone inspector behind `stock screen --exclude-suspended` — use the
flag to filter, this command to see the detail. (Earnings-disclosure calendar
is a documented follow-up — not yet a callable event type.)

### `doyoutrade-cli analysis pattern <code> [--patterns all] [--window 10]`

Detect candlestick / trend / consolidation patterns in a symbol's cached
OHLCV.

```bash
doyoutrade-cli analysis pattern 600519.SH
doyoutrade-cli analysis pattern 600519.SH --patterns doji,hammer,engulfing --window 20
```

**Prerequisite**: `ohlcv_<code>.csv` must already exist (run `data run`
first). The skill returns `file_not_found` when the cache is missing.

### `doyoutrade-cli analysis indicators <code> [--indicators rsi,macd,kdj|all] [--params '<json>'] [--tail 1]`

Compute technical-indicator **values** on a symbol's cached OHLCV — the
"see the numbers" companion to `stock screen` (which only matches
booleans). Covers the full SDK indicator surface (`sma/ema/rsi/macd/
bollinger/atr/adx/obv/kdj/williams_r/cci/roc/momentum/mfi/trix/vwap/cmf/
ad/volume_ratio/keltner/donchian/stdev/hist_volatility/wma/dema/kama/
supertrend/psar/ichimoku/zigzag`).

```bash
# Latest snapshot of every indicator
doyoutrade-cli analysis indicators 600519.SH

# A subset, with the last 5 bars of each series
doyoutrade-cli analysis indicators 600519.SH --indicators rsi,macd,kdj --tail 5

# Override an indicator's params (per-indicator dict, JSON)
doyoutrade-cli analysis indicators 600519.SH --indicators rsi,cci \
  --params '{"rsi": {"period": 21}, "cci": {"period": 14}}'
```

**Minimal valid payload** (the tool's `execute` args): `{"code": "600519.SH"}`
— `indicators` defaults to `"all"`, `tail` to `1`. `indicators` also
accepts a JSON array or a comma-separated string.

| Flag | Required | Notes |
| --- | --- | --- |
| `code` (positional) | yes | Canonical symbol; resolve via `stock lookup` first. |
| `--indicators` | no | Comma list or `all` (default). Unknown name → `unknown_indicator` with the available list attached. |
| `--params` | no | JSON object keyed by indicator name → param dict. Bad JSON → `invalid_params_json`. |
| `--tail` | no | Number of trailing rows to echo per indicator (default 1 = latest snapshot). |

**Prerequisite**: `ohlcv_<code>.csv` must exist (run `data run` first) —
missing cache returns `ohlcv_csv_missing`. Multi-output indicators expand
to dotted columns (`macd.macd` / `macd.signal` / `macd.hist`, `kdj.k` /
`kdj.d` / `kdj.j`, `bollinger.upper` …). The full series is written to
`~/.doyoutrade/assistant/artifacts/indicators_<code>.csv`; **read
`data.report_path`** for it, and `data` carries the latest value(s) per
indicator. Warm-up bars are `null` (NaN) — gate reads accordingly.

### `doyoutrade-cli analysis factor --factor-csv <path> --return-csv <path> [opts]`

Runs IC / IR / quantile-group analysis on a factor table.

```bash
doyoutrade-cli analysis factor \
  --factor-csv ./momentum.csv \
  --return-csv ./fwd_returns.csv \
  --n-groups 5

# Custom output dir
doyoutrade-cli analysis factor \
  --factor-csv ./factor.csv \
  --return-csv ./returns.csv \
  --output-dir ./factor_outputs/
```

| Flag | Required | Notes |
| --- | --- | --- |
| `--factor-csv` | yes | CSV with `index=date`, `columns=codes`, values = factor at that date. |
| `--return-csv` | yes | CSV same shape, values = forward returns. |
| `--output-dir` | no | Where to drop plots / per-group stats. Default `~/.doyoutrade/assistant/artifacts/factor_output/`. |
| `--n-groups` | no | Number of quantile groups (default `5`). |

The output dir gets per-group return curves, IC time-series, IR
summary, and a top-bottom-spread plot. The envelope's `data` block
echoes summary statistics.

### `doyoutrade-cli stock screen --universe-file <path> [conditions] [--top-k N] [--sort-by ... [--sort-desc]]`

Scan a universe of symbols against a fixed whitelist of technical
conditions and return the symbols that match **all** active conditions
(AND-combined). Each match comes with the relevant computed columns
(close, RSI, pct_change, …); the full result is written as CSV to
`~/.doyoutrade/assistant/artifacts/screener_<asof>_<ts>.csv` and the
envelope's `data.preview` lists the first 10 rows.

**Minimal valid invocation** — universe file + at least one condition:

```bash
echo -e "600000.SH\n600519.SH\n000001.SZ" > /tmp/u.txt
doyoutrade-cli stock screen --universe-file /tmp/u.txt --rsi-max 30
```

**Universe** — only `--universe-file` is supported in v1 (one
`CODE.EXCHANGE` per line; `#` lines are ignored). Resolve symbols via
`doyoutrade-cli stock lookup` **before** writing them into the file —
the screener does not re-validate codes. There is no `--market` /
`--sector` / `--strategy-task` shortcut yet.

**Time anchor** — `--asof YYYY-MM-DD` (default today). Bars after `asof`
are dropped before evaluation, so the run is reproducible on the same
universe + asof.

**Conditions (AND)** — pass any combination of the flags below; the
screener auto-sizes the bar fetch window so every indicator's warmup
is covered. Calling `stock screen` with **no** conditions returns
`no_conditions_specified`.

| Family | Flag | Meaning |
| --- | --- | --- |
| Pattern | `--patterns hammer,bullish_engulfing,bearish_engulfing,doji,head_and_shoulders,double_top,double_bottom,ascending_triangle,descending_triangle,broadening` | Any-of match within `--pattern-window` (default 10) bars before `asof`. |
| Pattern window | `--pattern-window N` | Lookback for detection (default 10). |
| RSI | `--rsi-period N` (default 14), `--rsi-min X`, `--rsi-max X` | Either bound is optional; can also combine `--rsi-min` and `--rsi-max` to require a range. |
| MA cross | `--ma-cross golden:fast,slow` or `death:fast,slow`, `--cross-window N` (default 3) | Match when the cross fired within the last `cross-window` bars. `fast` must be smaller than `slow`. |
| Price vs MA | `--price-above-ma N` or `--price-below-ma N` (mutually exclusive) | Match when `close` is above / below `SMA(N)` at `asof`. |
| Lookback return | `--pct-change-lookback N`, `--pct-change-min X`, `--pct-change-max X` | Match when `(close - close[-1-N]) / close[-1-N]` falls in range. `--pct-change-lookback` is required when either bound is set. |
| Volume ratio | `--volume-ratio-lookback N`, `--volume-ratio-min X` | Match when `today_vol / mean(volume[-1-N:-1]) >= X`. **Both** flags must be set together. |
| New high / low | `--close-at-high-window N` or `--close-at-low-window N` | Match when today's close equals the `N`-bar high / low (≈ breakout / breakdown). |
| Limit-up (approx) | `--limit-up-approx` | Match when `asof` close is at the board limit-up price (10%/20%/30% by code; ST not auto-detected) **and** `close == high`. Same logic as `indicators.limit_up_approx` in strategy code. |
| Limit-down (approx) | `--limit-down-approx` | Match when `asof` close is at the board limit-down price (10%/20%/30% by code; ST not auto-detected) **and** `close == low`. Same logic as `indicators.limit_down_approx` in strategy code. |
| Bollinger | `--bollinger upper_break\|lower_break`, `--bollinger-window N` (default 20) | Match when close pierces the upper / lower band at `asof`. |
| ADX | `--adx-period N` (default 14), `--adx-min X` | Match when `ADX >= X` at `asof` (trend strength). |
| MACD | `--macd golden_cross\|death_cross\|cross_zero_up\|cross_zero_down` | Match when the trigger fired within `--cross-window` bars. Uses 12/26/9 defaults. |
| KDJ | `--kdj golden_cross\|death_cross`, `--kdj-n N` (default 9) | K/D cross within `--cross-window` bars. Columns `kdj_k` / `kdj_d` / `kdj_j`. |
| CCI | `--cci-min X`, `--cci-max X`, `--cci-period N` (default 20) | Latest CCI in range. `min > max` → `conflicting_conditions`. Column `cci`. |
| Williams %R | `--williams-min X`, `--williams-max X`, `--williams-period N` (default 14) | Latest %R in `[-100, 0]`. Column `williams_r`. |
| Keltner | `--keltner upper_break\|lower_break` | Close pierces the EMA20 ± 2·ATR10 band at `asof`. Columns `keltner_upper` / `keltner_lower`. |
| Donchian | `--donchian upper_break\|lower_break`, `--donchian-window N` (default 20) | Close clears the **prior** N-bar high/low channel (shift-by-1 breakout). Columns `donchian_upper` / `donchian_lower`. |
| CMF | `--cmf-min X`, `--cmf-period N` (default 20) | Latest Chaikin Money Flow `>= X` (accumulation). Column `cmf`. |
| ROC | `--roc-min X`, `--roc-max X`, `--roc-period N` (default 12) | Latest rate-of-change (%) in range. Column `roc`. |
| MA above MA | `--ma-above-ma fast,slow` (e.g. `20,60`) | Match when `SMA(fast) > SMA(slow)` at `asof` (shorter MA above longer MA). `fast` period must be `< slow`. Columns `ma{fast}` / `ma{slow}`. |
| MA slope | `--ma-slope-min period,lookback,min_slope` (e.g. `20,5,0`) | Match when SMA(period)'s relative change over `lookback` bars `>= min_slope` (`ma_last / ma_prev - 1`, scale-free; `0` = rising). Column `ma_slope{period}`. |
| Avg turnover | `--avg-amount-lookback N`, `--avg-amount-min X` | Match when mean turnover (`amount` 成交额, in currency) over the last `N` bars `>= X`. **Both** flags together. A symbol whose provider omits `amount` is skipped (`insufficient_history`), never treated as zero turnover. Column `avg_amount`. |
| Float market cap | `--min-float-mv X`, `--max-float-mv X` | Match when float market cap (流通市值, currency; 100亿 = `1e10`) is in range. **Pulls the fundamentals axis** (one akshare snapshot for the whole universe, or qmt per-symbol) — a symbol the source can't serve is skipped with `fundamentals_unavailable` (distinct from `insufficient_history`). Column `float_mv`. |
| Suspension | `--exclude-suspended` | Drop symbols halted (停牌) as of `--asof`. **Pulls the event axis** (akshare 停复牌 snapshot). A provider error is top-level `events_fetch_failed`. Matched survivors carry a `not_suspended` tag in `matched_conditions`. |

**Output / ranking**:

`--sort-by` orders by any already-emitted column. `--rank-by` is for
"pick the strongest N" — it **computes a metric for every matched symbol
even when that metric is not a filter**, then orders by it
(strongest-first) so `--top-k` keeps the top N.

| Flag | Default | Notes |
| --- | --- | --- |
| `--rank-by METRIC` | none | One of `rsi` / `adx` / `cci` / `roc` / `macd_hist` / `avg_amount`. Computes + emits the metric (reusing a filter's value if that condition is also active) and orders by it. `avg_amount` needs `--avg-amount-lookback`. May be used with **no** filter to rank a whole universe. Matched symbols that can't compute the metric (short history) emit a `screener_rank_skipped` debug event and sort to the end. |
| `--rank-order asc\|desc` | `desc` | Ranking direction when `--rank-by` is set (`desc` = strongest first). |
| `--top-k N` | unlimited | Keep at most N matched rows after sort (top-N). |
| `--sort-by COLUMN` | `symbol` (or the rank metric when `--rank-by` is set) | Any output column: `rsi`, `pct_change`, `volume_ratio`, `avg_amount`, `close`, `bar_count`, `ma20`, `ma60`, `ma_slope20`, `adx14`, etc. Missing columns push the row to the end. An explicit `--sort-by` overrides `--rank-by` ordering. |
| `--sort-desc` / `--sort-asc` | asc | Direction (ignored when `--rank-by` drives ordering). |
| `--output PATH` | `~/.doyoutrade/assistant/artifacts/screener_<asof>_<ts>.csv` | Explicit CSV destination. |

**Code-screen mode** (`--scorer-file <path>` or `--by-strategy <sd-id>`): when
the boolean whitelist can't express the factor, write a Strategy SDK scorer
(`class Strategy(Strategy)` with `on_bar` returning a `Signal`) and screen with
it. The screener compiles it (same AST gate as `sdk validate`), smoke-tests it,
then evaluates each universe symbol on the latest bar — pure compute, no
backtest/run_cycle. A `Signal.buy` (default; tune with `--signal-direction
buy|sell|hold|any`) is a match; `--rank-by-diagnostic <key>` orders matches by a
`Signal.diagnostics` value. Mutually exclusive with the boolean conditions
(`conflicting_screen_mode`). `--by-strategy` reuses a persisted definition and
only works against the API server (else `by_strategy_unavailable`). Compile /
smoke failures surface the same `error_code`s as `sdk validate`.

```bash
# scorer.py: class Strategy(Strategy) with on_bar -> Signal.buy(tag=..., diagnostics={"score": ...})
doyoutrade-cli stock screen --universe-file /tmp/u.csv \
  --scorer-file ./scorer.py --rank-by-diagnostic score --top-k 20
```

**常见筛选条件 → 原子组合**（这些是人话口径，由你翻译成下面的确定性原子；阈值/周期可按上下文调整）：

| 用户话术 | 推荐原子组合 | 说明 |
| --- | --- | --- |
| "多头向上 / 均线多头" | `--ma-above-ma 20,60` (+ 可加 `--ma-above-ma` 不能叠多条，用 `--price-above-ma 20`) + `--ma-slope-min 20,5,0` | 短均线在长均线上 **且** 均线在上行。更严格可再加 `--price-above-ma 20`（价在 MA20 上）。"多头排列" 没有单一开关——它就是这几个原子的 AND。 |
| "趋势强 / 真突破不是假突破" | 上面 + `--adx-min 25` | ADX 确认趋势强度，过滤震荡假信号。 |
| "近10日平均成交额≥10亿" | `--avg-amount-lookback 10 --avg-amount-min 1e9` | 绝对成交额（成交额单位是元）。注意是成交额不是成交量倍数（后者用 `--volume-ratio-*`）。 |
| "放量" | `--volume-ratio-lookback 5 --volume-ratio-min 2.0` | 相对成交量倍数，与绝对成交额不同。 |
| "强势股里挑最强的 5 只" | 过滤条件 + `--rank-by rsi --top-k 5` | 先漏斗后排序；`rank-by` 也可不带任何过滤，直接对 universe 选最强。 |
| "限定某些板块" | 先 `data sector-members "白酒,半导体" --output u.csv`，再 `stock screen --universe-file u.csv [...]` | 板块成分先构造 universe 文件再喂给 screen；见下方 `data sector-members`。 |
| "只筛我的自选股 / '核心持仓'标签那批" | `doyoutrade-cli watchlist list --tag <标签>` 取 `symbol` 列写成 universe 文件，再 `stock screen --universe-file ...` | 自选股标签 → universe 文件再喂给 screen；见 `doyoutrade-watchlist`。本地 K 线库默认也只同步自选股。 |
| "流通市值 > 100亿" | `stock screen ... --min-float-mv 1e10` | 流通市值单位是元，100亿 = `1e10`。该条件会拉基本面轴（auto = akshare 快照 → qmt）；无基本面数据的票按 `fundamentals_unavailable` 跳过。也可单独 `data fundamentals <code>` 看数值。 |
| "市值在 50亿~200亿之间" | `--min-float-mv 5e9 --max-float-mv 2e10` | min/max 同时给即区间。 |
| "排除停牌的 / 避开事件雷" | `stock screen ... --exclude-suspended` | 拉事件轴（akshare 停复牌快照），停牌票直接剔除。单独看用 `data events`。财报日历过滤暂未上线（见 §不覆盖范围）。 |
| "用我自己写的因子/逻辑筛 / 白名单条件表达不了" | 写一个 Strategy SDK 打分器，`stock screen --scorer-file scorer.py` | code-screen 模式：编译+smoke 后对 universe 每只票评估，`on_bar` 返回 `Signal.buy(...)` 即命中；`--rank-by-diagnostic <key>` 按 `Signal.diagnostics[key]` 排序。和布尔条件**互斥**。写打分器先 `load_skill strategy-definition-authoring`。 |

**Worked examples**:

```bash
# 1. Find oversold stocks (RSI < 30 with hammer or engulfing pattern in the last 10 bars).
doyoutrade-cli stock screen \
  --universe-file /tmp/u.txt \
  --asof 2026-05-26 \
  --rsi-max 30 --rsi-period 14 \
  --patterns hammer,bullish_engulfing

# 2. Momentum scan — 5-day return >= 5% with a volume spike (>=2x avg).
doyoutrade-cli stock screen \
  --universe-file /tmp/u.txt \
  --pct-change-lookback 5 --pct-change-min 0.05 \
  --volume-ratio-lookback 5 --volume-ratio-min 2.0 \
  --top-k 20 --sort-by pct_change --sort-desc

# 3. Breakout: close at 60-bar high, price above MA60, ADX confirms trend.
doyoutrade-cli stock screen \
  --universe-file /tmp/u.txt \
  --close-at-high-window 60 \
  --price-above-ma 60 \
  --adx-min 25 \
  --top-k 50 --sort-by adx14 --sort-desc

# 4. MA golden cross with MACD confirmation, both within the last 3 bars.
doyoutrade-cli stock screen \
  --universe-file /tmp/u.txt \
  --ma-cross golden:20,60 \
  --macd golden_cross \
  --cross-window 3

# 5. Full multi-axis screen: 板块 → 流通市值>100亿 + 多头向上 + 近10日均额≥10亿, rank by RSI.
doyoutrade-cli data sector-members "白酒,半导体" --output /tmp/u.csv
doyoutrade-cli stock screen \
  --universe-file /tmp/u.csv \
  --min-float-mv 1e10 \
  --exclude-suspended \
  --ma-above-ma 20,60 \
  --ma-slope-min 20,5,0 \
  --avg-amount-lookback 10 --avg-amount-min 1e9 \
  --rank-by rsi --top-k 20

# 6. No filter — just rank a whole universe by ADX and take the 30 strongest trends.
doyoutrade-cli stock screen \
  --universe-file /tmp/u.txt \
  --rank-by adx --top-k 30
```

**Envelope shape** (success):

```json
{
  "status": "ok",
  "asof": "2026-05-26",
  "interval": "1d",
  "data_source": "auto",
  "universe_size": 4123,
  "matched": 27,
  "skipped": 12,
  "lookback_days": 250,
  "result_path": "~/.doyoutrade/assistant/artifacts/screener_2026-05-26_20260526T120000Z.csv",
  "columns": ["symbol", "matched_conditions", "close", "rsi", "pct_change", ...],
  "preview": [/* first 10 matched rows */]
}
```

`matched_conditions` is a `;`-separated list of human-readable hit
descriptions per row (e.g. `rsi(14)<=30;patterns:hammer`). `skipped`
counts symbols dropped before evaluation — each one emits a
`screener_symbol_skipped` debug event with a `reason` field
(`no_bars_before_asof` / `insufficient_history` / `bar_fetch_failed` /
`evaluation_raised`); a non-zero `skipped` is **not** a run failure but
should be inspected in the debug view if the user expected those
symbols to show up.

## Reading tool errors

| `error_code` | Exit | Where | Repair |
| --- | --- | --- | --- |
| `validation_error` | 2 | `data run` | Bad `code` shape or `period`. Resolve via `doyoutrade-cli stock lookup`. |
| `invalid_period` | 2 | `data run` | `--period` doesn't match `<N><unit>` (d/w/m/mo/y). |
| `invalid_date` | 2 | `data run` | `--start` / `--end` not `YYYY-MM-DD`, or `--start` is after `--end`. |
| `conflicting_range_args` | 2 | `data run` | Combined `--period` with `--start` / `--end`. Pick one mode. |
| `file_not_found` | 3 | `analysis pattern` | Run `data run <code>` first to generate the cache. |
| `unknown_data_source` | 2 | `data run` | Use `auto` / `akshare` / `qmt`. |
| `market_data_continuity_violation` | 1 | any backfill that writes the local DB (`data run`, task/backtest gap-fetch) | Upstream returned a discontinuous series — a trading day is missing that is not a suspension/holiday. The whole write is **rejected** (no dirty data persisted). Inspect the `market_data.get_bars.continuity_violation` debug event for `missing_days_sample` / `served_provider`. Re-fetch from a source with an authoritative calendar (`qmt`/`baostock`), or — only if you accept unverifiable gaps — set the task's `data_cache.continuity.on_unverifiable_gap='degrade'`. |
| `unknown_arguments` | 2 | `stock screen` | Caller passed a top-level kwarg not in the schema. Drop or rename per the suggested path. |
| `invalid_universe` | 2 | `stock screen` | `--universe-file` was empty after stripping blanks / comments, or an entry was not a string. Add at least one canonical symbol. |
| `invalid_date` | 2 | `stock screen` | `--asof` is not `YYYY-MM-DD`. |
| `invalid_condition_value` | 2 | `stock screen` | Condition flag has an out-of-range / wrong-type value, or a paired flag is missing (e.g. `--volume-ratio-lookback` without `--volume-ratio-min`, or `--avg-amount-lookback` without `--avg-amount-min`). |
| `invalid_ma_above_ma` | 2 | `stock screen` | `--ma-above-ma` not `fast,slow`, periods `< 2`, or `fast >= slow`. Use e.g. `20,60`. |
| `invalid_ma_slope` | 2 | `stock screen` | `--ma-slope-min` not `period,lookback,min_slope`. Use e.g. `20,5,0`. |
| `invalid_rank_metric` | 2 | `stock screen` | `--rank-by` value unsupported, or `--rank-by avg_amount` without `--avg-amount-lookback`. Supported: `rsi` / `adx` / `cci` / `roc` / `macd_hist` / `avg_amount`. |
| `sector_empty` | (per-board) | `data sector-members` | Board resolved but has no constituents. Check the name via `data sectors`, or pass `--sector-type`. Surfaces in `sectors[i]`, not top-level. |
| `sector_fetch_failed` | (per-board / 1) | `data sectors` / `data sector-members` | Provider raised. Check the source is reachable (akshare network / qmt base_url); try `--data-source` explicitly. |
| `invalid_sector_type` | 2 | `data sector*` | `--sector-type` not `industry` / `concept`. |
| `data_source_unavailable` | 2 | `data sector*` / `data fundamentals` | Pinned `--data-source qmt` but no `data.qmt.base_url` configured. Use `akshare` or `auto`. |
| `fundamentals_fetch_failed` | 1 | `data fundamentals` / `stock screen` | Fundamentals provider raised (akshare snapshot down / qmt unreachable). For `stock screen` this is top-level (the market-cap gate can't be evaluated for anyone); check the source / try `--data-source`. |
| `fundamentals_unavailable` | (per-symbol skip) | `stock screen --min-float-mv` | The source has no `float_mv` for that symbol — skipped (in `skipped`), never treated as zero / passing. Emits a `screener_symbol_skipped` debug event with this reason. |
| `events_fetch_failed` | 1 | `data events` / `stock screen --exclude-suspended` | Event provider raised (akshare 停复牌 snapshot down). Top-level for screen (the suspension gate can't be evaluated for anyone); check akshare network. |
| `conflicting_screen_mode` | 2 | `stock screen` | `--scorer-file` / `--by-strategy` mixed with each other or with boolean conditions. Run them as separate screens. |
| `scorer_file_not_found` | 2 | `stock screen --scorer-file` | The path doesn't exist. |
| `compile_failed` / `missing_on_bar` / `disallowed_import` / `lookahead_access` / … | 2 | `stock screen --scorer-file` | The scorer failed the AST gate — same `error_code`s as `sdk validate`. Fix the source (load `strategy-definition-authoring`). |
| `smoke_failed` | 2 | `stock screen --scorer-file` | The scorer compiled but crashed on synthetic data. |
| `by_strategy_unavailable` | 2 | `stock screen --by-strategy` | The strategy definition repository isn't wired in this context — use `--scorer-file`, or run via the API server. |
| `code_screen_failed` | 1 | `stock screen --scorer-file` | Strategy evaluation raised after compile/smoke (e.g. data fetch). Check the data_source / the scorer's data needs. |
| `unknown_pattern_name` | 2 | `stock screen` | `--patterns` contains a name not in the supported list. Use only the names from the "Pattern" row of the conditions table. |
| `conflicting_conditions` | 2 | `stock screen` | Two conditions that can't both hold (e.g. `--rsi-min 80 --rsi-max 20`, or `--price-above-ma` + `--price-below-ma`). Drop one. |
| `no_conditions_specified` | 1 | `stock screen` | At least one condition flag is required; pass e.g. `--rsi-max 30`. |
| `scan_failed` | 1 | `stock screen` | The data provider raised when building or fetching bars. Check `data_source` config (qmt base_url / tushare token / akshare network) — try `--data-source` explicitly to isolate. |
| `ohlcv_csv_missing` | 3 | `analysis indicators` | No cached OHLCV for `code`. Run `data run <code>` first. |
| `ohlcv_csv_read_failed` / `ohlcv_csv_empty` / `ohlcv_columns_missing` | 2 | `analysis indicators` | Cache is unreadable / empty / missing OHLCV columns. Re-run `data run <code>` to regenerate. |
| `unknown_indicator` | 2 | `analysis indicators` | `--indicators` contains a name not in the SDK surface. The error appends the available list — pick from it. |
| `invalid_indicators_json` | 2 | `analysis indicators` | `--indicators` wasn't a comma list / JSON array / `all`. |
| `invalid_params_json` | 2 | `analysis indicators` | `--params` wasn't a JSON object keyed by indicator name. |
| `invalid_indicator_param` | 2 | `analysis indicators` | A value inside `--params` had the wrong type (e.g. a non-int period). |
| `invalid_tail` | 2 | `analysis indicators` | `--tail` must be a positive integer. |
| `indicator_computation_failed` | 1 | `analysis indicators` | An indicator raised while computing (e.g. too few bars for its warm-up). Pull a longer window with `data run`. |
| `invalid_indicator_params_json` / `invalid_script_params_json` | 2 | `data run` | JSON flag was not an object. Fix the JSON or drop the flag. |
| `invalid_symbols` | 2 | `data run` | `--symbols` was not a list / comma string / JSON array. |
| `invalid_universe_file` / `universe_file_not_found` / `universe_file_read_failed` | 2 / 3 / 1 | `data run` | `--universe-file` path is malformed, missing, or unreadable. |
| `invalid_symbol` / `no_symbols` | 2 | `data run` | A resolved symbol failed the canonical-shape check or the input resolved to zero symbols. |
| `missing_symbol_input` / `conflicting_symbol_args` | 2 | `data run` | Pass exactly one of positional `<code>`, `--symbols`, or `--universe-file`. |
| `conflicting_script_args` | 2 | `data run` | Passed both `--script` and `--script-file`. Pick one. |
| `invalid_warmup_bars` / `invalid_script_timeout` | 2 | `data run` | `--warmup-bars` must be integer >= 0; `--script-timeout` must be a positive number. |
| `script_warmup_unspecified` | 2 | `data run` | Pure-script run without `REQUIRED_HISTORY` literal and without `--warmup-bars`. Declare one or pass the other. |
| `script_syntax_error` / `script_compile_failed` | 2 | `data run` | Inline / file script failed to parse or compile. Fix the SyntaxError. |
| `script_file_path_invalid` / `script_file_not_found` / `script_file_read_failed` | 2 / 3 / 1 | `data run` | `--script-file` is missing or unreadable. |
| `script_invalid` | 2 | `data run` | `--script` was empty / not a string. |
| `script_disallowed_import` / `script_ast_disallowed` | 2 | `data run` | Script imported a module outside `{decimal, math, numpy, pandas, typing, doyoutrade.strategy_sdk}`. Remove the import. |
| `script_lookahead_access` | 2 | `data run` | Script used `df.shift(-N)`, which reads from the future. Use `shift(N)` with N>=1. |
| `script_silent_exception_swallow` | 2 | `data run` | Script has `except Exception: pass` or similar silent broad catch. Narrow the exception or remove the try. |
| `script_silent_type_coercion` | 2 | `data run` | `if not isinstance(x, T): x = default` pattern. Raise instead. |
| `script_compute_signature_invalid` | per-symbol | `data run` | `compute()` must be defined as `compute(df, target_df, params)` — those exact names, in that order, no *args/**kwargs. |
| `script_required_history_invalid` | 2 | `data run` | `REQUIRED_HISTORY` literal was < 0. Must be >= 0. |
| `script_no_result` | per-symbol | `data run` | Script defined neither `compute(...)` nor a top-level `result =`. |
| `script_output_invalid` | per-symbol | `data run` | Script returned a type other than Series / DataFrame / non-empty dict, or an entry had an unsupported value type. |
| `script_output_scalar_broadcast` | per-symbol | `data run` | A dict entry was a scalar (int / float / str / None). Return a Series or list aligned to `target_df.index`; scalar broadcasting masks the "forgot to return a Series" bug. |
| `script_name_error` / `script_key_error` / `script_attribute_error` / `script_import_error` / `script_type_error` / `script_runtime_error` | per-symbol | `data run` | Script (or `compute()`) raised at runtime. `script_runtime_error` is the generic fallback; the named codes are sub-typed for the most common cases. `error_type` carries the exact Python exception class. |
| `script_timeout` | per-symbol | `data run` | Script exceeded `--script-timeout` seconds. Increase the timeout or simplify the script. |
| `data_fetch_failed` / `ohlcv_empty` / `no_ohlcv_in_requested_window` / `ohlcv_frame_invalid` / `ohlcv_columns_missing` | per-symbol | `data run` | Provider returned no usable bars for that symbol. Check the symbol, date range, and data source. |
| `interval_not_supported_for_instrument_type` | per-symbol | `data run` | The chosen `--data-source` cannot serve this `--interval` for this symbol's instrument type — most commonly a 指数 (e.g. `000001.SH`) requested at a minute interval (`5m`/`15m`/`30m`/`60m`) on `baostock`, which has no index minute-bar history. Rejected up front (no network call) instead of surfacing an opaque upstream parser error. Use `--interval 1d`, or try `--data-source akshare` (fragile but does expose an index minute endpoint) or `auto`. |
| `data_run_unexpected_failure` | per-symbol | `data run` | An uncategorised exception escaped per-symbol handling. Inspect the message + debug events. |
| `missing_symbol_input` / `conflicting_symbol_args` | 2 | `data news` / `data reports` / `data earnings` | Pass exactly one of positional `<code>`, `--symbols`, or `--universe-file`. Shared symbol-input helper. |
| `invalid_symbols` / `invalid_symbol` / `no_symbols` | 2 | `data news` / `data reports` / `data earnings` | Same symbol-shape checks as `data run` (shared helpers) — bad list/CSV, non-canonical symbol, or zero symbols resolved. |
| `invalid_universe_file` / `universe_file_not_found` / `universe_file_read_failed` | 2 / 3 / 1 | `data news` / `data reports` / `data earnings` | `--universe-file` path is malformed, missing, or unreadable. |
| `invalid_period` / `invalid_date` / `conflicting_range_args` | 2 | `data news` / `data reports` / `data earnings` | Window flags same as `data run`: bad `--period`, bad `--start`/`--end`, or both modes combined. |
| `unknown_data_source` | 2 | `data news` / `data reports` / `data earnings` | All three support only `auto` / `akshare`. |
| `invalid_limit` | 2 | `data news` / `data reports` | `--limit` must be an integer >= 0. (`data earnings` has no `--limit`.) |
| `news_empty` | per-symbol | `data news` | No articles fell inside the publish-date window (akshare returns only recent news — widen the window or try another symbol). Distinct from a fetch error. |
| `news_fetch_failed` | per-symbol | `data news` | The akshare upstream raised on every retry. `error_type` carries the exception class. Check the symbol and network. |
| `data_news_unexpected_failure` | per-symbol | `data news` | An uncategorised exception escaped per-symbol handling. Inspect the message + debug events. |
| `research_reports_empty` | per-symbol | `data reports` | No reports fell inside the report-date window (widen the window or try another symbol). Distinct from a fetch error. |
| `research_reports_fetch_failed` | per-symbol | `data reports` | The akshare upstream raised on every retry. `error_type` carries the exception class. Check the symbol and network. |
| `data_research_reports_unexpected_failure` | per-symbol | `data reports` | An uncategorised exception escaped per-symbol handling. Inspect the message + debug events. |
| `market_breadth_empty` | global | `data breadth` | All three pools (涨停 / 跌停 / 炸板) returned nothing — very likely a non-trading day or the after-hours snapshot hasn't updated yet. Confirm it is a trading day. Distinct from a fetch error. |
| `market_breadth_fetch_failed` | global | `data breadth` | The akshare upstream raised on every retry for **all** pools. `error_type` carries the exception class. Check the data_source and network. |
| `invalid_date` | global | `data breadth` | `--date` isn't a valid `YYYY-MM-DD` (or `YYYYMMDD`) calendar date. |
| `unknown_data_source` | global | `data breadth` | `--data-source` supports only `auto` / `akshare`. (Single-pool failures don't fail the run — they set `data.status: partial` + `data.pool_errors`, not an `error_code`.) |
| `lhb_empty` | global | `data lhb` (market mode) | No name made the 龙虎榜 in the window — very likely a non-trading window or the after-hours snapshot hasn't updated yet. Confirm it is a trading day and盘后数据已更新. Distinct from a fetch error. |
| `lhb_no_seat_data` | global | `data lhb` (seat mode, `--symbol`) | The name did NOT make the 龙虎榜 on the requested `--date` (akshare's own None-subscript), or returned zero parseable seats. **Distinct** from `lhb_fetch_failed`: confirm the name actually 上榜 that day; do NOT retry as a transport error. |
| `lhb_fetch_failed` | global | `data lhb` | The akshare upstream (`stock_lhb_detail_em` market / `stock_lhb_stock_detail_em` seat) raised a **non**-no-seat-data exception on every retry. `error_type` carries the exception class. Check the data_source and network. |
| `invalid_date` | global | `data lhb` | `--date` / `--start` / `--end` isn't a valid `YYYY-MM-DD` (or `YYYYMMDD`) calendar date; market mode: `--date` combined with `--start`/`--end`, only one range end given, or `start > end`; seat mode (`--symbol`): a `--start`/`--end` range was given (seat mode is single-`--date` only). |
| `invalid_symbol` | global | `data lhb` (seat mode) / `data chips` | `--symbol` was empty / blank. Pass a canonical `CODE.EXCHANGE`, e.g. `600519.SH`. |
| `unknown_data_source` | global | `data lhb` / `data fund-flow` / `data sector-heat` / `data chips` | `--data-source` supports only `auto` / `akshare`. |
| `chip_distribution_empty` | global | `data chips` | `stock_cyq_em` returned no rows — 筹码分布 only covers A-share individual stocks; confirm the symbol is not an ETF/index/delisted name. Distinct from a fetch error. |
| `chip_distribution_fetch_failed` | global | `data chips` | The akshare `stock_cyq_em` upstream raised on every retry. `error_type` carries the exception class. Check the symbol and network. |
| `invalid_days` | global | `data chips` | `--days` must be an integer in `[1, 90]`. |
| `fund_flow_empty` | global | `data fund-flow` | The ranking came back empty. Distinct from a fetch error. |
| `fund_flow_fetch_failed` | global | `data fund-flow` | The akshare fund-flow endpoint raised on every retry (the 今日 endpoint intermittently `RemoteDisconnected`). `error_type` carries the exception class. |
| `invalid_period` | global | `data fund-flow` | `--period` not in the scope's allowed set (`individual` = {今日,3日,5日,10日}; `sector` = {今日,5日,10日} — **no 3日**). |
| `invalid_sector_type` | global | `data fund-flow` / `data sector-heat` | `data fund-flow`: `--sector-type` must be `行业` / `概念` / `地域` (maps to akshare 行业/概念/地域资金流). `data sector-heat`: `--sector-type` must be `concept` / `industry`. |
| `sector_heat_empty` | global | `data sector-heat` | The board list came back empty. Distinct from a fetch error. |
| `sector_heat_fetch_failed` | global | `data sector-heat` | The akshare board-name endpoint raised on every retry (eastmoney board endpoints are occasionally rate-limited / `RemoteDisconnected`). `error_type` carries the exception class. |
| `earnings_empty` | per-symbol | `data earnings` | No rows for this symbol across every requested kind × report period (widen the window or try another symbol). Distinct from a fetch error. |
| `earnings_fetch_failed` | global / per-symbol | `data earnings` | The whole earnings batch (or all periods a symbol needed) failed upstream. Per-period failures are recorded by the provider but do NOT abort the batch. |
| `no_report_periods` | global | `data earnings` | The window contained no fiscal quarter-end (03-31 / 06-30 / 09-30 / 12-31). Widen the window. |
| `invalid_kind` | global | `data earnings` | `--kind` not in `forecast` / `express` / `both`. |

See the main-agent system prompt's "CLI envelope 速读" section for the general envelope.

## Combining with bash

```bash
# Quick eyeball: latest close + 5d return for a stock
SYMBOL=$(doyoutrade-cli stock lookup 茅台 --limit 1 | jq -r '.data.items[0].symbol')
doyoutrade-cli data run "$SYMBOL" --period 1m --indicators rsi,macd --tail 1 \
  | jq '.data.symbols[0] | {ohlcv_path, indicator_path, latest}'

# Multi-symbol batch via data run — one envelope, per-symbol artifacts,
# per-symbol failure visibility.
echo -e "600519.SH\n000001.SZ\n300750.SZ" > /tmp/u.txt
doyoutrade-cli data run --universe-file /tmp/u.txt --period 6m --indicators rsi \
  | jq '.data | {symbols_succeeded, symbols_failed, manifest_path}'

# Many-symbol boolean scans: use `stock screen` instead — it auto-sizes
# the lookback, evaluates conditions in a single fetch pass, and writes
# a single matched-row CSV. `data run` is for "I want the actual bars
# and indicator values" per symbol; `stock screen` is for "which symbols
# satisfy these conditions".
doyoutrade-cli stock screen --universe-file /tmp/u.txt --patterns hammer,bullish_engulfing --rsi-max 35
```

## Rendering charts in chat: the `render_panel` in-process tool

When the user wants to **see** a chart rather than read numbers — "画/看
K 线"、"用折线图/柱状图/饼图展示"、"把关系画成知识图谱"、回测净值曲线、
分类占比 — call the in-process `render_panel` tool (not a `doyoutrade-cli`
command). It renders a declarative panel inline in the web console as a
graphical card.

Args: `{"title"?: str, "panel_id"?: str, "blocks": [ ... ]}` — `blocks` is a
top-to-bottom stack (1–12), each discriminated by `type`:

- `kline` (candlesticks, **reference-style**): `{type:"kline","symbol":"600519.SH",
  interval?:"1d|5m|60m", start?, end?, adjust?:"qfq|hfq|none",
  main_indicator?:"MA|BOLL|none", sub_indicator?:"MACD|KDJ|RSI|WR|none",
  overlays?:["backtest_trades"|"task_fills"|"signals"]}`. `symbol` must be a
  canonical `CODE.EXCHANGE` from `stock lookup` first — the frontend pulls bars
  from the local 行情库 (`GET /market/bars`); you do **not** inline bars. If the
  local warehouse has no data for the range, the panel shows empty and the user
  syncs it from the page.
- `chart` (line/bar/area/pie, **inline data**): `{type:"chart",
  chart_type:"line|bar|area|pie", data:[{...}], x_field?, y_fields?:[..],
  category_field?, value_field?, unit?, stacked?}`. line/bar/area need
  `x_field`+`y_fields`; pie needs `category_field`+`value_field`. Keep `data`
  small (dozens of rows).
- `kgraph` (knowledge graph): reference `{type:"kgraph","entity":"贵州茅台",
  hops?:1..3, layout?:"radial|force", color_mode?:"type|community"}` (frontend
  fetches `GET /knowledge/graph`), or inline `{type:"kgraph","nodes":[{id,name,
  node_type?}],"edges":[{id,src_id,dst_id,relation?}],center_id?}`.
- `table`: `{type:"table","columns":[{title,data_index,align?}],"rows":[{...}]}`.
- `statcard`: `{type:"statcard","metrics":[{label,value,unit?,delta?,
  delta_dir?:"up|down|flat"}]}`.
- `markdown`: `{type:"markdown","content":"..."}`.

After a panel renders (`{"status":"rendered", ...}`), keep explaining in
prose — do **not** re-dump the whole spec. Skip the tool when plain
numbers/text are clearer.

**Minimal valid payload** (the tool's `execute` args):
`{"blocks":[{"type":"kline","symbol":"600519.SH"}]}`

**Reading tool errors** — `render_panel` `error_code`s:

| `error_code` | Meaning | Fix |
|---|---|---|
| `unknown_arguments` | Top-level kwarg outside `v` / `title` / `panel_id` / `blocks`. | Stick to the declared top-level keys; block fields live inside `blocks[]`. |
| `invalid_blocks_json` | `blocks` was a malformed JSON string. | Pass `blocks` as an actual array, not a stringified one. |
| `invalid_panel` | `blocks` empty / not a list / >12 items. | Provide 1–12 block objects. |
| `invalid_block` | A block has an unknown `type` or is missing its required fields (e.g. chart without `x_field`/`y_fields`). | Read the message: it names `blocks[i]` and the missing field. |
| `invalid_symbol` | A `kline` block's `symbol` is not canonical `CODE.EXCHANGE`. | Run `stock lookup` first; never guess a symbol from a name. |
| `invalid_kgraph` | A `kgraph` block has neither `entity` nor `nodes`+`edges`, or a bad `hops`/`layout`/`color_mode`. | Provide `entity` (reference) or a non-empty inline `nodes`+`edges`. |

## What this skill does *not* cover

- Symbol resolution (Chinese name → CODE.EXCHANGE) — use
  `doyoutrade-stock`.
- Programmatic strategy data fetching — use
  `DataProvider.get_data(...)` inside the strategy and declare
  data_requests in the definition.
- Backtesting — `doyoutrade-backtest run / watch`.
- **`stock screen` boundaries** (v1):
  - Universe is **only** `--universe-file` — no `--market sh|sz|all`,
    `--sector`, `--symbol-prefix`, or "use this strategy task's
    universe" shortcut. Build the file via `stock lookup` first.
  - **No** industry / concept / market-cap / float-shares / PE / PB
    filters — `instrument_catalog` does not store those fields.
  - **No** intraday or live filters — the scan operates on the bar at
    or before `--asof` from the configured data provider.
  - **No** index-component filters ("沪深 300 成份") — there is no
    constituent table in the runtime.
  - **No** factor-rank ("top 20% by momentum") — `analysis factor`
    evaluates an existing factor against returns; it does not pick
    stocks from a factor value.
- `data reports` covers **brokerage research reports only**
  (券商个股研报). Earnings preannouncements (业绩预告) and earnings express
  reports (业绩快报) are served by `data earnings` (full-market per
  report-period, symbol-filtered in memory). The full audited financial
  statements (三大报表) and financial-ratio history series are **not** yet
  wired.
