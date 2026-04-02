# 交易 AI 机器人技术方案（方案 1：单循环 Agent Bot）

> 本方案面向 `python + langchain + react` 技术栈，首期聚焦 A 股场景，数据接入以 `qmt-proxy` 为主，执行模式采用 `paper -> approval-required live -> gated auto live` 的渐进路线。  
> 方案参考了 `freqtrade` 的单循环机器人设计、`Qlib / RD-Agent` 的研究闭环思想，以及前文对量化开源生态的调研结论。

---

## 1. 方案目标

本项目目标是构建一个可持续运行的交易 AI 机器人系统，使其能够：

- 获取历史行情、实时行情、账户与持仓信息。
- 基于规则策略和 AI Agent 做股票分析与交易决策。
- 在严格风控与审批机制下执行纸盘或实盘交易。
- 通过 React 控制台以及飞书、Telegram 等通道进行控制、审批与告警。
- 提供统一的回测、仿真、纸盘、实盘运行模型，尽量减少不同运行模式之间的逻辑分叉。

本方案不追求：

- 高频交易、毫秒级撮合或超低延迟系统。
- 一开始就让 LLM 直接裸连券商 API 下单。
- 纯 RL 直接上实盘。

---

## 2. 设计原则

### 2.1 单循环主控

参考 `freqtrade`，系统以一个稳定的 `TradingWorker` 主循环驱动，每次循环按固定阶段执行：

1. 获取市场与账户状态
2. 更新候选标的池
3. 运行策略分析
4. 运行 Agent 分析
5. 生成结构化订单意图
6. 执行风险校验
7. 进入审批或执行
8. 记录 trace、订单状态和指标

该设计的优点是心智模型清晰，便于先落地 MVP，并且天然适合分钟级或更低频率的股票交易系统。

### 2.2 工具化事实来源

Agent 不允许“想象”价格、持仓、资金等关键事实，所有数值都必须来自工具调用或标准化数据提供器。  
也就是说：

- 行情来自数据层工具
- 持仓来自账户快照工具
- 执行结果来自执行适配器
- 风控结果来自风控引擎

LLM 只负责推理，不负责生成权威数值事实。

### 2.3 订单意图优先

LLM、规则策略、人工操作都不直接生成券商原始 payload，而是统一先生成 `OrderIntent`：

- 标的
- 方向
- 数量或金额
- 订单类型
- 时效
- 策略标签
- 价格参考来源
- 推理说明

只有执行适配器才负责把 `OrderIntent` 翻译为 `qmt-proxy` 或其他券商接口请求。

### 2.4 风控与审批独立于 Agent

风险治理必须是代码层强约束，而不是 prompt 约束。  
系统必须支持：

- 最大仓位限制
- 最大单笔金额
- 单日亏损限制
- 最大回撤限制
- 黑白名单
- Kill switch
- 人工审批超时取消

### 2.5 回测与实盘同构

回测不应另写一套策略逻辑，而应复用同一套 orchestrator、策略接口、风控接口和订单意图模型，只替换底层运行时：

- 实盘：`LiveDataProvider + LiveExecutionAdapter`
- 纸盘：`LiveDataProvider + PaperExecutionAdapter`
- 回测：`HistoricalDataProvider + SimulatedBrokerAdapter`

这样可以最大限度保证研究、仿真、纸盘、实盘之间的行为一致性。

---

## 3. 总体架构

### 3.1 逻辑分层

系统按四层组织：

1. 数据层
2. 策略层
3. 执行层
4. 通道层

同时引入一个位于中心的主控层：

- Orchestrator / TradingWorker

### 3.2 总体结构图

