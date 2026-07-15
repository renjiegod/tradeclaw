---
name: stockpick-expectation-repricing
description: 选股策略 — 预期重估 (Expectation Repricing). Use when the user wants to analyze changes in earnings/policy/valuation expectations and find expectation-gap repair or overheated-expectation pullback risk — 预期差 / 预期重估 / expectation gap / repricing / 预期修复 / 预期兑现. Ported from the DSA stock-picking library.
category: stockpick
---

# 预期重估 (Expectation Repricing)

- **别名 / aliases**: 预期, 预期差, 预期重估, expectation repricing
- **分类 / category**: framework (框架)
- **适配行情 / market regimes**: volatile, sector_hot
- **关联核心理念**: 3 效率 · 5 风险排查 · 6 量价配合

## When to use

市场对公司业绩、政策、行业景气、估值中枢或竞争格局的预期正在变化时，判断当前价格反映的是"预期修复""预期落空"还是"预期过热"。

## 分析框架

1. **预期来源**
   - 识别改变市场预期的信息：业绩预告、机构观点、订单、政策、产品进展、行业数据。
   - 区分硬信息（公告、财报、订单）与软信息（传闻、观点、情绪）。

2. **预期差方向**
   - 正向预期差：市场原本悲观，新信息显示好于预期。
   - 负向预期差：市场原本乐观，新信息低于预期或验证失败。
   - 信息已被连续大涨充分反映 → 提示预期兑现风险。

3. **估值重估**
   - 用 PE/PB、市值、ROE、现金流判断估值重估是否有基本面支撑；估值提升需匹配盈利质量、增长持续性与行业空间。

4. **价格确认**
   - 判断预期变化是否已转化为趋势；放量突破=资金确认，缩量反弹偏修复观察；高位放量滞涨/利好不涨/跌破关键支撑=预期转弱。

## 评分调整 (sentiment_score)

- 正向预期差且价格尚未充分反映：**+15**
- 正向预期差已被连续大涨兑现：**-5**
- 负向预期差或核心假设被证伪：**-15**
- 信息不充分但存在潜在修复：维持中性并降低置信度。

## 工具映射 (DSA → doyoutrade-cli)

| 原 DSA 工具 | doyoutrade 能力 |
|---|---|
| `search_stock_news`（预期来源） | `doyoutrade-cli data news <code>`；机构预期用 `data reports <code>`（评级 / EPS·PE 盈利预测按年）；业绩硬信息用 `data earnings <code>` |
| `get_stock_info`（估值字段） | `doyoutrade-cli stock lookup <code>`（基础信息）+ `data fundamentals <code>`（PE / PB / 市值） |
| `get_realtime_quote` + `analyze_trend`（价格确认） | 最新日线 `data run --tail 1` + `analysis indicators`（是否放量突破/滞涨） |
