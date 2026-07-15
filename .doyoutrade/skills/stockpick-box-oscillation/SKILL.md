---
name: stockpick-box-oscillation
description: 选股策略 — 箱体震荡 (Box Range Trading). Use when the user wants to identify a price box (support/resistance range) and buy near the box bottom / trim near the box top in range-bound markets — 箱体震荡 / 箱体战法 / range trading / support-resistance box / 箱底买箱顶卖. Ported from the DSA stock-picking library; pairs with examples/stockpick_scorers/box_oscillation_scorer.py for batch screening.
category: stockpick
---

# 箱体震荡 (Box Range Trading)

- **别名 / aliases**: 箱体, 箱体震荡, box oscillation
- **分类 / category**: framework (框架)
- **适配行情 / market regimes**: sideways (横盘震荡)
- **关联核心理念**: 1 严进 · 2 趋势 · 3 效率

## When to use

价格在阻力位与支撑位之间反复震荡时，"贴着支撑买、接近阻力卖"，通过波段获取区间收益。

## 分析步骤

1. **箱体识别**（近 60~120 日）
   - 箱顶（阻力）：多次触碰未有效突破的高点连线；箱底（支撑）：多次下探未有效跌破的低点连线。
   - 顶部与底部各至少触碰 2~3 次方可确认。

2. **当前位置判断**
   - 箱底区域（距支撑 ≤5%）：买入 / 加仓，止损箱底下方 3%。
   - 箱中区域（中间 1/3）：观望。
   - 箱顶区域（距阻力 ≤5%）：减仓 / 止盈，不追高。

3. **量能辅助**
   - 箱底放量企稳：强信号，可较重仓；箱顶缩量滞涨：卖出信号。
   - 箱体放量突破（>均量 2 倍）：向上→转多头趋势策略，向下→离场，原支撑转阻力。

4. **箱体宽度**：(顶-底)/底×100%。<5% 不参与；5%~15% 标准箱；>15% 大箱可做大波段。

5. **假突破识别**：单日盘中触边快速回撤收在箱内 → 假突破维持箱体操作；连续两日收盘突破 + 放量 → 真突破改策略。

## 评分调整 (sentiment_score)

- 箱底企稳 + 缩量：**+10**
- 箱底放量攻顶：**+12**
- 箱体向上有效突破：**+15（转趋势策略）**
- 处于箱顶区域：**-5（不追高）**
- 箱底有效跌破：**-15（离场）**

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_daily_history`（60~120 日） | `doyoutrade-cli data run <code> --period 6m --tail 5` |
| `analyze_trend`（支撑/阻力） | `doyoutrade-cli analysis pattern <code>`（support/resistance、peaks/valleys）；箱顶/箱底也可用 Donchian 上下轨自算 |
| `get_realtime_quote` | 最新日线 `data run --tail 1`（判断处于箱底/箱中/箱顶） |
| 量能辅助 | `stock screen --volume-ratio-*` / `analysis indicators` 看量比 |

## 批量打分器 (Strategy SDK scorer)

`examples/stockpick_scorers/box_oscillation_scorer.py` —— 以前 N 日高/低（不含当日）为箱顶/箱底，箱宽足够、价近箱底且未破位才命中；箱底放量/缩量在 tag 中区分：

```bash
doyoutrade-cli stock screen --universe-file /tmp/u.txt \
  --scorer-file examples/stockpick_scorers/box_oscillation_scorer.py \
  --top-k 20
```