```text
                    +-------------------------+
                    |     React 控制台        |
                    +-----------+-------------+
                                |
                    +-----------v-------------+
                    |  Channel Gateway / API  |
                    | Feishu / Telegram / UI  |
                    +-----------+-------------+
                                |
                    +-----------v-------------+
                    |  TradingWorker / Loop   |
                    | Orchestrator + State    |
                    +-----+-----------+-------+
                          |           |
                +---------v--+     +--v----------------+
                | Strategy Hub|     |  Risk Governance |
                | rules+agent |     | veto / scale     |
                +------+------|     +--------+---------+
                       |                    |
                 +-----v--------------------v-----+
                 |       Order Intent Layer       |
                 +---------------+----------------+
                                 |
                      +----------v-----------+
                      |   Execution Adapter  |
                      | live / paper / sim   |
                      +----------+-----------+
                                 |
      +--------------------------v---------------------------+
      | Data Layer: qmt-proxy REST + WS/gRPC + cache + PIT   |
      +------------------------------------------------------+
```

---

## 4. 核心运行模式

系统支持三种核心运行模式。

### 4.1 Live

- 数据来自实时行情与账户快照
- 订单进入真实执行适配器
- 默认要求审批或更严格风控

### 4.2 Paper

- 数据来自实时行情与账户快照
- 订单进入纸盘执行适配器
- 不真正下单，但完整记录模拟成交、持仓、收益与风控轨迹

### 4.3 Backtest

- 数据来自历史数据重放
- 订单进入模拟撮合器
- 使用历史时钟和历史账户状态推进
- 输出策略报告、交易明细、回撤与 trace

---

## 5. 数据层设计

### 5.1 设计目标

数据层负责将 `qmt-proxy` 和未来其他数据源转换为统一的数据接口，供策略、Agent、执行和回测共用。

### 5.2 首期接入：qmt-proxy

考虑到当前能力边界：

- `qmt_proxy_sdk` 适合接 REST 查询
- 实时推送需要直接接 `qmt-proxy` 的 WebSocket 或 gRPC 流

因此数据层建议分为两个面：

- 查询面：历史行情、快照、账户、持仓、委托查询
- 流式面：实时订阅、行情推送、断线重连、心跳管理

### 5.3 数据层子模块

#### `HistoricalDataProvider`

职责：

- 提供历史 K 线、tick、分时、财务等查询能力
- 支持按时间范围和标的查询
- 为回测和研究提供历史切片

#### `RealtimeMarketFeed`

职责：

- 维护实时订阅连接
- 管理订阅列表
- 处理断线重连、心跳和限流
- 向主循环推送新的市场事件

#### `PortfolioSnapshotProvider`

职责：

- 获取账户总览
- 获取持仓
- 获取委托与成交
- 提供策略与风控所需的账户上下文

#### `UniverseProvider`

职责：

- 生成候选股票池
- 支持固定池、板块池、自选池、策略池
- 类似 `freqtrade` 的 pairlist manager，但面向股票 universe

#### `MarketDataNormalizer`

职责：

- 统一 symbol、market、timezone、timestamp、adjust_type
- 输出系统内部标准模型
- 附带 provenance 信息，便于 trace 和审计

### 5.4 推荐内部数据模型

建议统一以下模型：

- `InstrumentKey`
- `Bar`
- `Tick`
- `Quote`
- `OrderBookSnapshot`（如未来需要）
- `AccountSnapshot`
- `PositionSnapshot`
- `OrderSnapshot`
- `TradeFill`

所有上层模块只能依赖这些内部模型，不直接依赖 `qmt-proxy` 的原始字段。

---

## 6. 策略层设计

### 6.1 设计目标

策略层的职责不是直接下单，而是输出：

- 市场分析结果
- 候选信号
- 候选订单意图
- 策略解释与标签

### 6.2 策略层拆分

#### `SignalStrategy`

面向规则或传统量化逻辑：

- 技术指标策略
- 板块轮动
- 多因子打分
- 风险预算驱动的候选排序

输出：

- `Signal`
- `SignalScore`
- `StrategyTag`

#### `AgentStrategy`

面向 LLM / Agent 分析逻辑：

