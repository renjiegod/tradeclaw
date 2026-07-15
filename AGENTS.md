# Doyoutrade Agent 工作指南

> 权威文件即 `AGENTS.md`（仓库根）；`CLAUDE.md` 是指向它的软链，内容完全一致。

**核心原则**：功能改动不能只改"业务结果"，必须同步更新 trace / 调试 / 观测三条可见性链路。任何会改变 cycle / job 走向的失败都必须可见。

## 速查表：我的改动属于哪一类？

| 改动类型 | 必读小节 |
|---|---|
| 运行链路（worker / cycle / strategies / execution / data） | §关键原则、§运行链路模块清单、§最低同步要求、§错误可见性、§测试要求 |
| 新增 / 修改 assistant 工具 | §Assistant 工具入参规范、§Assistant 主提示词、§错误可见性 |
| 改 assistant 行为（CLI 命令面 / cron / skill 加载策略 / 硬性约束 / 真实对话链路） | §Assistant 主提示词、§Assistant 真实对话验证 |
| 加 try/except、类型转换、计数器自加 | §错误可见性 |
| 改持久化（schema / repository / serializer） | §最低同步要求（持久化部分）、§Migration 流程 |
| 前端 | §测试要求（前端部分）；若改调试 / trace UI（`TaskDebugPanel`、`components/assistant/*` 等），另见 §最低同步要求、§运行链路模块清单 |
| 排查 QMT 数据 / 交易问题（超时 / TransportError / 疑似数据缺失） | §QMT 数据 / 交易问题排查 |
| 纯文档 / 纯注释 | §Commit 风格 |

## 计划执行方式

执行计划默认把独立任务分发给 **subagent 并行**在当前工作区完成（用 `Task` 工具：`general` 跑多步实现、`explore` 跑只读探索），需要时再串行协作。原则：同一文件不要并行改（会冲突）；只读探索优先 `explore` agent。

> 注：旧版引用过 `superpowers:subagent-driven-development` skill，但该 skill 在本环境未安装、无法 load；以上即等价做法。

## 关键原则

- 不要使用 `git worktree`；在本仓库的单一工作区内完成开发与提交。
- 不要破坏 `run_id` 贯穿关系。涉及 `TradingWorker.run_cycle()`、`CycleRunState`（`doyoutrade/core/cycle_state.py`）、`model_invocation_scope(...)`（`doyoutrade/models/invocation_context.py`）、`doyoutrade/debug/context.py` 的 `debug_session_scope` / `emit_debug_event`、`doyoutrade/observability/debug_span_export.py` 的改动属于**高风险**，必须跑 §测试要求 全量集合。
- 实例调试运行走真实执行链路。修改正式运行逻辑时必须确认调试运行仍可用。`running` 实例不支持调试，这个约束不能绕开。
- 改运行链路时，默认要检查是否同步更新：
  - `debug_sessions` / `debug_session_events` / `debug_session_spans`（调试会话下的结构化事件与从 OpenTelemetry 落库的 span）
  - `model_invocations`
  - 前端调试页、前端类型、API 返回结构
- 启动入口 `doyoutrade`（`doyoutrade/api/server.py::main`）支持 `--mode {doyoutrade,qmt-proxy,both}`（缺省按 OS：win32→both，否则 doyoutrade；`DOYOUTRADE_LAUNCH_MODE` 可覆盖）。`both` 在同进程内用 daemon 线程起内置 qmt-proxy（`doyoutrade/infra/qmt_proxy_server.py`，import 自打包的 `doyoutrade/_qmt_proxy/app.main:app`），并把默认账户 `base_url` 自动指向本机。qmt-proxy 依赖走 `pip install 'doyoutrade[qmt-proxy]'` extra（xtquant 仅 Windows）。改启动分派 / 内嵌逻辑时，内嵌 import/启动失败必须**明确 raise + 日志**，禁止静默降级为 doyoutrade-only。

## 配置管理（Web UI 可改的 YAML 配置）

静态 / 低频的**全局系统配置**存 YAML 文件、可在前端「设置」页（`/settings`）改；账户 / 模型路由 / 渠道 / 任务 / 策略 / Agent / Cron 仍存 **DB**（各自已有 CRUD + UI，且账户/模型带密钥加密与迁移，属高风险区，不要搬进 YAML）。

