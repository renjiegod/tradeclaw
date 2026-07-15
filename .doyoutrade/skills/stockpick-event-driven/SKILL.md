---
name: stockpick-event-driven
description: 选股策略 — 事件驱动 (Event Driven). Use when the user wants to evaluate stocks around earnings, policy, M&A, orders, product launches, regulation or litigation — judging catalyst strength, realization probability, and risk — 事件驱动 / 催化事件 / event driven / 业绩催化 / 政策利好 / 并购重组. Ported from the DSA stock-picking library.
category: stockpick
---

# 事件驱动 (Event Driven)

- **别名 / aliases**: 事件驱动, 催化, 催化事件, event driven
- **分类 / category**: framework (框架)
- **适配行情 / market regimes**: sector_hot, volatile
- **关联核心理念**: 3 效率 · 5 风险排查

## When to use

公司或行业出现明确事件催化（业绩预告、订单中标、并购重组、政策落地、产品发布、监管处罚、诉讼等），判断是短期交易催化、长期基本面改善，还是利好兑现。

## 分析框架

1. **事件分类**
   - 梳理近期关键事件，分为：业绩类、政策类、订单/产品类、资本运作类、监管/风险类。
   - 明确事件发生时间；过期或时间未知的信息不能作为主要依据。

2. **影响路径**
   - 判断事件影响的是收入、利润率、估值、融资能力、市场份额，还是仅影响情绪。
   - 重大订单/政策利好要说明兑现周期与不确定性；监管/减持/处罚/诉讼等风险优先。

3. **市场反应**
   - 事件是否已被价格充分反映；放量上涨但未过关键阻力可等确认；高位放量滞涨或利好后冲高回落须警惕兑现压力。

4. **交易计划**
   - 事件未兑现前强调仓位控制与时间窗口；兑现后重估是否从"预期交易"切换到"业绩验证"；负面事件先看风险释放是否充分。

## 评分调整 (sentiment_score)

- 高可信正向事件且价格尚未充分反映：**+14**
- 正向事件已大幅兑现：**-6**
- 负面事件仍在发酵：**-15**
- 事件影响不清晰或信息冲突：维持中性并降低置信度。

## 输出要求

- 明确事件性质：利好 / 利空 / 中性 / 不确定。
- 给出事件可信度、兑现周期、已反映程度。
- 操作建议须含失效条件（公告不及预期、跌破关键支撑、事件热度消退）。

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `search_stock_news`（事件梳理） | `doyoutrade-cli data news <code>`；业绩类事件另有 `data earnings <code>`（业绩预告/快报）、`data reports <code>`（研报/评级/盈利预测） |
| `get_realtime_quote` + `analyze_trend`（市场反应） | 最新日线 `data run --tail 1` + `analysis indicators`（是否放量过阻力 / 高位滞涨） |
| 资金 / 席位（事件后资金动向） | `doyoutrade-cli data fund-flow --scope individual`、`data lhb --symbol <code>` |
