---
name: stockpick-hot-theme
description: 选股策略 — 热点题材 (Hot Theme). Use when the user wants to judge theme/sector strength, board diffusion, and a stock's real relevance and relative strength within a hot theme — 热点题材 / 题材炒作 / hot theme / 板块扩散 / 蹭概念甄别. Ported from the DSA stock-picking library.
category: stockpick
---

# 热点题材 (Hot Theme)

- **别名 / aliases**: 热点, 题材, 热点题材, hot theme
- **分类 / category**: framework (框架)
- **适配行情 / market regimes**: sector_hot (题材发酵)
- **关联核心理念**: 2 趋势 · 3 效率 · 5 风险排查 · 7 强势股放宽

## When to use

市场出现明确政策 / 产业 / 技术路线 / 资金抱团热点时，判断个股是否真正受益，而非单纯蹭概念。

## 分析框架

1. **热点强度**
   - 相关板块是否在涨幅 / 成交额 / 人气上前列；热点是否从核心股扩散到板块内多只个股。
   - 仅单股异动、板块未共振 → 降低信号权重。

2. **个股相关性**
   - 公司业务 / 订单 / 产能 / 客户 / 公告是否与热点直接相关。
   - 区分"实质受益 / 间接受益 / 概念关联较弱"；关联弱但涨幅过大 → 题材兑现风险。

3. **相对强弱**
   - 个股涨幅 / 量比 / 换手率是否强于板块平均；强势热点股常放量、换手活跃、回调不破关键均线。

4. **节奏与风险**
   - 不在连续加速 / 高乖离位置追涨；新闻集中"已大涨 / 资金追捧 / 龙虎榜游资博弈" → 警惕短线情绪顶。
   - 重大利空 / 监管问询 / 澄清公告可一票降级。

## 评分调整 (sentiment_score)

- 热点处于启动或扩散期，且个股实质受益：**+12**
- 个股强于板块并有量能确认：额外 **+6**
- 热点进入分化或退潮：**-8**
- 仅概念蹭热点且乖离率过高：**-12**

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_sector_rankings`（板块强度 / 成交额 / 人气） | `doyoutrade-cli data sector-heat`（题材/板块热度榜）、`data fund-flow --scope sector`（板块资金流）、`data sectors` / `data sector-members` |
| `search_stock_news`（业务相关性） | `doyoutrade-cli data news <code>`（个股新闻）；行业景气可结合 `data reports`（研报） |
| `get_realtime_quote` + `analyze_trend`（相对强弱） | 最新日线 `data run --tail 1` + `analysis indicators`；`stock screen --rank-by rsi` 在板块 universe 内挑最强 |
| 情绪面 / 游资 | `doyoutrade-cli data breadth`（情绪温度计）、`data lhb`（龙虎榜游资） |
