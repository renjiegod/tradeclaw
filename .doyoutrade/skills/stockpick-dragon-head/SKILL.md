---
name: stockpick-dragon-head
description: 选股策略 — 龙头策略 (Dragon Head / sector leader). Use when the user wants to identify the leading stock during a sector rotation or when a theme/industry catalyst appears — 龙头股 / 龙头战法 / sector leader / 板块龙头 / 打板龙头. Ported from the DSA stock-picking library.
category: stockpick
---

# 龙头策略 (Dragon Head)

- **别名 / aliases**: 龙头, 龙头战法, dragon head
- **分类 / category**: trend (趋势)
- **适配行情 / market regimes**: sector_hot (板块轮动 / 题材发酵)
- **关联核心理念**: 2 趋势 · 7 强势趋势股放宽

## When to use

板块轮动中识别龙头股 —— 板块启动或行业催化剂出现时，找到率先启动、涨幅领先板块的那只。

## 评估标准

1. **板块领涨地位**
   - 所在板块近期涨幅是否前列；该股是否在板块启动周期中率先上涨或涨停。

2. **换手率与动能**（理念7 强势趋势股放宽）
   - 龙头股换手率通常 > 5%；量比 > 1.5 说明交易活跃。

3. **相对强度**
   - 个股涨跌幅 vs 板块平均；真龙头在上涨日应跑赢板块 2% 以上。

4. **新闻催化**
   - 搜索板块级催化剂（政策、事件、业绩）；龙头行情常伴板块整体催化。

5. **乖离率检查**（理念1 严进策略）
   - 龙头可放宽乖离率至 7%，超过 10% 仍需谨慎。

## 评分调整 (sentiment_score)

- 确认为龙头股：**+10**
- 板块正处于主动轮动期：额外 **+5**
- 在结论中注明"龙头策略"判断结果。

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_sector_rankings`（板块排名） | `doyoutrade-cli data sector-heat`（板块涨幅/热度榜）、`data fund-flow --scope sector`（板块资金流）、`data sectors` / `data sector-members`（板块成分 → universe） |
| `get_realtime_quote`（换手率 / 量比） | 最新日线 `data run --tail 1`；量比过滤 `stock screen --volume-ratio-*`；换手率 CLI 无直接单票字段，可从 `data breadth` / `data lhb` / `data fund-flow` 的行数据读取（含 turnover_rate） |
| `search_stock_news`（催化） | `doyoutrade-cli data news <code>` |
| 游资 / 席位辅证 | `doyoutrade-cli data lhb --symbol <code>`（买卖席位 + 游资标签）、市场龙虎榜 `data lhb` |
| 短线情绪面 | `doyoutrade-cli data breadth`（涨停面板 / 连板梯队 / 情绪温度计） |

思路：先 `data sector-heat` 找强板块 → `data sector-members "<板块>" --output u.csv` 取成分 → `stock screen --universe-file u.csv --rank-by rsi --top-k 10` 挑相对最强，再逐票核对换手/席位/催化。
