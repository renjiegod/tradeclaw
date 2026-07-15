---
name: doyoutrade-portfolio-import
description: Import the user's holdings and trade history into Doyoutrade — extract positions from a brokerage screenshot with the in-process `import_positions_from_image` tool (multimodal model + symbol resolution), or import a broker 交割单 CSV into the private knowledge base `trades/<broker>/<YYYY-MM>.csv` via `import_trades_csv` / `doyoutrade-cli portfolio import-csv` (multi-broker column aliases, month-split, dedupe). Use when the user pastes a 持仓截图, uploads a 交割单/对账单 CSV, or says "导入持仓 / 导入交割单 / import my positions / import my trades". Reads of imported trades go through `doyoutrade-knowledge` (attribution 归因 uses the same trades/ partition).
category: tool
style: process
---

<!-- Routing:
- Screenshot → in-process tool `import_positions_from_image` (needs the runtime's
  multimodal model adapter; falls back to `portfolio_import_unwired` when absent).
- CSV → in-process tool `import_trades_csv`, or `execute_bash doyoutrade-cli
  portfolio import-csv` (pure local; no API server required).
- `doyoutrade-cli portfolio import-image` intentionally returns
  `not_available_via_cli` — the CLI process has no model routing.
- Symbol verification after extraction: `doyoutrade-cli stock lookup <name>`.
-->

# doyoutrade-portfolio-import

## When to use

- 用户贴了一张券商持仓截图，"帮我把持仓导进来 / 识别一下我的持仓" → `import_positions_from_image`.
- 用户给了交割单 / 对账单 CSV，"导入我的成交记录 / 交割单" → `import_trades_csv`（或 CLI `portfolio import-csv`）.
- "我的交易记录 / 归因 / 复盘" 的**读取**不在这里 → `doyoutrade-knowledge`（trades/ 分区、归因看板）。

## Tools

### `import_positions_from_image` (in-process)

Minimal valid payload:

```json
{"file_path": "/home/user/.doyoutrade/knowledge/uploads/positions.png"}
```

Optional: `"mime_type"` ∈ `image/png` / `image/jpeg` / `image/webp` / `image/gif`
(inferred from the extension when omitted; content is always magic-byte checked).
The file must live inside a registered sandbox root (e.g. `~/.doyoutrade/knowledge`),
be ≤ 8MB, and be a real PNG/JPEG/WEBP/GIF.

Success payload: `positions[]` with `name` / `quantity` and optional `symbol` /
`cost_price` / `current_price`. Names the instrument universe cannot resolve are
KEPT with `"symbol_unresolved": true` and echoed in `unresolved[]` — confirm
those with `doyoutrade-cli stock lookup <name>` before acting on them. Money
values are model-extracted; treat them as user-supplied, not authoritative.

### `import_trades_csv` (in-process)

Minimal valid payload:

```json
{"file_path": "/home/user/.doyoutrade/knowledge/uploads/交割单.csv", "broker": "huatai"}
```

Writes canonical monthly files `trades/<broker>/<YYYY-MM>.csv` under the
knowledge base, dedupes re-imports on `date+symbol+side+price+qty`, refreshes
`_index.md`, and smoke-checks readability (`attribution_readable`). Success
data: `written{rel_path: appended}`, `appended_total`, `duplicates_skipped`,
`fills_total`, `unparsed[]` (every skipped file/row with a reason — surface
these to the user, they are never silently dropped).

## CLI

```bash
doyoutrade-cli portfolio import-csv --file ~/Downloads/交割单.csv --broker huatai
```

Same envelope contract as other commands (`ok` / `data` / `error.error_code`).
`portfolio import-image` always returns `not_available_via_cli` with a hint to
use the in-process tool.

## Reading tool errors

| error_code | Meaning | Repair |
|---|---|---|
| `unknown_arguments` | Top-level kwarg not in the schema | Use only `file_path` / `mime_type` (image) or `file_path` / `broker` (csv) |
| `validation_error` | Bad field shape (e.g. empty `file_path`) | Fix the field per the message |
| `sandbox_violation` | File is outside every registered sandbox root | Copy the file under `~/.doyoutrade/knowledge` first |
| `file_not_found` | No file at the resolved path | Check the path |
| `file_read_failed` | OS-level read failure | Check permissions / re-upload |
| `portfolio_import_unwired` | Runtime has no multimodal model adapter wired | Use a runtime with model routing (API server assistant) |
| `unsupported_image_type` | Extension gives no MIME and `mime_type` omitted | Pass `mime_type` explicitly |
| `image_empty` | 0-byte image | Re-upload |
| `image_too_large` | > 8MB | Downscale / re-screenshot |
| `image_mime_mismatch` | Declared/inferred MIME ≠ magic bytes, or unsupported format | Fix `mime_type` or convert to PNG/JPEG/WEBP/GIF |
| `model_error` | Vision model call raised (type + message included) | Retry; if persistent check model route |
| `extract_parse_failed` | Model output not a JSON array (first 500 chars echoed) | Retry once; if persistent the image may not be a holdings screenshot |
| `extract_empty` | Model found no positions | Confirm the screenshot actually shows holdings rows |
| `invalid_broker` | Broker name unusable as a directory name | 1-64 letters/digits/_-/中文 |
| `csv_no_fills` | Zero buy/sell fills parsed (see `unparsed[]`) | Check the CSV has the broker's original header row |
| `csv_import_failed` | Unexpected import failure (CLI catch-all) | Read `error_type` / message |
| `not_available_via_cli` | `portfolio import-image` on the CLI | Use `import_positions_from_image` in-process |
