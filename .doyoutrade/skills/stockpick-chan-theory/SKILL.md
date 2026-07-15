---
name: stockpick-chan-theory
description: 选股策略 — 缠论 (Chan / Zen Channel Theory). Use when the user wants pen/stroke/segment/hub structure analysis, trend-level judgment, buy/sell-point classification (一买/二买/三买) and MACD divergence (背驰) — 缠论 / 缠论分析 / chan theory / 中枢 / 背驰 / 一买二买三买. Ported from the DSA stock-picking library.
category: stockpick
---

# 缠论 (Chan / Zen Channel Theory)

- **别名 / aliases**: 缠论, 缠论分析, chan theory
- **分类 / category**: framework (框架)
- **适配行情 / market regimes**: volatile
- **关联核心理念**: 1 严进 · 2 趋势 · 3 效率 · 4 买点偏好

## When to use

用缠论"分型 → 笔 → 线段 → 中枢 → 趋势"框架判断趋势级别、买卖点与背驰信号。

## 分析步骤

1. **价格结构（中枢识别）**
   - 中枢：连续 3 段走势重叠区间，价格反复震荡；趋势：连续 3 个同级别中枢同向移动。
   - 判断当前处于震荡中枢还是趋势段（脱离中枢）。

2. **背驰判断（最高优先级）**
   - 顶背驰：价创新高但 MACD 红柱面积缩小 → 卖出/减仓。
   - 底背驰：价创新低但 MACD 绿柱面积缩小 → 买入/加仓。

3. **买卖点判定**
   - 一买（最强）：下跌趋势中最后一个中枢出现底背驰。
   - 二买：离开下跌中枢后第一次回调不破中枢高点。
   - 三买：中枢震荡后向上突破（不回中枢内）。
   - 一卖/二卖/三卖为对称结构，方向相反。

4. **级别与仓位**
   - 日线级别买卖点较重仓 (30-50%)；周线级别更重 (50-80%)；多级别共振（日+周同向）信号最强。

## 评分调整 (sentiment_score)

- 底背驰 + 一买信号：**+15**
- 二买/三买共振：**+10**
- 中枢震荡无明确方向：维持基准。
- 顶背驰 / 趋势向下：**-15**

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_daily_history`（近 60 日 + 多级别） | `doyoutrade-cli data run <code> --period 6m --tail 5`；周线级别用 `data run` 周期数据或 SDK `@informative('1w')` |
| `analyze_trend`（MACD 背驰对照） | `doyoutrade-cli analysis indicators <code> --indicators macd`（红/绿柱=`macd.hist`，与价格高低点对比判背驰）；结构高低点用 `analysis pattern`（peaks/valleys、swing） |
| `get_realtime_quote` | 最新日线 `data run --tail 1` |

> 说明：缠论的笔/线段/中枢/背驰是复合结构判断，doyoutrade 无内置"缠论"算子；用 `analysis pattern` 的摆动高低点 + `analysis indicators` 的 MACD 柱面积做量化对照，中枢与买卖点分级仍需在此框架下人工推断。