- **doyoutrade 全局**：`~/.doyoutrade/config.yaml`（`DOYOUTRADE_HOME` 改根；`$DOYOUTRADE_CONFIG` 显式路径最高优先；缺失时从打包 `doyoutrade/default_config.yaml` 播种）。读写走 `doyoutrade/config_store.py`（`read_config_masked` / `write_config`），HTTP 面 `GET/PUT /config`。
- **qmt-proxy 服务端**：`~/.doyoutrade/qmt-proxy.yml`（env `QMT_PROXY_CONFIG` 覆盖；内嵌启动时由 `qmt_proxy_server.py` 注入该路径）。读写走 `qmt-proxy/app/config_store.py`（**mode-aware**：文件按 `modes.<APP_MODE>` 分段，写回要写对段），HTTP 面 qmt-proxy 的 `GET/PUT /api/v1/config`（Bearer 鉴权）；doyoutrade 侧 `GET/PUT /qmt-proxy/config` 用默认账户 base_url+token **转发**，故 UI 改 QMT 需默认账户可达（内嵌 both 天然可达）。
- **写回纪律**：YAML 用 `ruamel.yaml` round-trip **保注释**；`write_config` 必须**复用既有校验器**（doyoutrade 复用 `config._parse`；qmt-proxy 复用 `QmtClientConfig`/`resolve_clients` + 类型守卫）结构化拒绝坏值，禁止静默 coercion；secret（token/api_keys/…）GET 脱敏为 `********`，PUT 收到掩码即保留原值；每次写盘 `logger.info` 记录 changed_fields + restart_required。
- **生效**：能热重载的走 `reset_config()` / `reset_settings()`；**多数字段在启动时被快照，改完需重启**——API 返回 `restart_required` + `restart_fields`，前端据 `restart_required_fields`（后端返回，不硬编码）标「需重启」。新增可改字段时，必须核实其消费方是否每次重读，不确定就归入 restart。
- **转发错误分层**：qmt-proxy 上游 4xx（用户输入校验错）原样透传保留 `error_code`/`field`；连接失败 / 5xx 才归 `502 qmt_proxy_error`；无默认账户/缺 base_url/token 归 `400 qmt_proxy_unreachable`。

## 运行链路模块清单

**高风险判定（行为触发，优先于路径清单）**：凡写入 `cycle_runs` / `debug_sessions` / `debug_session_spans` / `model_invocations` / `trade_fills` 表的代码——含被上述路径间接复用的 serializer / repository / mapper——一律视为高风险，必须走 trace / debug 同步检查并跑 §测试要求 全量集合。不要只靠下面的文件路径清单漏判（路径清单只覆盖显式入口）。

修改以下模块默认走 trace / debug 同步检查（即"高风险"判定依据）：

- 核心：`doyoutrade/core/worker.py`、`doyoutrade/core/cycle_state.py`
- 运行时与装配：`doyoutrade/runtime/*`、`doyoutrade/bootstrap.py`
- 策略：`doyoutrade/strategy_sdk/`（含 `runner.py` 的 `StrategyRunner` 与 `_emit_failure`）、`doyoutrade/strategy_runtime/compiler.py`（`StrategyCompiler` + `_StrategyASTVisitor`）、`doyoutrade/bootstrap.py::InstanceSignalGenerator`
- 执行 / 数据 / 平台：`doyoutrade/execution/*`、`doyoutrade/data/*`、`doyoutrade/platform/service.py`
- 模型调用：`doyoutrade/models/recording.py`、`doyoutrade/models/invocation_context.py`
- 可观测性 / 调试 / 持久化：`doyoutrade/observability/*`（含 OTel 初始化、debug span 导出）、`doyoutrade/debug/*`、`doyoutrade/persistence/*`
- API：`doyoutrade/api/app.py`
- 前端：`frontend/src/components/TaskDebugPanel.tsx`（及 `TraceViewer.tsx` / `TracesPanel.tsx` / `TaskCycleRunsPanel.tsx`）、`frontend/src/components/assistant/*`（`serializeSession` / `streamHelpers` / `MessageContentRenderer` / `InlineToolCall*` 等，决定 trace / 工具调用 / debug 导出的可见性）、`frontend/src/api.ts`、`frontend/src/types.ts`

