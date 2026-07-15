## Context

当前仓库的核心能力仍然是 `qmt-proxy` 服务本身和 `libs/qmt_proxy_sdk`，已经能够提供：

- 通过 REST 获取历史行情、财务数据、板块数据和账户状态。
- 通过订阅 + WebSocket 获取实时行情流。
- 通过交易接口完成连接、查询持仓/资产、下单和撤单。

如果直接在这些接口之上拼接一个“会调用大模型的脚本”，很快会遇到几个问题：

- 历史数据和实时推送没有统一的数据访问抽象，策略代码容易同时依赖多个底层接口。
- AI Agent 的推理链路如果直接连下单接口，会把风控、审批、回放和可审计性全部混在一起。
- Telegram、飞书、Web UI 等远程入口如果各自直接控制策略或下单，会出现权限、状态和行为不一致。
- 股票实盘具备交易时段、涨跌停、最小交易单位、T+1 等约束，不能把这些规则交给 LLM 自行“理解”。

`freqtrade` 给出的有效经验是：用一个主控对象管理运行状态，把统一数据访问对象注入策略，把策略约束为稳定接口，再把 Telegram/API/Web UI 收敛成统一 RPC 控制面。这个 change 需要把这些设计思想翻译到 `qmt-proxy` + `qmt_proxy_sdk` + LangChain + React 的场景里。

## Goals / Non-Goals

**Goals:**

- 建立一个四层架构：数据层、策略层、执行层、通道层。
- 首期仅以 `qmt_proxy_sdk` 作为数据和交易接入源，但保留后续新增数据源和券商适配器的接口边界。
- 让历史行情、实时行情、账户状态在策略运行时里形成统一上下文。
- 让 AI Agent 的作用聚焦在分析、计划和决策生成，而不是绕过规则直接下单。
- 支持 `backtest`、`paper`、`shadow`、`live` 四种运行模式。
- 提供 React 控制台和 Telegram/飞书通道，共享同一命令、审批和通知模型。
- 保证所有交易相关行为可审计、可回放、可限权。

**Non-Goals:**

- 首版不追求高频交易或毫秒级撮合优化。
- 首版不同时支持多个券商或多个行情提供商。
- 首版不把所有交易决策都交给 LLM 自主完成，硬风控和订单执行仍由确定性代码负责。
- 首版不在 `qmt_proxy_sdk` 中新增破坏性接口变更。
- 首版不引入复杂的分布式微服务和多集群部署。

## Decisions

### 1. 采用“模块化单体 + 明确边界”的首版架构，而不是一开始拆成多服务

首版建议在仓库中新增独立 AI 交易应用边界，例如：

```text
apps/
  ai_trader/
    api/                # FastAPI / control API
    supervisor/         # AgentSupervisor, mode/state machine
    data_layer/         # provider, cache, event journal
    strategy_layer/     # strategy contract, langchain graphs, backtest runtime
    execution_layer/    # risk engine, approval, broker adapter
    channel_layer/      # telegram, feishu, ui, webhook
    storage/            # repositories, audit tables
web/
  src/
    agent-console/      # React operator console
```

原因：

- QMT 场景天然是单账户、低并发、强状态系统，过早拆服务只会提高排障和联调成本。
- `freqtrade` 本身也是模块化单体，核心价值来自清晰职责边界，而不是微服务数量。
- 当前仓库已经有 Python/React 双栈和统一构建入口，新增模块比新增独立部署系统更平滑。

替代方案：

- 直接把 Agent 逻辑塞进现有 `app/routers` 或 `app/services`。该方案会把代理服务职责和交易机器人职责耦合，不采用。
- 一开始按数据、策略、执行拆成多个进程或消息队列服务。该方案对 MVP 过重，不采用。

### 2. 以 `MarketDataProvider` 统一历史行情、实时行情和账户上下文

数据层定义稳定抽象：

- `get_history(symbols, timeframe, start, end)`
- `stream_quotes(symbols, timeframe)`
- `get_account_snapshot()`
- `get_positions()`
- `get_instruments()`

首个适配器 `QmtProxyMarketDataProvider` 通过 `qmt_proxy_sdk` 实现：

- 历史数据走 `client.data.get_market_data(...)` / `get_local_data(...)`
- 实时数据走 `client.data.subscribe_and_stream(...)`
- 账户和持仓上下文走 `client.trading.*`

进入策略层前，统一归一化为规范事件对象：