- 对候选信号做过滤
- 结合新闻、公告、市场上下文做二次判断
- 输出理由、置信度、建议仓位修正

首期建议不要让 Agent 单独承担“从零到一生成交易信号”的全部责任，而是让其作为规则策略的增强器。

#### `PortfolioAllocator`

负责把策略信号映射为仓位建议：

- 每个标的目标仓位
- 单标的最大暴露
- 组合层约束

### 6.3 推荐输出模型

策略层统一输出如下对象：

- `Signal`
- `StrategyDecision`
- `OrderProposal`
- `StrategyRationale`

之后由执行层把 `OrderProposal` 转为 `OrderIntent`。

---

## 7. 执行层设计

### 7.1 设计目标

执行层负责“把候选交易决策转为可控的执行动作”，同时隔离 Agent 与券商接口。

### 7.2 核心组件

#### `OrderIntentValidator`

职责：

- 校验字段完整性
- 校验数量与金额互斥规则
- 校验引用价格是否有 provenance
- 校验 TIF、订单类型等取值

#### `RiskEngine`

职责：

- 校验仓位上限
- 校验单笔金额
- 校验总暴露
- 校验单日亏损和回撤
- 执行黑白名单规则
- 输出 pass / scale / veto

#### `ApprovalGate`

职责：

- 对 live 高风险订单进入审批队列
- 等待飞书 / Telegram / UI 审批
- 超时自动取消

#### `ExecutionAdapter`

统一接口，至少包含：

- `submit_intent(intent)`
- `cancel_order(order_id)`
- `query_order_status(order_id)`
- `sync_account_state()`

具体实现：

- `QmtLiveExecutionAdapter`
- `PaperExecutionAdapter`
- `SimulatedBrokerAdapter`

### 7.3 推荐订单流程

```text
Signal / Proposal
-> OrderIntent
-> Validation
-> RiskEngine
-> ApprovalGate
-> ExecutionAdapter
-> Fill / OrderState update
-> Trace & Metrics
```

---

## 8. 通道层设计

### 8.1 定位

通道层不负责交易核心逻辑，只负责：

- 控制入口
- 查询入口
- 审批入口
- 通知入口

### 8.2 设计方式

参考 `freqtrade` 的 `RPCManager`，建议采用插件式通道管理器：

- `ChannelManager`
- `FeishuHandler`
- `TelegramHandler`
- `WebhookHandler`
- `WebConsoleHandler`

### 8.3 典型能力

- `/start` 启动策略循环
- `/stop` 停止新开仓
- `/status` 查看当前状态
- `/positions` 查看持仓
- `/approve <intent_id>` 审批订单
- `/kill` 启动全局 kill switch
- 告警推送：回撤、断连、订单拒绝、审批超时

---

## 9. 主循环设计

### 9.1 主循环阶段

方案 1 推荐采用固定阶段主循环：

1. `load_context`
2. `refresh_market_state`
3. `refresh_portfolio_state`
4. `build_universe`
5. `run_signal_strategies`
6. `run_agent_strategies`
7. `build_order_intents`
8. `run_risk_checks`
9. `await_approval_if_needed`
10. `dispatch_orders`
11. `sync_fills_and_positions`
12. `persist_trace_and_metrics`

### 9.2 触发方式

首期支持两种触发：

- 定时触发：每分钟 / 每 5 分钟 / 每小时
- 事件触发：收到新 bar 或关键行情变化时触发

首版建议以定时触发为主，降低复杂度。

---

## 10. 多 Agent 平台化设计

### 10.1 设计目标

在保留方案 1 单循环主控优势的前提下，平台需要支持：

- 在界面上创建多个独立 `AgentInstance`
- 多个实例同时运行，彼此配置和状态隔离
- 每个实例既可以是 `single-agent`，也可以是 `multi-role`
- 用户可为每个实例配置模型、策略、运行模式、风控和通道
- 平台统一管理实例生命周期、资源配额、审计和告警

