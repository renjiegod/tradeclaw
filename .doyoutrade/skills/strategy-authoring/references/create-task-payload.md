# `doyoutrade-cli task create` / `task update` Payload Shape

Read this file when you're about to run `doyoutrade-cli task create` or
`task update` and need to confirm the field shapes, or when you've just
hit `error_code: unknown_arguments` and want to see where a key should
have gone.

## Common CLI invocation (with flat flags + `--params`)

Top-level scalar fields ship as flags; richer nested fields go through
`--params '<json>'`. Explicit flags win over `--params` keys.

```bash
doyoutrade-cli task create \
  --name MACD_BlueFocus_Backtest_300058 \
  --mode backtest \
  --description "MACD on 300058.SZ daily" \
  --universe 300058.SZ \
  --definition sd-… \
  --params '{"window": 14, "agent": {"react_max_turns": 5, "signal_tool_names": []}}'
```

`--definition` writes `settings.strategy.definition_id`. A flat
`--params` object (e.g. `'{"window": 14}'`) becomes
`settings.strategy.parameter_overrides`; you can also pass a full nested
`strategy` block. There is no separate persisted instance resource — a
task binds the definition directly and carries its own
`parameter_overrides`. Explicit flags win over `--params` keys.

## Allowed top-level fields

The underlying `doyoutrade-cli task create` payload is flat — no
`settings` wrapper.
Allowed top-level keys:

- `name`（必填）
- `mode`（默认 `paper`）
- `description`（默认 ""）
- `universe`（`array<string>`）
- `strategy_preferences`（string）
- `data_provider`（默认 `"auto"`）
- `agent`（object，含 `react_max_turns` / `signal_tool_names` 等；
  `react_max_turns` 可省略，默认 500）
- `strategy`（object，**必填**，须含 `definition_id`；可选
  `parameter_overrides` / `approval_policy` / `risk_overrides` /
  `execution_profile`）

不允许把 `settings` 当成顶层参数。任何顶层未列出的 key 会被
`unknown_arguments` 直接拒绝（CLI envelope 里的 `error.suggested_path`
会给迁移建议）。

## Minimal valid `--params` JSON

When you do need to feed the underlying JSON directly:

```json
{
  "name": "MACD_BlueFocus_Backtest_300058",
  "mode": "backtest",
  "description": "MACD on 300058.SZ daily",
  "universe": ["300058.SZ"],
  "agent": {
    "react_max_turns": 5,
    "signal_tool_names": []
  },
  "strategy": {
    "definition_id": "sd-...",
    "parameter_overrides": {"window": 14}
  }
}
```

But almost always the CLI flag form above is shorter and clearer.

## Type rules

`universe` 必须是真正的 JSON 数组；`agent` / `strategy` 必须是真正的 JSON
对象。如果偶发性 stringify 了，coercion 会接住并返回
`invalid_<field>_json`，按 hint 改回原生类型再试。

`doyoutrade-cli task update <identifier>` 用相同的顶层字段，全部 optional（除
`<identifier>` 作为位置参数），传哪个 flag 就 patch 哪个字段（None /
缺省字段一律忽略，不会覆盖既有值）。

## Common error codes

- `error_code: "unknown_arguments"` — 顶层传了未声明的 key（包括把
  `settings` 当顶层）；CLI envelope 里 `error.unknown` 列出来的字段名
  按 `error.suggested_path` 挪到正确位置即可。
- `error_code: "invalid_strategy_json"` / `"invalid_agent_json"` /
  `"invalid_universe_json"` — 对应字段送了 JSON 字符串或形状错误；按
  `error.hint` 改成原生 object / array 再试。
- `error_code: "missing_name"` — `task create` 的 `--name` 缺失或空白。
- `error_code: "missing_strategy_binding"` — `task create` 没传
  `--definition`，也没在 `--params.strategy` 里给出 `definition_id`。
  补上 `--definition sd-…`（参数走 `--params` → `parameter_overrides`）。
- `error_code: "invalid_params_json"` — CLI 层的 `--params` 不是合法 JSON
  对象（仅在 CLI 路径出现）。

See `error-codes.md` for the full backtest-side error vocabulary.
