---
name: stockpick-volume-breakout
description: 选股策略 — 放量突破 (Volume Breakout). Use when the user wants to find or evaluate stocks breaking above resistance / prior N-day highs on heavy volume (>2x average) — 放量突破 / 突破选股 / breakout screening / volume spike breakout. Ported from the DSA stock-picking library; pairs with examples/stockpick_scorers/volume_breakout_scorer.py for batch screening.
category: stockpick
---

# 放量突破 (Volume Breakout)

- **别名 / aliases**: 放量突破, 突破, volume breakout
- **分类 / category**: trend (趋势)
- **适配行情 / market regimes**: trending_up (上升趋势)
- **关联核心理念**: 1 严进 · 2 趋势 · 3 效率（量能确认）

## When to use

股价接近已知阻力位（20 日高点或前期平台顶部）时，判断是否放量有效突破。

## 突破判定标准

1. **阻力位识别**
   - 阻力位通常为 20 日高点或前期震荡平台顶部。

2. **量能确认**（理念3 效率优先）
   - 当日成交量 > 5 日均量的 2 倍（量比 > 2.0）。

3. **价格确认**（理念1 严进策略）
   - 收盘价必须站上阻力位。
   - 强势收盘：收盘在当日振幅上方 30%。
   - 突破后乖离率仍需 < 5%，避免追高。

4. **后续验证**
   - 次日开盘应在突破位之上，区分真突破与假突破。

5. **风险过滤**（理念5 风险排查）
   - 检查无重大利空；PE 不应过高（避免泡沫型突破）。

## 评分调整 (sentiment_score)

- 放量突破确认：**+12**
- 突破伴随板块共振（板块也走强）：额外 **+5**
- 理想买点设在突破位附近，止损设在突破位下方 3%；在结论中注明"放量突破"。

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_daily_history` | `doyoutrade-cli data run <code> --period 3m --tail 5`（先 `stock lookup`） |
| `analyze_trend`（阻力位） | `doyoutrade-cli analysis indicators <code>`；突破用 `stock screen --breakout` 系列 / `--volume-ratio-*` 组合 |
| `get_realtime_quote`（量比） | 最新日线 `data run --tail 1`；量比过滤用 `stock screen --volume-ratio-lookback 5 --volume-ratio-min 2.0`（CLI 无盘中 tick，用最新日线近似） |
| 板块共振 | `doyoutrade-cli data sector-heat` / `data fund-flow --scope sector` 判断所属板块是否走强 |
| `search_stock_news`（利空过滤） | `doyoutrade-cli data news <code>` |

## 批量打分器 (Strategy SDK scorer)

`examples/stockpick_scorers/volume_breakout_scorer.py` —— 突破前 N 日高点 + 量比 ≥2 + 强势收盘 才命中：

```bash
doyoutrade-cli stock screen --universe-file /tmp/u.txt \
  --scorer-file examples/stockpick_scorers/volume_breakout_scorer.py \
  --rank-by-diagnostic vol_ratio --top-k 20
```
