# 短线玩家手册：把你的战法接到 DoYouTrade

面向做 **短线 / 打板 / 追龙头 / 每天复盘** 的 A 股玩家。这份文档把你熟悉的日常动作，映射到 DoYouTrade **当前已实装、跑得起来**的能力上，并诚实标注：哪些免费就能用、哪些需要接 QMT、哪些现在还**做不到**。

> **两条前提，先说清楚：**
> 1. **关键数字只来自工具调用**（真实行情 / 真实回测），AI 不编数字、不荐股、不预测涨跌、不承诺收益。它帮你把想法变成**可求证的分析**和**可回测的策略**，判断和下单永远在你自己。
> 2. **免费数据源以日线为主**（baostock / akshare，克隆即用、免 token）。凡是要盘口、tick、竞价、实时封单的功能，都需要接 **QMT**（见主 README「梯度三」）。

---

## 一张总表

| 你的动作 | 命令 / 入口 | 数据要求 |
|---|---|---|
| 名称 / 代码 → 规范 symbol | `doyoutrade-cli stock lookup 茅台` | 免费 |
| 扫今天贴近涨停 / 近似封板的票 | `stock screen --universe-file u.csv --limit-up-approx` | 免费日线 |
| 按板块 / 题材拉一篮子票 | `data sector-members "半导体,白酒" --output u.csv` | 免费（akshare） |
| 技术条件筛股（均线多头 / 放量 / RSI / 形态…） | `stock screen --universe-file u.csv --ma-above-ma 20,60 ...` | 免费日线 |
| 盯盘：涨停 / 封单缩量 / 炸板打开，命中推送 | `monitor create --preset limit_up_open` | **需 QMT** 实时行情 |
| 把"低吸反包 / N 连板断板出场"写成可回测策略 | `strategy authoring` + `backtest run` | 免费日线回测 |
| 盘中纪律提醒（买了 X，走坏了叫我） | `cron create --task-kind deviation_monitor` | **需 QMT** 盘中价 |
| 每天收盘自动复盘并存档 | `cron create --task-kind daily_review` | 复盘框架免费；成交对账单需 QMT |
| 记情绪周期 / 题材 / 龙头 / 标的角色笔记 | 私有知识库 `cycles/`、`symbols/roles.md` | 免费、本地私有 |
| 自选股盘中看盘口 | 控制台「自选股」页 / `watchlist quotes` | **需 QMT** 实时行情 |

CLI 默认连 `http://127.0.0.1:8000`（先 `uv run doyoutrade` 起 server）。不确定某命令参数时先 `doyoutrade-cli schema <command>`。

---

## 分场景详解

### 1. 扫贴板股 / 近似涨停

先把候选池解析成 universe，再用 `--limit-up-approx` 扫近似封板的票。涨跌停阈值**按板块自动分档**（主板 10% / 创业板·科创板 20% / 北交所 30%）：

```bash
# 先把一个板块的成分写成 universe 文件
doyoutrade-cli data sector-members "半导体" --output /tmp/semi.csv
# 扫近似涨停 + 放量（近 10 日均额 > 1 亿），按涨幅取前 20
doyoutrade-cli stock screen --universe-file /tmp/semi.csv \
  --limit-up-approx --avg-amount-lookback 10 --avg-amount-min 1e8 \
  --rank-by rsi --top-k 20
```

结果 CSV 在 envelope 的 `data.result_path`，前 10 行在 `data.preview`。

> 提速提示：`stock screen` 优先读本地行情仓库。全市场扫描慢多半是仓库里没那批数据——把票加进自选（本地库默认只同步自选），或让运维开 `market_data.sync_full_market`。

### 2. 盯盘：炸板 / 封单缩量 / 打开（需 QMT）

盯盘是独立的「盯股票」实体，不依赖运行中的交易任务。命中即推飞书，tick 级评估、rising-edge + cooldown 去重，不用一直守屏：

```bash
doyoutrade-cli monitor create --name "半导体炸板盯盘" \
  --scope-kind watchlist_tag --tag 半导体 \
  --channel-id <chan-…> --chat-id <oc_…> \
  --condition '{"op":"or","children":[{"preset":"limit_up_open"},{"preset":"limit_up_seal_shrink"}]}'
```

可用预设：`limit_up` / `limit_down` / `limit_up_seal_shrink`（涨停封单缩量）/ `limit_down_seal_shrink` / `limit_up_open`（涨停打开=炸板）/ `limit_down_open`。`--cooldown <秒>` 控告警去重间隔。

### 3. 找板块龙头 / 按题材拉票

```bash
doyoutrade-cli data sectors --sector-type concept        # 先看有哪些题材
doyoutrade-cli data sector-members "白酒,半导体" --output /tmp/u.csv
```

`data.universe_path` 直接喂给 `stock screen`。这样你能拿到题材成分做进一步筛选——但注意：**"哪个是主线、连板梯队、题材热度排名"目前没有现成数据**（见下方边界），你能做的是拿到成分后自己用技术条件 / 涨幅排序找强势股。

### 4. 把打板 / 低吸想法写成可回测策略

短线直觉最值得做的一件事：**用真实回测数字验证它历史上到底行不行**，而不是拍脑袋。