- `BarEvent`
- `TickEvent`
- `AccountSnapshot`
- `PositionSnapshot`
- `MarketContext`

同时维护：

- 按 `symbol + timeframe` 的有界缓存
- 事件时间戳与来源字段
- 原始 payload 以便审计与问题回放
- 缺口检测和补拉逻辑

原因：

- 这与 `freqtrade` 的 `DataProvider` 思路一致，策略不应直接依赖底层 API 细节。
- 实盘与回测若要共用策略代码，输入对象必须先规范化。
- QMT 的 REST 与 WebSocket 数据格式并不完全一致，必须在数据层统一。

替代方案：

- 在每个策略内部自己调用 `qmt_proxy_sdk`。这会造成重复逻辑和强耦合，不采用。

### 3. 策略层采用“确定性策略接口 + Agent 规划器”的双轨设计

策略层不是单纯“调用一次大模型然后下单”，而是拆成两个可独立测试的部分：

- `StrategyPlugin`
  - `prepare_context(market_context, portfolio_state)`
  - `generate_signal(strategy_context)`
  - `build_trade_plan(signal, portfolio_state)`
- `AgentPlanner`
  - 使用 LangChain / LangGraph
  - 只允许调用白名单工具
  - 输出结构化 JSON：`action`, `symbol`, `thesis`, `confidence`, `risk_notes`, `candidate_orders`

运行时规则：

- 高频行情接收与特征更新是持续运行的确定性流程。
- LLM 只在配置事件触发时运行，例如 bar close、突发告警、人工发起分析请求、组合再平衡窗口。
- AI 输出的结果不能直接成为订单，而是先生成 `TradePlan`。

原因：

- `freqtrade` 的 `IStrategy` 证明了稳定接口对回测、实盘和插件扩展都非常关键。
- 把 AI 作为“分析器/规划器”而不是“执行器”，更容易落地风控、审计和模式切换。
- LangGraph 更适合长流程 Agent 状态机，例如“取行情 -> 读仓位 -> 调工具分析 -> 输出计划 -> 等审批”。

替代方案：

- 每个 tick 都直接调用 Agent。成本高、延迟大、不可控，不采用。
- 仅保留传统量化规则，不引入 Agent。不能满足用户“AI 机器人”目标，不采用。

### 4. 执行层必须采用“TradePlan -> OrderIntent -> BrokerOrder”的硬风控流水线

执行层由以下组件组成：

- `ExecutionPolicyEngine`
  - 仓位上限
  - 单票风险
  - 现金/可用资金校验
  - 交易时段限制
  - A 股约束：最小交易单位、涨跌停、T+1、禁买/禁卖状态
- `ApprovalService`
  - `manual`
  - `semi_auto`
  - `full_auto`
- `BrokerAdapter`
  - 首期 `QmtProxyBrokerAdapter`
  - 封装下单、撤单、查询订单/成交/资产/风控状态
- `ReconciliationWorker`
  - 周期性对账订单、成交、持仓和资金

关键原则：

- 任何 live order 都必须先经过 `ExecutionPolicyEngine`。
- Agent 无权直接调用 `submit_order`。
- 所有订单相关动作都带 `run_id`、`plan_id`、`intent_id`、`broker_order_id` 关联链路。

原因：

- `freqtrade` 的 bot 主循环把策略分析和交易执行分开，这是实盘稳定性的前提。
- 交易系统出问题时，真正需要排查的是“为什么生成计划”“为什么被放行”“为什么券商拒单”，所以链路必须可追踪。

替代方案：

- 让 Agent 直接拿到交易 SDK 工具并下单。风险过高，不采用。

### 5. 通道层借鉴 `RPCManager`，所有入口统一走命令总线和事件总线

通道层不是“多个 UI”，而是多个适配器共享一套命令模型：

- `start_bot`
- `stop_bot`
- `pause_bot`
- `resume_bot`
- `switch_mode`
- `request_analysis`
- `list_positions`
- `list_orders`
- `approve_trade_plan`
- `reject_trade_plan`

输出事件统一为：

- `analysis_ready`
- `plan_pending_approval`
- `risk_rejected`
- `order_submitted`
- `order_filled`
- `runtime_error`

首期通道：

- React 控制台
- Telegram Bot
- Feishu Bot

原因：

- 这和 `freqtrade` 通过 Telegram/API server/Webhook 共享 RPC 的思路一致。
- 统一命令模型后，审批、审计和权限控制只需实现一次。

替代方案：

