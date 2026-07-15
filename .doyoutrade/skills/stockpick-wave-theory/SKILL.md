---
name: stockpick-wave-theory
description: 选股策略 — 波浪理论 (Elliott Wave Theory). Use when the user wants to count impulse (1-3-5) and corrective (A-B-C) waves, judge the current wave position, Fibonacci targets and best entry (2nd/4th wave pullback) — 波浪理论 / 艾略特 / elliott wave / 推动浪 / 调整浪 / 斐波那契目标. Ported from the DSA stock-picking library.
category: stockpick
---

# 波浪理论 (Elliott Wave Theory)

- **别名 / aliases**: 波浪, 波浪理论, 艾略特, elliott wave
- **分类 / category**: framework (框架)
- **适配行情 / market regimes**: volatile
- **关联核心理念**: 1 严进 · 2 趋势 · 3 效率 · 4 买点偏好

## When to use

市场按 5 浪推进 + 3 浪调整循环运行。判断当前所处浪型与潜在目标价，锁定最优买点。

## 分析步骤

1. **识别当前浪型**
   - 推动浪（1-3-5）：第 1 浪反转首波量温和；第 3 浪最强、放量、MACD 强势，绝不是最短浪；第 5 浪量弱于第 3 浪，顶背离预警。
   - 调整浪（A-B-C）：A 浪首跌量较大；B 浪反弹弱、量萎缩、陷阱风险高；C 浪二次下跌力度常超 A 浪。

2. **黄金位置**
   - 第 2 浪回调常在第 1 浪的 38.2%~61.8%；第 3 浪目标为第 1 浪的 1.618~2.618 倍延伸；第 4 浪不得进入第 1 浪价格区域；C 浪目标≥A 浪长度。

3. **最优买点**
   - 第 2 浪回调企稳（黄金坑）：最安全，止损第 1 浪起点。
   - 第 4 浪回调企稳：次优，止损第 1 浪顶部。
   - 第 3 浪初期突破：放量突破第 1 浪高点时。
   - 避免在第 5 浪末端追高（顶背离风险）。

4. **风险提示**
   - B 浪反弹不宜重仓；波浪计数主观，需结合其他指标验证；规则被违反（第 4 浪侵入第 1 浪）须重新归数。

## 评分调整 (sentiment_score)

- 第 2 浪底部企稳（黄金坑）：**+15**
- 第 3 浪突破确认：**+12**
- 第 5 浪末端 / 顶背离：**-10**
- C 浪下跌中：**-12**

## 输出要求

给出当前浪型位置、关键斐波那契支撑/阻力位（0.382/0.618/1.618）、买入/等待/规避判断，并标注波浪计数置信度（高/中/低）。

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_daily_history`（近 120 日） | `doyoutrade-cli data run <code> --period 6m --tail 5` |
| `analyze_trend`（趋势 / MACD 顶背离 / 结构高低点） | `doyoutrade-cli analysis indicators <code> --indicators macd`；摆动高低点 / ZigZag 用 `analysis pattern`（swing/peaks/valleys），SDK 侧有 `indicators.zigzag` |
| `get_realtime_quote` | 最新日线 `data run --tail 1`（斐波那契回撤位对照现价） |

> 说明：波浪计数与斐波那契目标是主观结构判断，doyoutrade 无内置"数浪"算子；用 `analysis pattern` 的摆动点 + ZigZag + MACD 强弱做辅助，浪型划分仍需在此框架下人工推断，并按置信度标注。