## 最低同步要求

新增运行阶段 / 策略分支 / 工具调用 / 执行结果 / 错误类型时，至少在以下一个位置可见：

- Worker 阶段可观测输出（OTel phase span、结构化日志、经 `emit_debug_event` 进入调试导出的 payload）
- 关键步骤的 span 信息 + span event（"关键步骤"判据见 §错误可见性）
- Model invocation request / response
- 前端调试弹窗

新增**条件分支 / 状态 / 失败模式**时必须三处同步：OTel span attribute、debug event、frontend types。

修改持久化结构时必须同步：SQLAlchemy 模型 → repository / serializer → Alembic migration → API → frontend types / UI。

## Migration 流程

- migration 位置：`alembic/versions/`，命名 `YYYYMMDD_<slug>.py`（参考最近的文件风格）。
- 本地验证：`make migrate` 走仓库统一入口（`doyoutrade.persistence.runtime_state.run_migrations`），不要直接 `alembic upgrade`，避免漏掉 runtime 装配。
- autogenerate 不可信（特别是 enum / JSON 字段 / index），必须手工 review 生成的 op 列表。
- 同一 PR 内不允许有未应用的 migration，否则 e2e / unit test 会用旧 schema。

## Assistant 工具入参规范

新增 / 修改 assistant 工具时按下列约定声明，避免再次踩到 `unknown_arguments` / 静默吞参 / JSON 字符串 / 标识符串台 / 硬覆盖既有字段。基础 helper 在 `doyoutrade/tools/{_contract,_coercion,_identifier_kinds}.py`，**每个 helper 顶部 docstring 是权威定义**，下面只是要点：

- 顶层 schema 默认 `additionalProperties: false`；`execute(**kwargs)` 入口必须先 `self._enforce_kwargs_contract(kwargs)`，禁止裸 `**kwargs`（typo 会静默失败）。
- 历史 / 错位字段用 `legacy_top_level_lifts` 声明搬迁规则（顶层 key 搬到 `settings.X`、JSON 字符串容错）；部分更新类工具加 `autocreate_lift_parents = True`。
- 任何 `type: object` / `type: array` 字段声明 `coercion_rules`，入口调 `self._apply_schema_coercion(...)`，错误码统一 `invalid_<field>_json`。
- 接受 `task_id` / `instance_id` / `definition_id` 的字段声明 `identifier_guards`，入口调 `self._apply_identifier_guards(...)`，错误结构沿用 `wrong_identifier_type`。
- 更新嵌套对象走 patch 语义：只写入调用方显式提供的字段，None 字段一律忽略；禁止整体覆盖既有 `approval_policy` / `risk_overrides` / `parameter_overrides`。
- 错误返回：成功带 `status: ok|created|updated`；已知错误带 `error_code` / `error_type` / `repair_hints`；入参拒绝带 `type` ∈ {`unknown_arguments`, `validation_error`} + `message` / `suggested_path`。`error_code` 一旦发布即视为 skill 文档可引用的稳定 token。
- debug event 命名：`operation_<name>.{request, validated, rejected, failed, created}`。`.rejected` 对应 unknown_arguments，`.failed` 对应 validation_error / 业务异常。
- 新工具或入参变更时，同步更新 `.doyoutrade/skills/<skill>/SKILL.md` 的 minimal valid payload 示例与 "Reading Tool Errors" 里的 error_code 列表。

## Assistant 主提示词

主 Agent 的系统提示词在 `doyoutrade/assistant/prompt_templates/main_agent.j2`，是定义 assistant 行为的**权威文件**。它规定了 CLI 命令分布、in-process 工具集合、CLI envelope / 退出码语义、资源任务速查表、硬性约束（金额十进制、symbol 必查 lookup、资源 ID 不得凭名字推断、回测报告必须读 `data.report_path`、不得擅自拉长回测窗口）、`<system-reminder>` 注入的 `currentDate` / `currentTime`、资源任务起手式（`stock lookup` + `strategy inspect` 并行，分流"复用 vs 创作"）、Skill 使用规则、调度与延时任务（Cron 是唯一通道，`--in` / `--at` / `--cron-expression` 三选一，禁用 `sleep` / `at` / `crontab` 模拟）、`[cron-trigger]` 会话的处理纪律。