该设计采用“平台实例 + 角色编排模式”：

- 平台层对用户暴露的是 `AgentInstance`
- 实例内部通过 orchestrator 决定是单 Agent 运行还是多角色协作

### 10.2 设计方案选择

系统支持的目标形态不是“仅一个 Agent 进程”，而是：

1. 平台允许同时存在多个 `AgentInstance`
2. 每个 `AgentInstance` 都有独立配置
3. 每个 `AgentInstance` 内部可切换运行模式：
  - `single-agent`
  - `multi-role`

其中推荐默认模式为：

- 外部：多实例并发
- 内部：单 Agent 与多角色协作并存

这样既保留 MVP 的简单性，也为后续复杂协同留下空间。

### 10.3 平台核心对象

#### `AgentTemplate`

用于定义预设模板，例如：

- 单 Agent 趋势跟踪模板
- 单 Agent 事件驱动模板
- 多角色研究-交易模板
- 多角色研究-风险-执行模板

模板负责提供默认配置与页面初始化项。

#### `AgentInstance`

表示用户在界面上创建的一个独立运行实例。  
每个实例拥有：

- 唯一标识
- 基础信息
- 模型配置
- 策略配置
- 运行模式
- 风控配置
- 通道配置
- 生命周期状态

#### `AgentRuntime`

表示实例的实际运行时容器，负责：

- 初始化上下文
- 维护实例主循环
- 管理角色执行
- 执行 trace 记录
- 接收平台调度命令

#### `AgentRole`

表示实例内部的逻辑角色。建议支持：

- `research`
- `signal`
- `portfolio`
- `risk`
- `execution`
- `ops`

对于 `single-agent` 模式，可以只启用一个综合角色；对于 `multi-role` 模式，则由 orchestrator 编排多个角色协同。

#### `RuntimeScheduler`

平台级调度器，负责：

- 启动、暂停、停止、重启实例
- 控制实例并发数
- 监控异常与超时
- 统一分配共享资源

### 10.4 单实例内部运行模式

#### `single-agent`

特点：

- 一个 Agent 完成分析、决策和解释
- 风控与执行仍由独立代码模块处理
- 配置简单，适合快速创建和试验

适合：

- MVP
- 简单策略
- 较低并发的研究与纸盘场景

#### `multi-role`

特点：

- 内部拆分多个角色 Agent
- 每个角色有独立提示词、模型、工具权限和输出边界
- 由实例内 orchestrator 合并结果并形成最终决策

建议角色职责：

- `research`：研究市场上下文、新闻、公告、主题
- `signal`：结合规则策略和工具结果生成候选信号
- `portfolio`：生成仓位与组合建议
- `risk`：给出风险意见，但最终 veto 仍由代码级 RiskEngine 执行
- `execution`：负责解释执行意图，不直接持有真实券商凭证
- `ops`：处理健康检查、告警和运行建议

### 10.5 实例状态模型

每个 `AgentInstance` 建议拥有如下生命周期状态：

- `draft`
- `configured`
- `running`
- `paused`
- `stopping`
- `stopped`
- `error`
- `archived`

每个 `AgentRole` 可额外拥有运行态信息：

- 最近一次运行时间
- 最近一次输出状态
- 工具调用次数
- 模型 token 消耗
- 最近错误信息

### 10.6 实例配置模型

建议将实例配置拆成多个分组。

#### 基础信息

- 名称
- 描述
- 标签
- 模板来源
- 所属分组或命名空间

#### 运行模式

- `single-agent` / `multi-role`
- `backtest` / `paper` / `live`
- 触发方式：定时 / 事件
- 调度周期

#### 模型配置

- 提供商
- 模型名
- temperature
- 最大上下文
- 是否允许工具调用
- 工具白名单

#### 策略配置

