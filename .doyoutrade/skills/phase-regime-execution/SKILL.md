---
name: phase-regime-execution
description: "Execute stage-aware strategy switching for Doyoutrade tasks: detect 主升/震荡 transition from recent bars, then bind the corresponding strategy definition to the task and run backtest/live cycle. Use when the user asks '主升结束了吗', '现在该用主升还是震荡策略', '按阶段切策略', 'regime switch', 'stage routing', or asks the agent to auto-judge phase and apply the right strategy."
category: strategy
style: process
---

<!-- Routing:
- Strategy source edits / SDK contract changes → strategy-definition-authoring.
- Task CRUD / lifecycle only (without regime judgement) → doyoutrade-task.
- Backtest run / watch / summary deep-dive → doyoutrade-backtest.
- This skill is for "phase judgement + strategy switch execution" in one flow.
-->

# phase-regime-execution

## Purpose

Use this skill to let the Doyoutrade agent do two things in one workflow:

1. judge the current regime (主升是否结束、是否进入震荡/退潮候选),
2. apply the mapped strategy definition to a task (or one-shot backtest run).

This skill is not a replacement for strategy code. It is an execution playbook
that tells the agent when to use each existing strategy definition.

## Knowledge Source (must read first)

Read this file before any judgement or switch:

- `~/.doyoutrade/knowledge/cycles/2026-06/_phase-regime-routing-v2.md`

That note is the canonical memory for:

- regime rules and thresholds,
- default strategy-definition mapping,
- execution and risk guardrails.

If the file is updated, follow the file (do not hard-code stale thresholds).
If v2 is unavailable or explicitly disabled by the operator, fallback to:

- `~/.doyoutrade/knowledge/cycles/2026-06/_phase-regime-routing-v1.md`

## Trigger Phrases

Load this skill when the user says any of:

- "主升结束了吗"
- "现在该用主升还是震荡"
- "按阶段切策略"
- "帮我自动判断阶段并执行"
- "regime switch"
- "stage-aware strategy"

## Regime Decision Contract (v2 default)

The current default in knowledge is:

- **C2 mainrise-end confirm**: 2 consecutive days satisfy:
  - `close < ema20`
  - `drawdown10 <= -0.10`
  - `ret1 < 0`

The v2 router then decides:

- score trigger-day features using the thresholds from knowledge,
- switch to `range` only when score reaches the knowledge threshold,
- otherwise keep `mainrise`.

This v2 path is no-lookahead by contract: use only data available at
decision time.

## Execution Workflow

1. **Read knowledge note first**  
   Parse current thresholds and strategy mapping from the knowledge file.

2. **Fetch bars for target symbol/universe**
   - For one symbol:
     - `doyoutrade-cli data run <symbol> --start <focus+1> --end <latest> --data-source qmt --tail 1`
   - For a task universe:
     - fetch each symbol and evaluate independently, then aggregate by rule in
       the knowledge note.

3. **Compute regime features**
   - `ret1`, `ema20`, `drawdown10`, and optional dd5 / neg5 / break20_in5.
   - determine `days_to_c2` when C2 is present.

4. **Choose strategy definition**
   - Use the knowledge mapping for `mainrise` / `range` / `decay`.
   - Never infer by name similarity; use explicit `sd-...` ids.

5. **Apply switch**
   - Persisted task:
     - `doyoutrade-cli task update <task_id> --definition <sd-...>`
   - One-shot validation:
     - `doyoutrade-cli backtest run --definition <sd-...> ...`

6. **Emit explainable decision**
   Return:
   - selected regime,
   - triggered conditions,
   - selected definition id,
   - confidence caveat (if no clear trigger).

## Minimal Command Pattern

```bash
# 1) inspect current task
doyoutrade-cli task get <task_id>

# 2) bind the chosen stage strategy
doyoutrade-cli task update <task_id> --definition <sd-...>

# 3) optional verify via backtest
doyoutrade-cli backtest run --definition <sd-...> --universe <...> --range-start <...> --range-end <...>
```

## Error Handling

| error_code | Meaning | Repair |
| --- | --- | --- |
| `task_not_found` | task id/name cannot be resolved | discover via `doyoutrade-cli task list --q ...` |
| `wrong_identifier_type` | passed `sd-...` where a task id is expected | use `task_id` for task ops, `sd-...` for definition ops |
| `strategy_definition_not_found` | mapped definition id missing | read mapping file again; fix stale id in knowledge |
| `invalid_date` / `conflicting_range_args` | bar fetch window invalid | use one range mode and valid `YYYY-MM-DD` |
| `data_fetch_failed` / `ohlcv_empty` | no usable bars for judgement | report uncertainty; do not force a switch |

## Guardrails

- Do not switch strategy when feature inputs are missing; return "insufficient data".
- Do not silently broaden backtest windows to force a signal.
- Do not overwrite mapping ad hoc in chat; update the knowledge note explicitly.
- Keep `run_id` / debug trace visibility intact when integrating with runtime flows.

