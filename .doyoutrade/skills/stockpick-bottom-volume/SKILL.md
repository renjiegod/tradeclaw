---
name: stockpick-bottom-volume
description: 选股策略 — 底部放量 (Bottom Volume Surge). Use when the user wants to find or evaluate stocks that, after an extended decline, stabilize with a volume spike and a bullish candle — 底部放量 / 地量见底 / bottom reversal / capitulation volume reversal. This is a higher-risk reversal probe. Ported from the DSA stock-picking library; pairs with examples/stockpick_scorers/bottom_volume_scorer.py for batch screening.
category: stockpick
---

# 底部放量 (Bottom Volume Surge)

- **别名 / aliases**: 地量见底, 底部放量, bottom volume
- **分类 / category**: reversal (反转)
- **适配行情 / market regimes**: trending_down (下跌趋势末端)
- **关联核心理念**: 2 趋势 · 5 风险排查

## When to use

长期下跌后股价企稳、突然放量，判断是否为潜在趋势反转。**反转信号风险高于趋势跟踪，仓位宜小、止损宜严。**

## 反转判定标准

1. **持续下跌确认**
   - 股价从 20 日高点到近期低点跌幅 > 15%。
   - 趋势状态应为 BEAR 或 STRONG_BEAR。

2. **量能异动**
   - 当日成交量 > 5 日均量的 3 倍（量比 > 3.0）。
   - 该异动应出现在前期极度缩量之后。

3. **价格企稳**
   - 当日 K 线收阳（收盘 > 开盘）。
   - 价格守住近期低点，最好出现长下影线（买方支撑）。

4. **确认因素**（理念5 风险排查）
   - 是否有基本面催化；筹码平均成本接近现价（成本收敛）。

5. **风险提示**（理念2 趋势交易）
   - 反转信号风险高；仓位建议最多 2-3 成；止损设在近期低点下方。

## 评分调整 (sentiment_score)

- 底部放量确认：**+8**
- 配合阳线 + 新闻催化：额外 **+5**
- 止损设在近期低点；在结论中注明"底部放量"。

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_daily_history`（30 日） | `doyoutrade-cli data run <code> --period 2m --tail 5` |
| `analyze_trend`（趋势状态） | `doyoutrade-cli analysis indicators <code> --indicators adx,macd`；跌幅用 OHLCV 自算 |
| 量能异动（量比 >3） | `stock screen --volume-ratio-lookback 5 --volume-ratio-min 3.0` |
| `search_stock_news`（催化） | `doyoutrade-cli data news <code>` |
| 资金/席位辅证 | `doyoutrade-cli data lhb --symbol <code>`（龙虎榜席位/游资）、`data fund-flow --scope individual` |

## 批量打分器 (Strategy SDK scorer)

`examples/stockpick_scorers/bottom_volume_scorer.py` —— 距前 N 日高点跌幅 ≥15% + 量比 ≥3 + 收阳（长下影线加强 tag）才命中：

```bash
doyoutrade-cli stock screen --universe-file /tmp/u.txt \
  --scorer-file examples/stockpick_scorers/bottom_volume_scorer.py \
  --rank-by-diagnostic vol_ratio --top-k 20
```
