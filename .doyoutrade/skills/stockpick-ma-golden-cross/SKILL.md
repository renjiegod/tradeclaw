---
name: stockpick-ma-golden-cross
description: 选股策略 — 均线金叉 (MA Golden Cross). Use when the user wants to find or evaluate stocks where MA5 crosses above MA10 (or MA10 above MA20) with volume confirmation — 均线金叉 / 金叉选股 / MA cross screening / trend continuation entry. Ported from the DSA stock-picking library; pairs with the companion scorer examples/stockpick_scorers/ma_golden_cross_scorer.py for batch screening.
category: stockpick
---

# 均线金叉 (MA Golden Cross)

- **别名 / aliases**: 均线金叉, 金叉, MA golden cross
- **分类 / category**: trend (趋势)
- **适配行情 / market regimes**: trending_up (上升趋势)
- **关联核心理念**: 1 严进（乖离率<5%）· 2 趋势（多头排列）· 3 效率（量能确认）

## When to use

选出或评估"均线金叉配合量能"的个股 —— 经典的趋势反转 / 延续信号。盘整后金叉信号最强，上升趋势中金叉为延续信号。

## 信号判定标准

1. **金叉检测**（理念2 趋势交易）
   - 主信号：MA5 在最近 3 个交易日内上穿 MA10。
   - 强信号：MA10 上穿 MA20（更慢但更可靠）。
   - MACD 状态：金叉或零轴上方金叉更佳。

2. **量能确认**（理念3 效率优先）
   - 金叉日成交量应高于 5 日均量。
   - 金叉日量比 > 1.2 为积极信号。

3. **趋势背景**
   - 盘整后金叉：最强信号。
   - 上升趋势中金叉：延续信号。
   - 深度下跌中金叉：弱信号，需更多确认。

4. **价格位置**（理念1 严进策略）
   - 价格应在交叉均线附近或上方。
   - 乖离率 < 5% —— 避免追高延迟入场。

## 评分调整 (sentiment_score)

- MA5 × MA10 金叉配合量能：**+10**
- MA10 × MA20 金叉：**+8**
- MACD 零轴上方金叉：额外 **+5**
- 理想买点设在交叉均线水平附近；在结论中注明"均线金叉"。

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_daily_history` | `doyoutrade-cli data run <code> --period 3m --indicators macd --tail 5`（先 `stock lookup <name>` 取 canonical symbol） |
| `analyze_trend`（均线排列 / MACD） | `doyoutrade-cli analysis indicators <code> --indicators macd`；均线关系用 `data run --indicators` 计算 MA，或 `stock screen --ma-cross golden:5,10 --cross-window 3` / `--ma-above-ma 5,10` |
| 量能确认 | `stock screen --volume-ratio-lookback 5 --volume-ratio-min 1.2`，或 `analysis indicators` 看量比 |

全市场筛选可用 `stock screen --universe-file u.txt --ma-cross golden:5,10 --cross-window 3 --volume-ratio-lookback 5 --volume-ratio-min 1.2 --rank-by rsi --top-k 20`。

## 批量打分器 (Strategy SDK scorer)

`examples/stockpick_scorers/ma_golden_cross_scorer.py` 把上述逻辑编码为一个 Strategy SDK 打分器（`on_bar` 返回 `Signal.buy` 即命中）。用 code-screen 模式跑：

```bash
doyoutrade-cli stock screen --universe-file /tmp/u.txt \
  --scorer-file examples/stockpick_scorers/ma_golden_cross_scorer.py \
  --rank-by-diagnostic vol_ratio --top-k 20
```