```bash
# 让助手走创作流程：strategy authoring open → 写 on_bar 规则 → compile → finalize
# 然后回测（示例：某只票 2024 全年）
doyoutrade-cli backtest run --definition sd-… --params '{"window":14}' \
  --universe 600519.SH --range-start 2024-01-01 --range-end 2024-12-31
```

策略 SDK 是**日线级事件驱动**：`on_bar(df, ctx) -> Signal`，可以表达"昨涨停今接力""跌破 5 日线出场""N 日不新高止损"这类逻辑，卖出可带 `exit_reason`（`stop_loss` / `take_profit` / `signal`…）。

- 高换手 / 做 T / 短持策略**务必开启费用**：`--config-overrides '{"settings":{"fee_config":{"enabled":true}}}'`（回测默认不计佣金 / 印花税）。
- 想知道会不会过拟合：`backtest walk-forward`（固定参数多窗样本外验证）。
- ⚠️ **做不了 tick / 竞价级回测**：免费源只有日线，"涨停瞬间打板""集合竞价抢筹"这类微观结构无法在日线回测里如实还原。

### 5. 盘中纪律提醒（需 QMT）

买了票、给自己定个纪律，走坏了才提醒（否则静默不打扰）：

```bash
doyoutrade-cli cron create --name "纪律提醒-600519" \
  --cron-expression "50 14 * * mon-fri" --timezone Asia/Shanghai \
  --task-kind deviation_monitor \
  --task-params '{"strategy_definition_id":"sd-…","symbols":["600519.SH"],"thesis":"连阳、不破5日线，跌破止损就提醒我"}'
```

需要先写一个"偏离判定策略"（命中 `Signal.sell`、否则 `Signal.hold`）。只有偏离计划才提醒，并复述你当初的买入逻辑督促你按计划走。

### 6. 每日收盘复盘（自动存档）

```bash
doyoutrade-cli cron create --name "每日收盘复盘" \
  --cron-expression "30 15 * * mon-fri" --timezone Asia/Shanghai \
  --task-kind daily_review \
  --task-params '{"agent_id":"<asst-…>","user_request":"每天收盘后帮我复盘当天交易"}'
```

触发时预采集当日账户对账单（现金 / 持仓 / 当日成交，经 QMT）+ 私有知识库摘要，生成结构化复盘，写入 `journal/<年>/<日期>.md` 并推回会话。非交易日自动跳过。

### 7. 情绪周期 / 题材 / 龙头 私有记忆

DoYouTrade 有一个**本地私有知识库**（`~/.doyoutrade/knowledge`，不进 git / 不外传），天然按短线视角分区：

- `cycles/` —— 情绪周期 / 题材 / 龙头笔记
- `symbols/roles.md` —— 标的角色标签 + 策略匹配建议
- `trades/` —— 个人交割单
- `journal/` —— 每日复盘

对话里说"帮我把这只票的角色记到 knowledge / 记一下今天的复盘"即可写入；平时助手会主动检索它来回答"这票以前是什么角色 / 上一轮情绪周期我怎么记的"。**默认只读**，只有你明说才写。

### 8. 自选股盘中盯盘（控制台页）

控制台「自选股」页在接了 QMT 后展示实时盘口：股价、涨跌幅、成交额、**振幅、委比（一档近似）、距涨停、封单量**，数值列可排序（快速把最强 / 最贴板的票排到前面）。或用 CLI：

```bash
doyoutrade-cli watchlist quotes --tag 半导体
```

---

## 现在还做不到（诚实清单）

这些是短线圈高频依赖、但 DoYouTrade **目前没有**的数据 / 功能。列在这里是为了不让你踩空——它们在路线图上，但**还没实装**：

- **龙虎榜 / 游资席位**（营业部买卖、游资画像、按概念分类上榜）
- **连板梯队 / 涨停家数 / 情绪温度计**（市场级涨停统计、封板率 / 炸板率 / 晋级率、冰点↔高潮阶段判定）
- **集合竞价量能 / 弱转强**（竞价量比、高开幅度）
- **L2 逐笔 / 五档盘口**（免费源不覆盖；当前盘口只到一档封单量，需 QMT）
- **板块资金流 / 主力净流入细粒度**、**题材热度排名 / 主线判断**

为什么：免费数据源以**日线**为主，盘口 / tick / 竞价类数据要么需要 QMT，要么需要接入尚未落地的专用数据服务。我们宁可**少承诺**，也不做标题党。

---

## DoYouTrade 明确不做的事

- **不荐股、不预测涨跌、不承诺收益**。AI 只陈述工具查到的客观数据，或帮你把想法写成可回测策略用真实数字说话。
- **不托管任何资金**。实盘需要你自己接券商 QMT 并显式授权，且每一笔订单都经过你配置的风控 + 审批闸门，随时可停。
- **不做账户托管撮合 / 实盘收益排名 / 付费荐股 / 一键跟单**。DoYouTrade 是你自己的分析与回测工具，不是荐股社区。

---

## 免责声明

DoYouTrade 是**研究与教育用途**的开源项目，不构成任何投资建议。市场有风险，据此操作的一切后果由使用者自行承担。
