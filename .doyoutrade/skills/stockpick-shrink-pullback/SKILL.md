---
name: stockpick-shrink-pullback
description: 选股策略 — 缩量回踩 (Shrink Volume Pullback). Use when the user wants to find or evaluate uptrend stocks pulling back to MA5/MA10 support on shrinking volume, then bouncing — 缩量回踩 / 回踩均线 / pullback entry / shrink-volume dip buy. Ported from the DSA stock-picking library; pairs with examples/stockpick_scorers/shrink_pullback_scorer.py for batch screening.
category: stockpick
---

# 缩量回踩 (Shrink Volume Pullback)

- **别名 / aliases**: 缩量回踩, 回踩, shrink pullback
- **分类 / category**: trend (趋势)
- **适配行情 / market regimes**: trending_down, sideways（趋势中的回调 / 横盘）
- **关联核心理念**: 1 严进 · 2 趋势 · 4 买点偏好（回踩均线支撑）

## When to use

上升趋势个股回踩均线、量能萎缩、守住支撑后再入场 —— 趋势延续的理想低吸点。

## 入场判定标准

1. **前提条件**（理念2 趋势交易）
   - 必须处于上升趋势：MA5 > MA10 > MA20 多头排列。

2. **回踩检测**（理念4 买点偏好）
   - 价格回踩至 MA5 附近（误差 1% 内）或 MA10 附近（误差 2% 内）。
   - 回调期间成交量 < 5 日均量的 70%（缩量特征）。

3. **反弹信号**（理念1 严进策略）
   - 当前价格守住均线支撑位。
   - MA5 乖离率 < 2% —— 最佳买入区间。

4. **确认条件**（理念5 风险排查）
   - 无利空消息；筹码分布健康（获利比例 50-80%）。

## 评分调整 (sentiment_score)

- 缩量回踩 MA5：**+10**
- 缩量回踩 MA10 且量能 < 0.6 倍均量：**+8**
- 理想买点设在 MA5 水平，次优 MA10；止损设在 MA20 水平；在结论中注明"缩量回踩"。

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_daily_history` | `doyoutrade-cli data run <code> --period 3m --tail 5` |
| `analyze_trend`（多头排列 + 缩量状态） | `stock screen --ma-above-ma 5,10` / `--ma-above-ma 10,20`；量能萎缩用 `--volume-ratio-lookback 5 --volume-ratio-min ...`（回踩看缩量，取小倍数）或 `analysis indicators` 看量比 |
| `get_realtime_quote` | 最新日线 `data run --tail 1`（判断距 MA5 乖离率） |
| `search_stock_news` | `doyoutrade-cli data news <code>` |

## 批量打分器 (Strategy SDK scorer)

`examples/stockpick_scorers/shrink_pullback_scorer.py` —— 多头排列 + 距 MA5 ≤2% + 量比 <0.7 才命中。命中股的 `vol_ratio` 越小缩量越明显（可用 `--rank-by-diagnostic vol_ratio` 排序，方向以该命令实际支持为准）：

```bash
doyoutrade-cli stock screen --universe-file /tmp/u.txt \
  --scorer-file examples/stockpick_scorers/shrink_pullback_scorer.py \
  --rank-by-diagnostic vol_ratio --top-k 20
```
