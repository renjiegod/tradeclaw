---
name: stockpick-emotion-cycle
description: 选股策略 — 情绪周期 (Sentiment Cycle). Use when the user wants to time entries against crowd behavior using turnover rate, volume structure and news sentiment — find panic bottoms and euphoric tops to trade counter-sentiment — 情绪周期 / 情绪底 / 情绪顶 / 换手率 / sentiment cycle / 逆情绪. Ported from the DSA stock-picking library.
category: stockpick
---

# 情绪周期 (Sentiment Cycle)

- **别名 / aliases**: 情绪, 情绪周期, sentiment cycle
- **分类 / category**: framework (框架)
- **适配行情 / market regimes**: sector_hot
- **关联核心理念**: 1 严进 · 2 趋势 · 3 效率 · 5 风险排查

## When to use

市场情绪在"恐慌→悲观→怀疑→希望→乐观→兴奋→贪婪→狂热"间循环。聪明钱在恐慌底部布局、狂热顶部离场 —— 用换手率、量价结构与新闻情绪逆情绪布局。

## 情绪阶段量化

1. **换手率（情绪热度核心）**
   - <0.5%/日：冷淡，潜在底部；0.5%~2%：正常；2%~5%：活跃不宜追高；>5%：高热警惕顶；>10%：极度过热常为短顶。

2. **连续换手率走势（近 20 日）**
   - 由高向低 + 量萎缩 → 情绪退潮，等待；由低向高 + 量陡增 → 情绪启动可介入；突然单日暴量（超前期 5 倍）→ 常为主力出货。

3. **新闻情绪面**
   - 集中"利好兑现/超预期/涨停/机构推荐" → 可能过热；集中"业绩下滑/利空/破位" → 悲观或造底；散户极端负面 → 反向指标近底。

4. **均线收缩与波动率**
   - MA5/10/20 粘合 → 蓄势情绪冷淡；ATR 萎缩 → 极度低迷、爆发前兆。

### 情绪底部特征（买入区，满足 ≥3 项）
换手率近一年低位 · 量持续萎缩低于近 60 日均量 50% · 新闻低调/中性/负面 · 股价在 MA20 附近或下但无恐慌暴跌 · 机构持仓稳定或小增。

### 情绪顶部特征（减仓区，满足 ≥3 项）
近 5 日换手率 > 近 20 日均值 2 倍 · 单日脉冲放量 · 新闻利好兑现/目标价大幅上调/散户追捧 · 偏离 MA5 超 8% · MACD 顶背离。

## 评分调整 (sentiment_score)

- 情绪底部特征满足 3 项以上：**+14**；满足全部 5 项：**+20**
- 情绪顶部特征满足 3 项以上：**-12**；满足全部 5 项：**-20**
- 情绪平稳区间：不调整基础分。

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `get_daily_history` + `get_realtime_quote`（换手率 / 量） | `doyoutrade-cli data run <code> --period 3m --tail 20`（量的走势）；换手率单票字段可从 `data breadth` / `data lhb` / `data fund-flow` 的行数据（turnover_rate）读取 |
| `analyze_trend`（均线收缩 / ATR） | `doyoutrade-cli analysis indicators <code> --indicators atr,macd` |
| `search_stock_news`（新闻情绪） | `doyoutrade-cli data news <code>` |
| 市场级情绪 | `doyoutrade-cli data breadth`（涨跌停 / 连板梯队 / **情绪温度计**，规则型非预测）—— 判断整体情绪冷热的首选轴 |
| 资金 / 游资出货 | `doyoutrade-cli data fund-flow --scope individual`、`data lhb --symbol <code>` |