- 策略模板
- 股票池范围
- 周期配置
- 因子参数
- 指标参数
- Agent 参与阶段

#### 执行配置

- 执行适配器
- 账户绑定
- 审批要求
- 默认订单类型
- 默认滑点容忍

#### 风控配置

- 单笔限额
- 单标的仓位
- 单日亏损限制
- 最大回撤
- Kill switch 策略
- 黑白名单

#### 通道配置

- 飞书机器人
- Telegram 机器人
- webhook
- 告警等级
- 审批通道

### 10.7 多实例资源隔离

多个实例同时运行时，需要明确隔离边界：

- 模型上下文隔离
- 实例配置隔离
- 执行账户权限隔离
- trace 与日志隔离
- 审批记录隔离

共享但应统一管理的资源：

- 行情连接
- 历史数据缓存
- 工具注册中心
- 模型调用配额
- 任务调度器

### 10.8 多实例并发策略

首期建议采用相对简单但可靠的方式：

- 一个 `AgentInstance` 对应一个独立 runtime
- 多实例由平台统一调度
- 运行时之间不共享 prompt 状态
- 平台共享市场数据缓存和订阅连接

推荐并发控制策略：

- 限制同时运行实例数
- 限制每实例并发工具调用数
- 限制每实例模型速率
- 限制每实例最大运行时长

### 10.9 模板化与快速创建

为降低配置复杂度，建议平台支持“模板 + 自定义参数”模式。

典型模板包括：

- `Single Agent / Trend Following`
- `Single Agent / Event Driven`
- `Multi Role / Research + Trader`
- `Multi Role / Research + Trader + Risk`
- `Backtest Only / Signal Evaluation`

用户流程：

1. 选择模板
2. 填写基础信息
3. 配置模型与策略
4. 配置风控与通道
5. 保存为草稿或直接启动

### 10.10 多 Agent 平台的风险点

#### 风险 1：实例之间资源竞争

缓解：

- 调度器控制并发与配额
- 将高成本模型与实时交易实例优先级分离

#### 风险 2：实例配置过于复杂

缓解：

- 以模板驱动
- 高级配置默认折叠
- 提供配置校验与启动前预检查

#### 风险 3：多角色协作不可控

缓解：

- 角色边界清晰
- 工具权限白名单
- 所有最终执行仍统一走 `OrderIntent -> Risk -> Approval -> Adapter`

---

## 11. 回测子系统设计

### 11.1 设计目标

回测的目标不是写第二套交易系统，而是让同一套决策接口运行在“历史时间 + 历史数据 + 模拟成交”环境中。

### 11.2 总体原则

- 复用同一套 orchestrator、策略、风控、订单意图模型
- 替换 live 运行时依赖
- 禁止未来信息泄漏
- 模拟 A 股关键成交约束

### 11.3 回测核心组件

#### `ReplayClock`

职责：

- 推进模拟时间
- 控制每次循环的可见时间边界
- 支持 bar-driven 模式

首版建议只实现 `bar-driven` 回测。

#### `HistoricalDataProvider`

职责：

- 按 `as_of_time` 提供历史可见数据
- 只返回当前时刻以前的数据
- 支持 lookback 窗口

#### `BacktestPortfolioState`

职责：

- 维护现金、持仓、挂单、成交、净值曲线
- 记录 realized / unrealized PnL
- 为风控和报告提供基础状态

#### `SimulatedBrokerAdapter`

职责：

- 模拟接收订单
- 模拟成交和拒单
- 计算滑点、费用和税费
- 输出订单状态和成交回报

#### `BacktestReporter`

职责：

- 汇总回测结果
- 输出交易明细
- 输出曲线和指标
- 保存 trace 用于复盘

### 11.4 回测流程

