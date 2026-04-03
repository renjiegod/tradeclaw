# 从 `run_cycle` 到自主 Agent：参考 Claude Code 的技术设计建议

> 目的：在保留 Tradeclaw「事实来自工具 / 数据层、订单经风控与审批」的前提下，把当前**单轮流水线**（`TradingWorker.run_cycle`）演进为可**多轮工具驱动、可扩展技能与记忆、可注入策略与配置**的自主决策运行时。  
> 参考源码：本地 Claude Code 仓库（示例路径 `/Users/renjiegod/code/ClaudeCode`）。其内部文档对 agent loop 已有系统说明，本文在此基础上提炼可迁移模式并落到 Tradeclaw 语境。

---

## 1. Claude Code 里和本议题直接相关的材料

| 材料 | 路径（相对 Claude Code 根目录） | 对 Tradeclaw 的启发 |
|------|--------------------------------|----------------------|
| Agent loop 总览 | `docs/08-agent-loop.md` | **显式状态机**、消息协议、工具结果回灌、终止语义 |
| 核心循环 | `src/query.ts`（`query` / `queryLoop`） | `while (true)` + `State`，非递归多轮推进 |
| 模型与消息修复 | `src/services/api/claude.ts` | `tool_use` / `tool_result` 成对、流式与恢复 |
| 工具编排 | `src/services/tools/toolOrchestration.ts`、`StreamingToolExecutor.ts` | 并行/串行、流式重叠执行 |
| 单工具执行 | `src/services/tools/toolExecution.ts`（`runToolUse`） | schema 校验 → 语义校验 → hooks → 权限 → `call` → 映射为 `tool_result` |
| Subagent 复用同一内核 | `src/tools/AgentTool/runAgent.ts` | **专用工具池 + 专用 system prompt**，仍调用同一 `query()` |
| System prompt 分层 | `src/utils/systemPrompt.ts`（`buildEffectiveSystemPrompt`） | 覆盖/追加/默认优先级，便于注入「交易策略说明」 |
| Skills 加载 | `src/skills/loadSkillsDir.ts`、`.claude/skills` 约定 | 目录化、frontmatter、热更新（`skillChangeDetector`） |
| Memory / 附件 | `src/utils/attachments.ts`（如 `startRelevantMemoryPrefetch`） | 在轮次间注入结构化上下文，而非塞进一条 user 文本 |

以下设计建议默认你已阅读 `docs/08-agent-loop.md` 中的「单轮 8 阶段」「工具结果必须回到消息流」两节。

---

## 2. 当前 Tradeclaw 与 Claude Code 的结构性差异

| 维度 | Tradeclaw `run_cycle`（现状） | Claude Code `queryLoop` |
|------|------------------------------|-------------------------|
| 控制流 | 固定阶段 DAG，每 tick 跑一轮 | 模型输出驱动是否继续；多轮直到 terminal |
| 模型调用 | 主要在 `agent_strategy.review` 单次生成 | 每轮流式调用，可能多轮 |
| 与环境的交互 | `data_provider` / `execution_adapter` 由代码直接调用 | 通过 **tool_use → tool_result** 进入消息历史 |
| 扩展点 | 替换策略类、工厂注册数据源 | 工具池、MCP、skills、hooks、子 agent |

结论：**不必把 Tradeclaw 整段换成 TS 式 loop**，但值得吸收三类思想：

1. **统一「观察–行动–再观察」协议**（消息或等价结构体，而非散落的全局调用）。  
2. **工具链：校验 / 权限 / 错误可回灌模型**（与现有风控、审批对齐）。  
3. **上下文分层：system（角色+策略+合规）+ 可变记忆 + 本轮事实快照**。

---

## 3. 建议的总体架构：双轨或分阶段演进

### 3.1 轨道 A（保守）：保留 `run_cycle`，只把「Agent 段」换成小型 tool loop