修改 assistant 行为时务必同步改这里——否则模型在真实会话里仍然按旧 prompt 走，CLI / skill / cron 哪边的代码改了都不会被实际遵守：

- 新增 / 改名 / 删除 `doyoutrade-cli` 子命令、调整命令域划分 → 同步"工具入口"、"CLI 域分布"、"资源任务速查表"。
- 改 in-process tool 集合（新增 / 移除 / 重命名） → 同步"仅有的 in-process tool"清单与"其余一切……必须 `execute_bash`"那段。
- 改 CLI envelope 字段、`error_code` 体系、退出码、`DOYOUTRADE_*` 环境变量 → 同步"CLI envelope 速读"小节及其表格。
- 改 cron 调度行为（schedule_kind、`delete_after_run` 默认值、`[cron-trigger]` header、recursive cron 拦截、模板可用变量） → 同步"调度与延时任务"整节。
- 改资源 ID 前缀（`sd-` / `si-` / `task-` / `btjob-` / `run-` / `dbg-` / `sess-`）、`stock lookup` canonical symbol 规则、`backtest run` 必读 `report_path` 的契约 → 同步"硬性约束"。
- 改 skill 加载时机 / 名称（`strategy-authoring` / `strategy-definition-authoring` / `strategy-iteration` 等） → 同步"资源任务起手式"与"Skill 使用规则"。
- 改 `<system-reminder>` 注入键（`currentDate` / `currentTime` / `currentWeekday`、`TRACEPARENT`） → 同步对应段落。

CLI / 工具 / cron 行为与 prompt 文案脱节是历史踩过的坑（典型如 `unknown_command` 改名后 `did_you_mean` 在 prompt 里没提，模型反复试错），按 §Assistant 工具入参规范 的标准发布 `error_code` 时也要在本 prompt 的 envelope 段或速查表里有一处指向。

## Assistant 真实对话验证（server + doyoutrade-cli）

单元测试 / `make test-e2e` 通过后，凡改动会影响 agent 对话真实路径时，默认还要由编程 agent 自己启动 server，再用 `doyoutrade-cli` 跑一次真实 chat 对话验证。适用场景包括：assistant service、CLI assistant 命令面、prompt、skill loading、cron 对话入口、model invocation、trace / debug / session export，以及任何以前需要人工打开页面对话再复制会话信息给编程 agent 的场景。

这项验证是对 `make test-e2e` 的补充，不替代单元测试和 E2E。目标是确认真实 API server、真实 agent 配置、真实会话持久化、span / invocation 导出在完整链路中都能工作。

推荐流程：

1. 编程 agent 在当前仓库启动 API server：

   ```bash
   uv run doyoutrade
   ```

2. 解析 API base URL，并用 API 找可用 agent。CLI 不负责列 agent；原始 `curl` 要显式使用同一个 base URL：

   ```bash
   API_BASE="${DOYOUTRADE_API_URL:-http://127.0.0.1:8000}"
   curl "$API_BASE/assistant/agents?include_inactive=false"
   ```

3. 用 active agent 跑真实 one-shot chat，并导出详细会话信息：

   ```bash
   DOYOUTRADE_API_URL="$API_BASE" uv run doyoutrade-cli assistant run \
     --agent-id <active-agent-id> \
     --message "Validate the changed assistant flow" \
     --output /tmp/doyoutrade-chat-export.md
   ```

4. 检查 stdout 的 JSON envelope：`ok` 必须为 `true`，且包含 `session_id` / `export_path`。导出的 Markdown 应包含 `# Assistant Session Export`、attempt / run / trace 标识、spans、model invocations，以及本轮涉及的 tool calls / errors。

5. 需要结构化分析时，再导出 JSON：

   ```bash
   uv run doyoutrade-cli assistant export \
     --session-id <asst-session-id> \
     --format json \
     --output /tmp/doyoutrade-chat-export.json
   ```

`assistant run` 会在 chat 完成后等待诊断数据落库再写 export；如果导出里缺 span / model invocation，不要当作"页面没刷新"忽略，要按真实链路问题排查。若本地没有 active agent、模型路由、数据库或 API 凭据，不能声称已完成真实验证；交付说明里必须写清楚未跑原因和剩余风险。