- 每个通道直接访问数据库或执行层。会导致权限绕过和行为分叉，不采用。

### 6. 运行模式显式建模，回测和实盘复用同一策略与执行契约

定义四种模式：

- `backtest`: 只重放历史数据，不连接真实账户。
- `paper`: 连接真实行情，但订单在本地模拟成交。
- `shadow`: 连接真实行情和真实账户状态，但只生成计划并对比“若执行会怎样”，不下单。
- `live`: 真实下单。

其中：

- 策略层接口不因模式变化而变化。
- 执行层通过模式选择不同 `BrokerAdapter`。
- 通道层必须清晰展示当前模式，并在 `live` 模式下增加额外确认信息。

原因：

- 这是 `freqtrade` dry-run/live 分层思想在股票 AI Agent 场景的扩展。
- `shadow` 模式尤其适合在接入 AI 决策后做实盘前验证。

替代方案：

- 只提供 `paper` 和 `live`。会降低上线前验证质量，不采用。

### 7. 使用事件审计表和可回放日志，而不是只依赖应用日志

至少持久化以下对象：

- `agent_runs`
- `strategy_decisions`
- `trade_plans`
- `order_intents`
- `broker_orders`
- `broker_fills`
- `approval_records`
- `channel_commands`
- `runtime_alerts`

日志之外再保存结构化实体，原因是：

- 需要追踪 Agent 的输入、输出和审批链。
- 需要回放历史行情片段和策略决策进行诊断。
- 需要为后续绩效分析、prompt 评估和策略迭代提供样本。

替代方案：

- 只写文本日志。不可查询、不可统计、不可稳定回放，不采用。

### 8. React 控制台作为首要运营入口，但不承载核心交易逻辑

React 控制台建议增加以下工作区：

- 市场总览：实时行情、订阅状态、数据延迟
- 策略工作区：策略实例、信号、AI 分析结果
- 执行工作区：待审批计划、订单、成交、风控拒绝
- 通道与运行模式：机器人状态、模式切换、机器人日志

前端只调用控制 API 和订阅事件流，不直接接触券商适配器。

原因：

- 控制台适合观察和审批，不应拥有绕过后端风控的能力。
- 当前仓库已有 React 工作台，扩展比另起前端更自然。

## Risks / Trade-offs

- [LLM 幻觉或不稳定输出] → 所有 Agent 输出使用结构化 schema 校验，并在执行层再次做硬风控与审批。
- [实时行情频率高、LLM 推理慢] → 只在配置事件触发 Agent 推理，实时流先进入缓存与特征更新，不逐 tick 调模型。
- [QMT 账户状态与本地状态不一致] → 执行前后都做对账，关键查询直接以券商返回为准。
- [A 股交易规则复杂] → 把 lot size、涨跌停、交易时段、T+1 等约束沉入执行策略，不依赖 prompt 约束。
- [多通道控制带来权限风险] → 所有通道复用统一鉴权和命令模型，区分只读、操作、审批三类权限。
- [仓库复杂度增加] → 首版采用模块化单体，优先把边界做好，再按瓶颈拆分。

## Migration Plan

1. 新增 AI 交易应用骨架、核心领域模型和运行模式状态机，但暂不接入 live order。
2. 打通数据层，先完成 `qmt_proxy_sdk` 历史数据、实时订阅、账户快照的统一抽象。
3. 落地策略层插件接口和 `backtest` / `paper` / `shadow` 模式，先让策略能稳定回放和输出计划。
4. 落地执行层的风控、审批和 `QmtProxyBrokerAdapter`，默认仅启用 `paper` 和 `shadow`。
5. 增加 React 控制台和 Telegram/飞书通道，用于观察、审批和远程控制。
6. 在验证通过后再开放 `live` 模式，并要求显式配置和多重确认。

回滚策略：

- 关闭或移除 AI 交易应用入口即可，不影响现有 `qmt-proxy` 服务和 SDK。
- 即使控制台或 Agent 运行时失效，基础行情与交易代理仍可独立运行。

## Open Questions

- 首期是否只做 A 股股票，还是同时覆盖 ETF、可转债和期货，不同品种的交易约束差异要不要在首版统一抽象。
- LangChain Agent 首期是否允许接入外部资讯/研报/新闻工具，还是仅限行情和账户数据。
- 审批链是只支持“单人审批”，还是支持“双人复核”这类更强约束。
- 是否需要把策略研究和在线运行拆成两个进程，以便后续做 FreqAI 风格的离线训练与在线推理分离。