- **风控、审批、下单**仍在 Python 流水线中顺序执行（与现在一致）。  
- **仅**将 `LangChainAgentStrategy.review` 扩展为：在单 tick 内允许 **N 轮**「模型 ↔ 工具」，工具只读行情/账户/研报等，最终仍要输出结构化 `reviews` 或 `OrderIntent` 草案。  
- 优点：改动面小，实盘安全边界清晰。  
- 缺点：模型无法在「下单后」再开一轮（除非把整个下单也包进受控工具，见轨道 B）。

### 3.2 轨道 B（目标形态）：`TradingAgentRuntime` 与 `run_cycle` 并列或内嵌

- 抽象一个 **`AgentTurnState`**（类比 `query.ts` 的 `State`）：`messages`（或 transcript）、`turn_count`、`tool_use_context`（数据源、执行器、审批门引用、`AbortSignal`）。  
- 外层仍是平台 **tick**：每 tick 调用一次 `runtime.step()` 或 `run_until_quiescent(max_turns)`。  
- **终止条件**显式化：`completed` / `max_turns` / `risk_blocked` / `approval_pending` / `fatal_error`（对齐 Claude Code 的 `Terminal` 思想）。

推荐 **先 A 后 B**：A 验证工具与 prompt 分层；B 再把「提交意图」也纳入工具，由统一运行时调度。

---

## 4. 工具（Tools）：设计要点（对照 `toolExecution.ts`）

1. **Schema 优先**  
   - 每个工具 JSON Schema + 名称空间（如 `trade.qmt.get_positions`），与现有 `OrderIntent` 校验器同一哲学。  
2. **错误不进异常栈顶断循环，而是 tool_result**  
   - 参数错、数据源超时、权限拒绝 → 结构化错误文本回灌，让模型改参数或放弃（Claude Code 明确采用此方式）。  
3. **Pre / Post hooks**  
   - **Pre**：审计日志、速率限制、只读模式强制。  
   - **Post**： redact 敏感字段、写入 trace_store。  
4. **与风控/审批的边界**  
   - 建议：**工具层不直接成交**；工具可返回「建议 intent」或「模拟结果」，真正 `submit_intent` 仍走 `RiskEngine` + `ApprovalGate`（可由「finalize」类工具触发固定代码路径，避免模型绕过）。  
5. **并行**  
   - 只读工具可并行（参考 `toolOrchestration` 的并发安全分批）；涉及账户或订单状态写操作的工具必须串行或加锁。

---

## 5. Skills：在 Tradeclaw 中的落地方向（对照 `loadSkillsDir.ts`）

Claude Code 的 skills 本质是 **带元数据的指令包**（目录 + `SKILL.md` + frontmatter），运行时注入上下文或注册斜杠命令。

迁移建议：

1. **目录约定**（可与现有 OpenSpec / Cursor skills 并存）：  
   - 例如 `tradeclaw/skills/<name>/SKILL.md` 或实例级 `.tradeclaw/skills/`。  
2. **解析层**：frontmatter 声明 `description`、`allowed_tools`、可选 `triggers`。  
3. **注入时机**（对齐 `queryLoop` 的 prefetch 思想）：  
   - **tick 开始**：把与当前 `template_id` / `universe` 相关的 skill 摘要注入 system 或首条 user 块。  
   - **按需检索**：第二轮再加载全文（控制 token，类似 memory/skill prefetch）。  
4. **与「策略」的关系**：  
   - **信号策略**可保持代码；**skills** 承载「如何解读信号、如何填参数、合规话术」等人读+模型读Procedure，减少把业务写死在 `SYSTEM_PROMPT` 常量里。

---

## 6. 记忆（Memory）：分层建议（对照 attachments / team memory）

Claude Code 将记忆、附件、队列通知作为 **消息流的补充块**，而不是隐藏全局变量。

建议在 Tradeclaw 分三层：

| 层级 | 内容 | 持久化 | 注入方式 |
|------|------|--------|----------|
| **会话 / 实例记忆** | 本 agent 实例近期决策摘要、用户偏好 | SQLite / 文件 / Redis | 每轮 system 或单独 `memory` 块 |
| **标的 / 市场记忆** | 某 symbol 近期事件、策略标签 | 与实例或全局 research store 绑定 | 工具 `memory.search` 或预取 |
| **本轮事实快照** | 当前 `MarketContext`、`AccountSnapshot` | 不必长期存 | 与现在一样，作为 user 结构化输入或 tool_result |

