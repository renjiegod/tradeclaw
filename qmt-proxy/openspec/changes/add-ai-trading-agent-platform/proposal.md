## Why

`qmt-proxy` 现在已经具备历史行情查询、实时行情订阅和交易执行的基础接口，但还缺少一个面向 AI 交易机器人的上层架构，无法把“行情输入、策略推理、交易执行、远程控制”串成一条可回测、可观测、可控风控的闭环。参考 `freqtrade` 的核心设计，将统一数据访问、策略插件、执行引擎和通道控制分层，可以更快落地一个既能做股票分析、又能逐步演进到全自动交易的 Python + LangChain + React 平台。

## What Changes

- 新增一个以 `qmt_proxy_sdk` 为首个接入源的 AI 交易平台架构，统一接收历史行情、实时行情和账户交易状态。
- 新增一个类似 `freqtrade` 的策略运行时，支持策略插件、结构化 AI 决策输出，以及 `backtest`、`paper`、`shadow`、`live` 四种运行模式。
- 新增一个独立的执行引擎，将交易计划转换为订单意图，并在真正下单前执行风控、审批和账户状态校验。
- 新增一个通道层，支持 React 控制台、Telegram、飞书等远程控制入口复用同一命令总线和通知总线。
- 新增统一的审计与事件记录模型，用于回放策略决策、排查订单问题和追踪 Agent 行为。

## Capabilities

### New Capabilities
- `agent-market-data-layer`: 提供统一的历史行情、实时行情和账户上下文接入层，首期基于 `qmt_proxy_sdk` 实现。
- `agent-strategy-runtime`: 提供策略插件接口、AI Agent 推理流程和多运行模式的统一策略运行时。
- `agent-execution-layer`: 提供交易计划到订单执行的风控、审批、下单和状态对账能力。
- `agent-control-channels`: 提供 React、Telegram、飞书等多通道复用的控制和通知框架。

### Modified Capabilities
- None.

## Impact

- 新增一个 Python 侧 AI 交易服务或模块边界，用于承载数据层、策略层、执行层和通道层。
- 新增 LangChain 相关依赖以及策略状态、审计事件、订单流水等持久化模型。
- React 前端需要扩展为交易 Agent 控制台，而不仅是当前的行情订阅工作台。
- 现有 `libs/qmt_proxy_sdk` 将成为首期数据层和执行层的关键依赖，但不要求修改其公开契约。
- 需要补充回测、纸面交易、实盘风控、远程审批和端到端运行链路的测试方案。