## 错误可见性 / 静默吞 bug 禁令

历史上踩过的坑：策略 / 仓位 / 执行 / cron 链路里 `except Exception: pass`、silent `continue`、`TypeError` 静默回退、`int(weird_value)` 容错截断，让"业务结果"看起来 OK 但其实在掩盖真实故障（典型如 LLM 写错 `required_history` 导致整段回测零交易、`task_params_json` 非 dict 被替换成 `{}`、`_resolved_fill_quantity` 返回 0 但 `submitted_count++` 照常）。**任何会改变 cycle / job 走向的失败都必须可见**。

### "关键步骤" 判据 —— 需要 OTel span + span event

1. 每个 cycle phase（`worker.phase.*`）
2. 每个 assistant 工具入口（`operation_<name>`）
3. 每个 cron task 调度 / 投递 / pre-action（`cron.task.run` / `cron.delivery` / `cron.pre_action`）
4. 每个外部 IO（数据源拉取、broker 下单、LLM 调用）
5. 每个用户可见状态切换（intent → order、order → fill、cycle → terminal_status）

span attribute 至少包含：`run_id`、业务标识符（`job_id` / `intent_id` / `symbol` 适用项）、`status` / `terminal_status`。

### 禁止

- 裸 `except:` 或 `except Exception: pass`，包括 swallow 后 `continue`。即便是"幂等清理"也必须区分异常类型并 `logger.info`，其他异常必须 `logger.warning` 或更高级别带类型与消息。参考 `doyoutrade/assistant/cron_manager.py::_deregister_best_effort` 的"分流 + 三种 log 级别"写法。
- silent `continue` / silent `return` 让 signal / intent / order / job 默默消失。合法跳过必须发结构化 debug event（含 `reason` 与 `hint`）+ 至少一行 `logger.info`。参考 `doyoutrade/execution/position_manager.py::_emit_skip` 与 `doyoutrade/strategy_sdk/runner.py::_emit_failure`（及 `strategy_base_history_insufficient` 事件）。
- "宽容"类型转换掩盖 schema 违反：`int(value)` 容错截断、`if not isinstance(x, dict): x = {}`、`try: f(**kw) except TypeError: f()` 一律禁止。必须 raise 并附实际类型与值。参考 `doyoutrade/bootstrap.py::InstanceSignalGenerator`（strategy `__init__` 必须零参，违反时包成带类型 + 消息的 `ValueError`）与 `doyoutrade/strategy_sdk/errors.py` 的 `data_insufficient` error_code。
- 多种失败模式共用一个 `except` 只报笼统状态。失败模式必须在事件名 / 状态码层面区分（例：`strategy_base_history_insufficient` vs `signal_generation_failed` vs `strategy_on_bar_failed` vs `strategy_populate_indicators_failed`，均为真实发出的事件名）。
- 计数器自加在错误路径上 —— 例：`submitted_count++` 在 adapter 返回零成交时。计数语义必须与实际结果一致；零成交属于 `vetoed_count`。
- AST / 编译期能拦的"漂移 bug"放到运行期吃。例：`required_history = 34` 但策略代码内部写 `if len(df) < slow + signal`（=35）—— 字面量 / 计算量与 class 属性脱钩的模式必须在 `StrategyCompiler` 装配的 `_StrategyASTVisitor._history_check_literal` 里拒绝（error_code `history_check_literal_disallowed`）。

### 必须

任何 `try / except` 至少：

1. `logger.exception(...)` 或 `logger.warning(...)`（warning 及以上级别）含**异常类型 + 消息 + 关键上下文**（`job_id` / `intent_id` / `symbol` / `run_id` / `task_kind`）；
2. 属于 cycle / job 链路的，**发对应 debug event**（事件名 `<module>_<reason>` 风格，payload 含 `hint` 字段指向上游修复方向）；
3. 调用方能从**结构化字段**（`error_code` / `reason` / `status`）区分这次失败模式，不要把分类塞进自由文本。

新增 silent skip 时同步发 `<module>_skipped` 或同等结构事件 —— 参考 `position_manager_skipped` / `execution_zero_fill` / `dispatch_rejected` / `intent_validation_failed` / `intent_vetoed` / `intent_approval_blocked` / `signal_generation_failed` / `strategy_base_history_insufficient` 的 payload 风格。

