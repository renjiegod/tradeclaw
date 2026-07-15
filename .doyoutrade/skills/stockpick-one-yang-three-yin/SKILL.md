---
name: stockpick-one-yang-three-yin
description: 选股策略 — 一阳夹三阴 (One Yang Three Yin). Use when the user wants to find or evaluate the 5-bar consolidation-end candlestick pattern (big bullish bar, three small pausing bars, then a bullish breakout bar) — 一阳夹三阴 / 一阳穿三阴 / candlestick consolidation pattern / trend-continuation K-line setup. Ported from the DSA stock-picking library; pairs with examples/stockpick_scorers/one_yang_three_yin_scorer.py for batch screening.
category: stockpick
---

# 一阳夹三阴 (One Yang Three Yin)

- **别名 / aliases**: 一阳穿三阴, 一阳夹三阴, one yang three yin
- **分类 / category**: pattern (形态)
- **适配行情 / market regimes**: 趋势中的整理末端
- **关联核心理念**: 2 趋势 · 4 买点偏好

## When to use

识别"一阳夹三阴"K 线整理形态 —— 大阳后三根小阴 / 小 K 缩量整理，再收阳突破，视为整理结束、趋势延续入场信号。

## 形态定义（最近 5 个交易日）

1. **第 1 日**：大阳线（收盘 > 开盘，实体 > 股价的 2%）。
2. **第 2-4 日**：连续三根阴线或小 K 线
   - 每根最低价不跌破第 1 日开盘价。
   - 成交量逐步萎缩（量比 < 0.8）。
   - 三根收在第 1 日实体范围内。
3. **第 5 日**：又一根阳线，收盘突破第 1 日收盘价。

## 评分调整 (sentiment_score)

- 形态成立 + 趋势看多（MA5 > MA10 > MA20）：**+15**
- 形态成立但趋势不明：**+5**
- 理想买点设在第 5 日收盘价附近，止损设在第 1 日开盘价下方；在结论中注明"一阳夹三阴"。

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_daily_history`（近 10 日） | `doyoutrade-cli data run <code> --period 1m --tail 6`（逐根核对最后 5 根 K 线的开收高低量） |
| `analyze_trend`（多头排列确认） | `stock screen --ma-above-ma 5,10` / `--ma-above-ma 10,20`，或 `analysis indicators` |

> 说明：doyoutrade 内置 candlestick pattern 检测（`analysis pattern` / `stock screen --patterns`）不含"一阳夹三阴"这一复合形态；用下方专用打分器逐票判定，或用 `data run` 拉最近 5 根 K 线人工核对。

## 批量打分器 (Strategy SDK scorer)

`examples/stockpick_scorers/one_yang_three_yin_scorer.py` —— 用最近 5 根 K 线的实体 / 收盘 / 成交量精确复现该形态（命中 = `Signal.buy`）：

```bash
doyoutrade-cli stock screen --universe-file /tmp/u.txt \
  --scorer-file examples/stockpick_scorers/one_yang_three_yin_scorer.py \
  --top-k 20
```