```text
加载历史数据
-> 初始化回测账户
-> ReplayClock 推进到当前 bar
-> 构造当前可见市场快照
-> 策略层运行
-> Agent 读取当前时点可见工具数据
-> 生成 OrderIntent
-> 风控校验
-> SimulatedBroker 模拟成交
-> 更新账户与持仓
-> 记录指标与 trace
-> 进入下一个 bar
```

### 11.5 Agent 回测建议

首期建议分层实现，不要一步到位：

#### 模式 A：纯规则回测

- 不启用 Agent
- 验证主循环、风控和模拟成交是否正确

#### 模式 B：Agent 辅助回测

- 规则策略先给候选信号
- Agent 只负责过滤、解释和打分

#### 模式 C：完整 Agent 回放

- Agent 深度参与每次决策
- 成本高、不可复现性更强

MVP 建议先做模式 A 和模式 B。

### 11.6 A 股回测必须考虑的规则

至少建议模拟：

- T+1
- 涨跌停约束
- 停牌
- 最小交易单位
- 手续费
- 印花税
- 滑点
- 部分成交或不成交
- 午间休市和交易日历

如果这些规则缺失，回测结果很容易偏乐观。

### 11.7 防未来函数设计

回测子系统必须支持：

- `as_of_time` 强约束
- 所有工具按回测时间切片
- 特征计算只使用历史窗口
- Agent 上下文不包含未来数据
- 结果缓存带时间边界

---

## 12. 持久化与审计

### 12.1 设计目标

系统必须支持完整的 append-only trace，用于回放、审计、调试与合规留痕。

### 12.2 建议持久化对象

- `Run`
- `RunPhase`
- `ToolInvocation`
- `StrategyDecision`
- `OrderIntent`
- `RiskDecision`
- `ApprovalRecord`
- `OrderRecord`
- `FillRecord`
- `PortfolioSnapshot`
- `MetricPoint`

### 12.3 存储建议

首期可采用：

- `SQLite` 或 `Postgres` 持久化核心表
- 文件系统保存报告、曲线、导出 JSON、HTML 分析结果

如需高频 trace 或指标流，可后续引入：

- `Redis`
- `ClickHouse`
- 对象存储

---

## 13. 风控设计

### 13.1 前置规则

首期建议硬编码或配置化支持：

- 最大单笔下单金额
- 单标的最大仓位
- 单策略最大仓位
- 总账户最大暴露
- 黑名单 / 白名单

### 13.2 会话级规则

- 单日亏损达到阈值后停止新开仓
- 最大回撤超过阈值后停止策略
- 连续亏损达到阈值后触发冷却

### 13.3 全局规则

- Kill switch
- 停止所有新开仓
- 必要时取消未成交订单

### 13.4 审批规则

以下情形建议必须审批：

- 第一阶段所有 live 订单
- 超过设定金额阈值的订单
- 高波动时段的订单
- Agent 高不确定性的订单

---

## 14. 前端后台管理设计

### 14.1 前端定位

前端不应被设计成“交易终端大屏”，而应设计为：

- AI 交易平台后台管理
- Agent 实例管理中心
- 策略与运行配置中心
- 审批与风控控制台
- 回测与运行分析工作台

### 14.2 技术方案

前端建议采用：

- React
- Ant Design
- 路由与权限体系
- 图表组件用于收益、回撤、运行指标展示

整体信息架构应围绕“实例管理、运行追踪、审批治理、研究分析”展开。

### 14.3 视觉设计语言

主题风格参考示例图片，采用“暖中性色、极简、高留白”的后台风格，而不是高饱和交易大盘风格。

建议视觉关键词：

- `Warm Neutral`
- `Minimal Premium`
- `Research-first`
- `Calm Dashboard`

建议色彩方向：

- 页面背景：暖灰白、米白
- 卡片背景：纯白
- 主文字：近黑
- 次级文字：中灰
- 分割线：浅灰
- 强调色：克制的暖橙或金橙
- 成功色：低饱和绿色
- 风险色：深红或棕红，避免荧光警报风格

建议布局特征：