正例对照（合法跳过）：

```python
# doyoutrade/execution/position_manager.py 风格
if not lot:
    self._emit_skip(
        intent_id=intent.id,
        reason="lot_size_unknown",
        hint="ensure instrument metadata is loaded before dispatch",
    )
    logger.info("position_manager skipped intent=%s reason=lot_size_unknown", intent.id)
    continue
```

正例对照（schema 违反必须 raise）：

```python
if not isinstance(payload, dict):
    raise ValueError(
        f"task_params_json must be dict, got {type(payload).__name__}: {payload!r}"
    )
```

用户输入 / LLM 生成代码进入运行链路前必须走**结构化校验**：编译期 AST 检查（`_StrategyASTVisitor._history_check_literal`，error_code `history_check_literal_disallowed`）、`validate_params`、`_enforce_kwargs_contract` / `_apply_schema_coercion` / `_apply_identifier_guards`。不允许运行时"试一下行不行"。

### 写代码前的自检

- 写 `except Exception:` 或 `except .*: pass` 前问：操作员看到"跑过去了但没生效"，能不能从日志或 debug event 立刻看到这里被吞了？答不上 → 补日志或事件。
- 写 `if not isinstance(x, T): x = default` 前问：上游 schema 是否已经保证？保证不了 → raise 带类型 + 值的错误。
- 计数器自加前问：实际状态机里这件事真发生了吗？没发生分到对应的失败计数里。
- 加 LLM-facing 工具 / 生成模板前问：LLM 写错一个数字或拼错 key，bug 会在编译期、运行期、还是交付前最后一刻才暴露？越早越好。

## QMT 数据 / 交易问题排查

QMT 取数 / 下单出问题时（`TransportError` / `ReadTimeout` / 取数超时 / 零交易疑似数据缺失），优先用观测接口定位，不要靠盲改重试或拉长超时撞运气：

- **doyoutrade 侧**：按 `run_id` 或 OTel `trace_id` 拉调试视图 `doyoutrade-cli debug get-run-view <run_id>` / `get-trace-view <trace_id>`。按粒度看三层 span：HTTP 层 `qmt.http.request`（含 `qmt.http.transport_error` event，payload `error_type` / `message` / `url`）、数据层 `data.qmt.<method>`、SDK 层 `qmt_sdk.data.<method>` / `qmt_sdk.trading.<method>`（失败时设 `qmt_sdk.error` span attribute），定位是哪个 symbol、什么取数区间、超时还是业务错误。历史取数走读优先快路径（`disable_download=True`）；本地无数据时回退到下载并发 `qmt_market_download_fallback` debug event（payload 带 `reason=local_read_empty` 与 `hint`），看到它就说明该区间没预下载，应 backfill。
- **qmt-proxy 侧（部署 qmt-proxy 的 Windows 主机，默认端口 8000）**：每个 xtdata 操作的耗时 / 参数 / 成败都记到内存环形缓冲 + `logs/xtdata_ops.jsonl`，**无需复现**即可查：
  - `GET /api/v1/diagnostics/summary` —— 按 operation 看 count / avg_duration_ms / max_duration_ms / error_count，一眼判断哪个接口慢 / 在报错。
  - `GET /api/v1/diagnostics/xtdata-ops?limit=&only_errors=true&min_duration_ms=&operation=` —— 拉最近的慢调用 / 失败明细（operation、精简参数、duration、exit_code、stderr 片段）。
  - 两个接口都要带 `Authorization: Bearer <api-key>`（同数据账户 token）。
- 背景：QMT 历史取数（`/api/v1/data/market`）有 ~秒级基线延迟（隔离子进程 + `xtdata.connect`，下载路径已合并为单子进程）。数据账户 `timeout_seconds` 默认 30s（`accounts` 表，迁移 `20260614_03`）。怀疑超时先看上面两侧的实测耗时，再决定调超时还是预下载 / backfill —— 不要先动 timeout。

## 测试要求

### 后端