**关键原则**：模型看到的「数字」仍应来自工具或快照，**记忆层只存摘要与引用**，避免与「工具化事实来源」设计冲突。

---

## 7. 交易策略与配置注入（对照 `buildEffectiveSystemPrompt` + `runAgent`）

参考 `systemPrompt.ts` 的优先级链：**override → agent 专用 → custom → default**，以及 proactive 模式下 **append** 而非 replace。

Tradeclaw 可映射为：

1. **平台默认**：合规 + 工具使用规范 + 输出 schema。  
2. **模板 / 实例配置**：`AgentInstanceConfig` 扩展字段，如 `strategy_brief_path` 或内联 `trading_instructions`。  
3. **实例覆盖**：创建实例时 API 传入 `system_prompt_append` 或绑定某 skill 包。  
4. **子策略 / 子 agent**（可选）：研究 agent 只读工具集；交易 agent 窄工具集；共享同一份 **Transcript 协议**（与 `runAgent` 复用 `query` 同理，Python 侧复用同一 `AgentTurnState` 类型）。

---

## 8. 与现有 `run_cycle` 阶段的对齐表

便于实现时分任务：

| `run_cycle` 阶段 | 自主 agent 化后的可能形态 |
|------------------|---------------------------|
| `refresh_*` / `build_universe` | 变为 **工具**（`get_market_context` 等），由模型决定何时拉取；或 tick 开头仍由代码预取，结果作为首条 observation |
| `run_signal_strategies` | 保留为代码，结果作为 observation；或封装为只读工具 |
| `run_agent_strategies` | **替换为多轮 tool loop**，终点仍为结构化 approve / intent |
| `build_order_intents` | 可由模型 + 工具草案，**固定代码**做最终组装与校验 |
| `run_risk_checks` / `await_approval` / `dispatch` | **保留在代码路径**；模型仅通过受控 API 触发 |

---

## 9. 实现顺序建议（里程碑）

1. **Transcript 模型**：定义 `Message` / `ToolCall` / `ToolResult` 的 Python 结构，与现有 `trace_store` 对齐。  
2. **只读工具集**：行情、账户、持仓、universe；接现有 `TradingDataProvider`。  
3. **小型 loop**：`max_turns` + `AbortSignal`；单测模拟错误回灌。  
4. **Skills 加载器**：读 markdown + frontmatter，拼入 system。  
5. **记忆 MVP**：实例级 append-only 摘要，每轮注入 token 上限。  
6. **与审批联动**：工具 `propose_order` → 生成 pending intent，下一轮只读状态工具可见。  

---

## 10. 风险与约束（交易场景特有问题）

- **延迟**：多轮模型调用不适合超高频；tick 内 `max_turns` 与总时长上限必备。  
- **确定性**：同样输入应可复现（transcript + 模型版本 + seed 记录）。  
- **合规**：日志中脱敏账号、密钥；工具层审计不可关。  
- **不要**让模型直接构造券商原始报文；维持 `OrderIntent` → 校验 → 风控 → 审批链。

---

## 11. 小结

Claude Code 的核心不是「多调几次 API」，而是：**显式状态机 + 工具结果消息化 + 分层 system 上下文 + 子环境复用同一运行时**。Tradeclaw 已有清晰的单轮业务阶段，最适合 **把 agent 循环嵌在「信号之后、风控之前」或升级为与 `run_cycle` 同级的 `TradingAgentRuntime`**，并通过工具 / skills / 记忆三层扩展能力，同时把成交与风控留在确定性代码路径中。

进一步阅读 Claude Code 源码时，建议顺序与 `docs/08-agent-loop.md` 文末一致；其中 **`query.ts` → `toolExecution.ts` → `claude.ts`** 三份与本文关系最大。