- 顶部导航与左侧菜单并存
- 页面保持较大留白
- 卡片化分区清晰
- 图表与表单保持简洁边界
- 使用较少阴影、更多留白和分割线

### 14.4 前端页面结构

建议后台包含以下一级模块：

1. `Dashboard`
2. `Agent Instances`
3. `Create Agent`
4. `Strategies`
5. `Backtests`
6. `Approvals`
7. `Models`
8. `Channels`
9. `System`

### 14.5 Dashboard 设计

Dashboard 重点展示：

- 运行中的 Agent 数量
- 今日收益概览
- 风险告警数量
- 待审批订单数
- 最近运行事件
- 系统健康状态

推荐卡片包括：

- `Active Agents`
- `Pending Approvals`
- `Daily PnL`
- `Risk Alerts`
- `Backtest Tasks`
- `System Health`

### 14.6 Agent 列表页

列表页是平台核心入口，建议展示：

- Agent 名称
- 模板
- 运行模式
- 当前状态
- 绑定模型
- 绑定策略
- 账户 / 标的范围
- 最近运行时间
- 快捷操作：启动 / 暂停 / 停止 / 克隆 / 删除

建议支持：

- 状态筛选
- 标签筛选
- 模型筛选
- 运行模式筛选
- 批量启停

### 14.7 Agent 创建页

该页面建议采用 Ant Design 的分步表单或 Tabs 表单。

推荐步骤如下：

1. 选择模板
2. 填写基础信息
3. 配置运行模式
4. 配置模型
5. 配置策略
6. 配置执行与风控
7. 配置通道
8. 预览并创建

页面操作建议支持：

- `Save Draft`
- `Create`
- `Create and Run`
- `Save as Template`

### 14.8 Agent 详情页

详情页建议采用“概览 + 多标签页”的后台模式。

概览区展示：

- 基础信息
- 当前状态
- 运行模式
- 当前模型
- 当前策略
- 快捷操作按钮

标签页建议包括：

- `Overview`
- `Roles`
- `Runs`
- `Orders`
- `Risk`
- `Logs`
- `Metrics`
- `Config`

如果实例为 `multi-role` 模式，则 `Roles` 标签页应展示：

- 每个角色的模型
- 工具权限摘要
- 运行状态
- 最近输出
- token 消耗
- 最近错误

### 14.9 Approvals 页面

审批页建议突出“少而关键”的决策信息，不做成复杂交易终端。

每条待审批记录建议展示：

- Agent 实例
- 订单意图摘要
- 风险原因
- 置信度与解释摘要
- 审批倒计时
- `Approve / Reject / Delay`

### 14.10 Backtests 页面

回测页建议支持：

- 创建回测任务
- 查看回测状态
- 查看回测报告
- 对比多个回测结果
- 查看收益曲线、回撤、交易列表

首版无需做成复杂量化研究平台，但应保留对比能力。

### 14.11 前端权限与安全

后台前端应考虑最基本的权限模型：

- 查看权限
- 编辑配置权限
- 启停实例权限
- 审批权限
- 系统配置权限

敏感操作需二次确认，包括：

- 启用 live
- 关闭 kill switch
- 修改高风险配置
- 直接批准高风险订单

### 14.12 前端与后端边界

前端只负责：

- 展示实例、状态、日志和结果
- 提交配置变更
- 触发启停、审批等控制动作

前端不负责：

- 保存模型凭证
- 执行风险判断
- 直接与券商接口通讯

所有关键动作均应通过平台 API 进入后端统一处理。

---

## 15. 技术栈建议

### 15.1 后端

- Python 3.12+
- FastAPI
- LangChain
- Pydantic
- SQLAlchemy
- SQLite / Postgres
- Redis（可选）

### 15.2 前端

- React
- Ant Design
- 状态管理与图表组件
- 审批队列、订单追踪、回测报告展示
- 后台管理型路由

