---
name: stockpick-bull-trend
description: 选股策略 — 默认多头趋势 (Default Bull Trend). Use as the default individual-stock analysis lens — find bullish MA stacks, trend continuation, and pullback-not-broken low-buy chances while avoiding chasing highs — 多头趋势 / 趋势分析 / 默认趋势选股 / bullish trend / 回踩低吸. Ported from the DSA stock-picking library (its default_active strategy).
category: stockpick
---

# 默认多头趋势 (Default Bull Trend)

- **别名 / aliases**: 趋势, 趋势分析, 多头趋势, bull trend
- **分类 / category**: trend (趋势)
- **适配行情 / market regimes**: trending_up (上升趋势)
- **关联核心理念**: 1 严进 · 2 趋势 · 3 效率
- **备注**: DSA 中此为默认激活（default_active）策略 —— 常规个股分析的首选视角。

## When to use

常规个股分析的默认策略：优先寻找"趋势向上 + 风险可控 + 不追高"的机会。

## 分析框架

1. **趋势确认（优先级最高）**
   - MA5 >= MA10 >= MA20 且 MA20 斜率向上 → 多头结构。
   - 价格显著跌破 MA20 → 降低看多权重。

2. **位置与节奏**
   - 优先"回踩不破"而非"高位追涨"。
   - 距 MA5/MA10 过远 → 提示等待回踩。
   - 放量突破有效阻力可提高胜率评级。

3. **量价验证**
   - 突破日 / 反弹日是否放量；缩量上涨需谨慎，放量滞涨需警惕分歧。

4. **交易建议输出**
   - 明确"买入/观望/减仓"倾向及触发条件，必须给止损参考（MA20 下方或结构低点）。
   - 无清晰优势时明确写"暂不出手"，避免过度交易。

## 评分调整 (sentiment_score)

- 多头排列 + 趋势强度良好：**+12**
- 回踩关键均线后企稳：**+8**
- 放量突破关键阻力：**+10**
- 跌破 MA20 或趋势转弱：**-12**

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_daily_history` | `doyoutrade-cli data run <code> --period 6m --indicators macd --tail 5` |
| `analyze_trend`（均线排列 / 斜率） | `stock screen --ma-above-ma 5,10` + `--ma-above-ma 10,20` + `--ma-slope-min 20,5,0`（MA20 上行）；单票看数值用 `analysis indicators` |

全市场找多头趋势票：`stock screen --universe-file u.txt --ma-above-ma 20,60 --ma-slope-min 20,5,0 --price-above-ma 20 --rank-by rsi --top-k 20`。