- 本项目用 **stdlib `unittest`**（见 `Makefile` 的 `test` 目标），**不要**假设有 pytest。
- 运行前需 `make install` / `make deps` 或 `uv sync`。
- 全量：`make test`（= `uv run python -m unittest discover -s tests -v`）。
- 子集：`uv run python -m unittest tests.test_persistence -v`。
- 涉及 trace / debug / API / persistence 改动，**至少跑**：

  ```bash
  uv run python -m unittest tests.test_persistence tests.test_platform_service tests.test_api_app tests.test_worker_signal_path tests.test_worker_code_root_pin tests.test_observability tests.test_model_invocations -v
  ```

- 紧贴调试会话 / span 导出 / debug 覆盖逻辑时，再跑 `tests.test_debug_overrides`。

### 前端

前端有改动时（仓库根目录运行）：

```bash
npm --prefix frontend run build    # 必跑，等价 type check
npm --prefix frontend run test     # vitest，改动涉及组件行为 / 选择逻辑 / API 适配层时跑
```

> 单文件跑：`npx vitest run src/path/to/foo.test.tsx`（仓库根）。全量并行下少数用例（`AssistantPage` / `CreateAgentCard` 等带 SSE / rAF 时序的）偶发超时 flake；遇疑似 flake 用 `npx vitest run --no-file-parallelism` 或单文件复核，**不要误判为回归**——先和 `git stash` 后的 baseline 对比。

### E2E

- 默认要跑 `make test-e2e`（profile=`isolated`，临时 SQLite + mock data + stub model 验证真实 runtime 链路）。
- **可豁免**（豁免须在交付说明里写明剩余风险）：纯文档 / 纯前端样式 / 仅注释 / 仅新增测试。
- **不可豁免**：运行链路、trace/debug、model invocation、persistence、API、金额序列化、任务生命周期。
- 写 E2E 前先读 `docs/e2e-testing.md`；优先复用 `tests/e2e/support.py`，不要重新搭 runtime。
- 用例应覆盖 `run_id` 在 `cycle_runs` / `debug_sessions` / `debug_session_spans` / `model_invocations` / `trade_fills` / API payload 之间的贯穿关系，而不只验证内存返回值。
- 真实 QMT / 真实模型 / 真实 DB 走 `DOYOUTRADE_E2E_PROFILE=local` 或 `live`，配置参考 `tests/e2e/config.yaml.example`。
- 改 agent 对话场景时，除 `make test-e2e` 外，默认还要跑 §Assistant 真实对话验证，用 `doyoutrade-cli assistant run` 的导出结果闭环分析。

## Commit 风格

- 用 conventional commits + scope：`feat(assistant): ...` / `fix(worker): ...` / `refactor(docs): ...` / `test(persistence): ...`。
- subject 一行（祈使句、不带句号），多行 detail 放 body。
- 一个 PR 一个语义改动；migration、API、frontend types 同步更新可以放同一 PR。

## 完成前自检

按改动类型勾对应组，不要把不相关的项无意义打勾。

**通用（每次都要）**
- [ ] 功能实现
- [ ] §测试要求 的子集已实际跑过
- [ ] 新引入的 `try/except` / `continue` / 类型回退满足 §错误可见性 三条要求

**运行链路类（cycle / job / trace / debug）**
- [ ] `run_id` 仍贯穿 `cycle_runs` ↔ `debug_sessions` ↔ `debug_session_spans` ↔ `model_invocations` ↔ `trade_fills`
- [ ] trace 可用、调试会话能看到关键步骤（含 OTel 导出的 span / 事件）
- [ ] 新增分支 / 状态 / 失败模式在 OTel span attribute、debug event、frontend types 三处都体现
- [ ] E2E 已跑；未跑须写明豁免理由与剩余风险

**持久化类（schema / repository / serializer）**
- [ ] migration / API / frontend types 三处一致（SQLAlchemy → repository/serializer → Alembic → API → frontend types / UI）

**assistant 工具类**
- [ ] 新增 / 修改 assistant 工具走了 `_enforce_kwargs_contract` / `_apply_schema_coercion` / `_apply_identifier_guards`，并同步了 skill 示例与 error_code
- [ ] 改 agent 对话场景时，已用 `uv run doyoutrade` + `uv run doyoutrade-cli assistant run ...` 做真实验证；未跑须写明原因与剩余风险
