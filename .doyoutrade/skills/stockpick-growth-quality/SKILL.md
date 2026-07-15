---
name: stockpick-growth-quality
description: 选股策略 — 成长质量 (Growth Quality). Use when the user wants to evaluate mid/long-term growth quality via revenue & profit growth, ROE, cash flow and industry space, and spot growth-stall risk — 成长股 / 成长质量 / growth quality / ROE / 现金流质量 / 高景气. Ported from the DSA stock-picking library.
category: stockpick
---

# 成长质量 (Growth Quality)

- **别名 / aliases**: 成长, 成长股, 成长质量, growth quality
- **分类 / category**: framework (框架)
- **适配行情 / market regimes**: trending_up
- **关联核心理念**: 2 趋势 · 3 效率 · 5 风险排查

## When to use

关注公司中长期成长能力（而非只看短线技术形态），适合高景气行业、业绩持续改善或商业模式扩张阶段的公司。

## 分析框架

1. **成长性**
   - 看营业收入、归母净利润、经营现金流、ROE；收入增长与利润增长是否同向，是否"增收不增利"。
   - 仅概念热度但财报未验证 → 降低成长确定性。

2. **质量**
   - ROE 越高且稳定质量越好；经营现金流与净利润方向一致说明盈利质量可靠；现金流显著弱于利润 → 提示回款/存货/应收风险。

3. **估值承受力**
   - 用 PE/PB、市值判断是否提前透支成长；高成长可承受更高估值，但须说明增长能否覆盖估值；估值高且成长放缓应明显下调评分。

4. **趋势确认**
   - 判断长期成长逻辑是否被资金确认；基本面向好但技术面未确认时优先给观察条件而非直接追买。

## 评分调整 (sentiment_score)

- 收入、利润、现金流和 ROE 同向改善：**+15**
- 行业景气与公司新闻互相验证：额外 **+6**
- 高估值但成长未验证：**-8**
- 增收不增利或现金流恶化：**-12**

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_stock_info`（财报 / 估值字段） | `doyoutrade-cli stock lookup <code>`（基础信息）+ `data fundamentals <code>`（PE/PB/市值）+ `data earnings <code>`（业绩快报：营收/净利/ROE/EPS 同比）+ `data reports <code>`（分析师盈利预测） |
| `search_stock_news`（行业景气 / 公司验证） | `doyoutrade-cli data news <code>` |
| `get_realtime_quote` + `analyze_trend`（趋势确认） | 最新日线 `data run --tail 1` + `analysis indicators` |

> 说明：ROE / 现金流 / 营收利润增长的完整财报明细，CLI 侧主要来自 `data earnings`（业绩快报字段：`eps,revenue,revenue_prev_yoy,net_profit,net_profit_prev_yoy,roe` 等）与 `data reports`（分析师 EPS/PE 预测）；更细的三大报表科目当前不在 CLI 覆盖内，需以这两轴 + 新闻交叉印证。