### 15.3 集成

- `qmt_proxy_sdk` 用于 REST 查询
- `qmt-proxy` WebSocket / gRPC 用于实时行情
- 飞书 / Telegram Bot API

---

## 16. MVP 范围

### 16.1 一期必须完成

- 单账户
- A 股
- `qmt-proxy` 数据接入
- 单循环主控
- 多个 `AgentInstance` 的创建、编辑、启停与并发运行
- 模板驱动的 Agent 创建流程
- `single-agent` 模式
- 预留 `multi-role` 模式的数据模型与页面结构
- 规则策略 + Agent 辅助分析
- Paper 模式
- 基础回测
- 基础风控
- 基于 `Ant Design + React` 的后台管理最小版
- Agent 列表页、创建页、详情页、审批页
- 飞书或 Telegram 其中一个通道

### 16.2 一期暂不做

- 多账户
- 高频交易
- 多市场统一接入
- 复杂组合优化
- 完整多 Agent 并行研发闭环
- RL 实盘
- 可视化工作流编排器
- 完整拖拽式 DAG 设计器
- 高级主题系统与品牌化设计器

---

## 17. 分阶段演进路线

### 阶段 1：平台底座

- 打通历史数据、实时数据、账户快照
- 完成 paper 执行
- 完成基础回测
- 完成 trace 与风控
- 完成多实例创建、配置和启停
- 完成后台管理最小版界面

### 阶段 2：Agent 增强

- 引入 Agent 辅助过滤和解释
- 增加审批流
- 增加更丰富的策略标签和分析输出
- 引入 `multi-role` 模式的首批角色组合

### 阶段 3：受控 live

- 启用 live adapter
- 接入人工审批
- 增加更完整的订单与异常告警
- 完善实例级权限和审批治理

### 阶段 4：平台化扩展

- 多账户
- 多策略
- producer / consumer 信号复用
- 更强的控制台和回测分析能力
- 更丰富的模板体系
- 更细粒度的角色编排
- 更完整的运营与审计能力

---

## 18. 主要风险与缓解

### 风险 1：Agent 幻觉导致错误交易

缓解：

- 所有市场事实必须工具化
- LLM 不直接输出券商 payload
- 风控和审批独立于 LLM

### 风险 2：回测与实盘表现偏差过大

缓解：

- 统一订单意图模型
- 统一风控规则
- 增强 A 股成交模拟
- 记录 paper 与 backtest 偏差

### 风险 3：数据层不统一导致后期无法扩展

缓解：

- 首期就定义内部标准模型
- 不向上层暴露 `qmt-proxy` 原始接口

### 风险 4：系统复杂度过早膨胀

缓解：

- 首版坚持单循环方案
- 不急于服务化拆分
- 用接口隔离未来演进方向

### 风险 5：多实例并发导致资源和权限失控

缓解：

- 增加调度器和实例级配额
- 执行账户与审批权限按实例隔离
- 对 live 实例设置更严格的启停和审批规则

### 风险 6：后台界面功能过多导致难用

缓解：

- 以 Agent 实例为核心对象
- 用模板与分步表单降低配置门槛
- 把高级配置折叠到二级页面或高级设置中

---

## 19. 结论

本方案选择以 `freqtrade` 风格的单循环机器人架构为基础，结合 AI Agent 的分析能力、强风控、结构化订单意图、同构回测运行时以及多 Agent 平台化管理能力，形成一个适合首期落地并可持续演进的交易 AI 机器人技术方案。

其核心价值在于：

- 先把“数据可信、决策可追踪、执行可控制、回测可复现”打牢
- 再逐步增强 Agent 能力、实例管理能力和产品化能力
- 避免一开始就走向“黑盒 LLM 直接全自动交易”的高风险路线

该方案适合作为 Tradeclaw 的首版系统设计基线，并可在后续平滑演进为更强的多策略、多账户、多通道、多实例协同平台。