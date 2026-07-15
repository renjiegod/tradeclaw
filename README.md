<p align="center">
  <b>中文</b>
</p>

<h1 align="center">DoYouTrade</h1>

<p align="center">
  <b>面向 A 股短线的多智能体 AI 交易平台 · 对话即可 选股 · 盯盘打板 · 复盘沉淀 · 写策略回测</b>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/React%2018-20232A?style=for-the-badge&logo=react&logoColor=61DAFB" alt="React">
  <img src="https://img.shields.io/badge/Vite-646CFF?style=for-the-badge&logo=vite&logoColor=white" alt="Vite">
  <img src="https://img.shields.io/badge/TypeScript-3178C6?style=for-the-badge&logo=typescript&logoColor=white" alt="TypeScript">
  <img src="https://img.shields.io/badge/TimescaleDB-FDB515?style=for-the-badge&logo=timescale&logoColor=black" alt="TimescaleDB">
  <img src="https://img.shields.io/badge/OpenTelemetry-425CC7?style=for-the-badge&logo=opentelemetry&logoColor=white" alt="OpenTelemetry">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/🤖_AI_Agent-8A2BE2?style=flat-square" alt="AI Agent">
  <img src="https://img.shields.io/badge/📈_A股量化-E4405F?style=flat-square" alt="A-Share Quant">
  <img src="https://img.shields.io/badge/🧪_回测引擎-2E8B57?style=flat-square" alt="Backtest">
  <img src="https://img.shields.io/badge/🐝_多智能体-FF8C00?style=flat-square" alt="Multi-Agent">
  <img src="https://img.shields.io/badge/🔗_LLM_原生-00A67E?style=flat-square" alt="LLM Native">
  <img src="https://img.shields.io/badge/🔍_全链路可观测-425CC7?style=flat-square" alt="Observability">
  <img src="https://img.shields.io/badge/🛡️_风控审批-B22222?style=flat-square" alt="Risk & Approval">
  <img src="https://img.shields.io/badge/📟_QMT_实盘-1E90FF?style=flat-square" alt="QMT Live">
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-D22128?style=flat-square&logo=opensourceinitiative&logoColor=white" alt="License"></a>
  <img src="https://img.shields.io/badge/version-0.1.0-blue?style=flat-square" alt="Version">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey?style=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/PRs-welcome-brightgreen?style=flat-square" alt="PRs Welcome">
</p>

<p align="center">
  <a href="#-核心特性">核心特性</a> &nbsp;&middot;&nbsp;
  <a href="#-快速开始">快速开始</a> &nbsp;&middot;&nbsp;
  <a href="#-你能用它做什么">能做什么</a> &nbsp;&middot;&nbsp;
  <a href="#-打板复盘看板">打板复盘看板</a> &nbsp;&middot;&nbsp;
  <a href="#-私有交易记忆你的复盘工作台">私有记忆</a> &nbsp;&middot;&nbsp;
  <a href="#-为什么选它">为什么选它</a> &nbsp;&middot;&nbsp;
  <a href="#-免责声明">免责声明</a>
</p>

---

> 面向 A 股短线的**开源多智能体 AI 交易平台**。把看盘、盯盘打板、复盘沉淀、写策略回测收进一个对话界面，由单循环 `TradingWorker` 全程驱动、全链路留痕：
>
> ```
> 行情 → 策略 / Agent 分析 → 订单意图 → 风控 / 审批 → 执行 → 全链路 trace
> ```
>
> - 🗣 **听懂人话** — 「看看今天打板情绪 + 龙虎榜谁在买」「把'低吸次日反包'写成策略回测茅台 2024」，AI 自己拉真实数据、跑真实回测。
> - 🔒 **不编数字、本地私有** — 关键数字全部来自工具调用，LLM 绝不编造；自选 / 复盘 / 交割单 / 战法记忆只存在你自己机器，**不进 git、不外传**。
> - 🕹 **你握方向盘** — 不荐股、不预测、不承诺收益；实盘仅在你接入券商 QMT 并显式授权后可用，每笔订单必过风控 + 审批闸门，平台不托管资金。
>
> 详情见 [docs/design.md](docs/design.md)（架构）· [docs/short-term-playbook.md](docs/short-term-playbook.md)（短线上手）· [AGENTS.md](AGENTS.md)（贡献规范）。

---

## ✨ 核心特性

<div align="center">
<table align="center" width="94%">
  <tr>
    <td width="50%" valign="top">
      <h3>🤖 AI 原生工作流</h3>
      <div align="left">
        • 对话式选股 / 写策略 / 回测 / 迭代<br>
        • 内置 Strategy SDK + 编译期 AST 校验<br>
        • 关键事实全部来自工具调用，杜绝 LLM 编数
      </div>
    </td>
    <td width="50%" valign="top">
      <h3>🔥 打板情绪面板</h3>
      <div align="left">
        • 涨停 / 跌停 / 炸板家数 + 连板梯队 + 情绪温度计<br>
        • 龙虎榜 / 游资席位 · 资金流 · 题材热度榜<br>
        • 一页看清"今天情绪几度、主线在哪、谁在买"
      </div>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h3>🧠 私有交易记忆</h3>
      <div align="left">
        • 情绪周期时间线 · 个股角色卡 · 交割单归因 · 打板模式库<br>
        • 每日复盘自动沉淀，关键时刻主动唤起<br>
        • <b>本地私有 · 绝不进 git / 导出 / 外传</b>
      </div>
    </td>
    <td width="50%" valign="top">
      <h3>📊 多源行情 + 回测引擎</h3>
      <div align="left">
        • <code>qmt → baostock → akshare → tushare</code> 自动降级<br>
        • 免费源开箱即用，无需 token<br>
        • 回测与实盘同构，指标 / 报告 / run 卡片可复查
      </div>
    </td>
  </tr>
  <tr>
    <td width="50%" valign="top">
      <h3>🛡️ 渐进式执行 + 风控审批</h3>
      <div align="left">
        • 模拟盘 → 需审批实盘 → 受控自动实盘<br>
        • 订单意图优先，风控 / 审批独立于 Agent<br>
        • 实盘仅在接入 QMT 后可用，随时可停
      </div>
    </td>
    <td width="50%" valign="top">
      <h3>🔍 全链路可观测</h3>
      <div align="left">
        • <code>run_id</code> / <code>trace_id</code> 贯穿每一次 cycle<br>
        • OpenTelemetry span + 调试会话 + 模型调用记录<br>
        • 任何改变走向的失败都可见、可追溯
      </div>
    </td>
  </tr>
</table>
</div>

---

## 🚀 快速开始

### 最快：一条命令装完即用（推荐）

**macOS / Linux：**

```bash
curl -fsSL https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.sh | sh
```

**Windows（PowerShell）：**

一条命令装完即用：

```powershell
irm https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.ps1 | iex
```

想先审阅再执行（更稳，能绕开任何脚本传输 / 解析层面的意外）：

```powershell
irm https://raw.githubusercontent.com/renjiegod/doyoutrade/main/install.ps1 -OutFile install.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
```

脚本会自动：检测 / 安装 [uv](https://docs.astral.sh/uv/)（自带 Python 3.12，零前置）→ 把 `doyoutrade` 装成常驻命令。**Windows 版会一并装上内置 qmt-proxy**（含 xtquant），macOS / Linux 只装 DoYouTrade 本体。装完在你自己的终端运行：

```bash
doyoutrade
```

`doyoutrade` 默认按操作系统选择启动内容：**Windows → `--mode both`**（同一进程内起 DoYouTrade:8000 + 内置 qmt-proxy:8001，并自动把默认账户指向本机 qmt-proxy，登录 miniQMT 后实时行情开箱即用）；**macOS / Linux → `--mode doyoutrade`**（只跑本体，QMT 行情指向远程 Windows 上的 qmt-proxy）。可用 `--mode {both,doyoutrade,qmt-proxy}` 或环境变量 `DOYOUTRADE_LAUNCH_MODE` 覆盖。

首次启动会：**自动执行数据库迁移**（默认 SQLite，零外部依赖）→ 进入**安装向导**，在终端问你一个大模型供应商（DeepSeek / Kimi / 通义 / Anthropic / LM Studio / 自定义）+ API Key + 模型 ID，写好后**自动绑定默认智能体** → 启动服务。浏览器打开 `http://localhost:8000` 即是完整控制台（前端与 API **同源**，无需另起 Vite）。

- 已配置过模型则向导自动跳过，不再打扰。
- 非交互启动（后台 / Docker / CI）不会被向导阻塞：打印一行引导后照常启动，稍后在 `/settings/models` 配置即可。
- 安装机装有 Node.js 时会自动打包 Web UI；没有 Node 也能装，只是退化为「API + CLI」无网页界面。
- 安装脚本本身是交互式：若已安装过 doyoutrade，会先询问是否卸载重装；CI / 自动化请先下载脚本并加 `-Force`（见下方「谨慎用户」示例）。
- 升级 `uv tool upgrade doyoutrade`；卸载 `uv tool uninstall doyoutrade`。
- 想要真实 A 股行情、QMT 实盘等进阶能力，见下方梯度二 / 三。

> 谨慎用户可先下载脚本审阅再执行（`curl -fsSL … -o install.sh` → 查看 → `sh install.sh`）；Windows 用
> `powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1`（可加 `-Force` 跳过确认）。
> 已经装了 uv、想跳过脚本的话，等价的一条命令是
> `uvx --from git+https://github.com/renjiegod/doyoutrade doyoutrade`（即跑即用，不持久安装）。
> 从源码开发（改代码、跑测试）走下面的「梯度一：克隆源码本地开发」。

按需求选一个梯度即可，三个梯度层层递进、互不冲突。

### 环境要求

| 组件 | 要求 | 说明 |
|---|---|---|
| Python | >= 3.12 | 配合 [uv](https://docs.astral.sh/uv/) 管理依赖 |
| Node.js + npm | 较新的 LTS | 仅前端控制台需要 |
| 主数据库 | 默认 SQLite，可选 PostgreSQL | 默认 `sqlite+aiosqlite:///./data/doyoutrade.db`，无需安装 |
| 行情仓库数据库 | 默认 SQLite，可选 PostgreSQL + TimescaleDB | 默认 `sqlite+aiosqlite:///./data/market_bars.db`，无需安装；全市场同步 / 重度 5m 历史建议切 TimescaleDB |
| LLM API key | 自备 | `anthropic` / `openai_compatible`（DeepSeek、Kimi、通义等）/ `lmstudio` |
| 操作系统 | macOS / Linux / Windows | 仅 QMT 实盘链路的 miniQMT + qmt-proxy 必须跑在 Windows |

### 梯度一：克隆源码本地开发（mock 数据 + 模拟盘）

想改代码 / 跑测试 / 开发前端时走这条；只想用可直接看上面的「一条命令」。除了一个 LLM API key，不需要任何数据库服务、行情源 token、券商账户或 Windows——主库与行情仓库默认都是本地 SQLite 文件，零外部依赖。

**1. 克隆并安装依赖**

```bash
git clone https://github.com/renjiegod/doyoutrade.git
cd doyoutrade
make install        # = uv sync --extra doc-processing + npm --prefix frontend ci
```

**2.（可选）性能升级：TimescaleDB**

默认行情仓库是 `sqlite+aiosqlite:///./data/market_bars.db`，开箱即用。只有在开启全市场同步（`sync_full_market: true`）或重度使用 5m 历史时，才建议切换到 PostgreSQL + TimescaleDB（Docker 一条命令）：

```bash
docker run -d --name doyoutrade-timescaledb \
  -e POSTGRES_USER=doyoutrade -e POSTGRES_PASSWORD=doyoutrade -e POSTGRES_DB=doyoutrade \
  -p 5432:5432 timescale/timescaledb:latest-pg16
```

然后在仓库根写 config.yaml（与 `doyoutrade/default_config.yaml` 深合并，只写要覆盖的键）：

```yaml
market_data:
  database_url: postgresql+asyncpg://doyoutrade:doyoutrade@localhost:5432/doyoutrade
```

扩展与 hypertable 由启动迁移自动创建。完整可配项见 [config.yaml.example](config.yaml.example)（`config.yaml` 已 gitignore，不要提交真实凭据）。也可用 `DOYOUTRADE_CONFIG` 环境变量指向别处的配置文件。

> 配置文件解析优先级：`$DOYOUTRADE_CONFIG` > `~/.doyoutrade/config.yaml`（首启自动从打包默认播种、可在 Web UI「设置」页直接改）> 仓库根 `config.yaml` > 打包默认。装好后无需手写 YAML，多数配置在「设置」页改即可（改完部分字段需重启进程生效，页面会标「需重启」）；QMT 服务端配置（qmt-proxy）也在同一页改，落在 `~/.doyoutrade/qmt-proxy.yml`。

**3. 启动后端与前端**

```bash
uv run doyoutrade               # API server，默认 0.0.0.0:8000；启动自动执行两套数据库迁移
make frontend                  # 另开终端：Vite dev server，http://localhost:5173，API 自动代理到 :8000
```

**4. 配置 LLM（Provider + Route）**

模型供应商与路由存在数据库（不是 YAML）。推荐走前端：

1. 打开 `http://localhost:5173/settings/models`；
2. 新建 **Provider**：填 `provider_key`、`provider_kind`（`anthropic` / `openai_compatible` / `lmstudio`）、`api_key`，OpenAI 兼容接口再填 `base_url` 与 `target_model`；
3. 同页新建 **Route**（如 `default`）并关联该 provider；
4. 打开 `http://localhost:5173/agents`，编辑内置「默认智能体」（`agent_default`），把模型路由设为该 `route_name`。

配置结果可用 `uv run doyoutrade-cli route list` 验证。

**5. 跑通第一个回测**

见上文 [「一次典型对话」](#-一次典型对话)。关于 mock：`mock` 数据源生成确定性合成行情，**不代表真实市场**，仅用于验证链路；模拟盘走 `PaperExecutionAdapter`（内存撮合），不触碰任何真实资金。

### 梯度二：免费真实行情（默认行为，什么都不用装）

`auto` 链天然落在免 token 的 baostock / akshare，克隆完即可用真实 A 股行情：

```bash
# 启动 API server 后，另开终端：
uv run doyoutrade-cli data run 600519.SH --period 3m     # 拉贵州茅台最近 3 个月日线
uv run doyoutrade-cli stock lookup 茅台                    # 名称 / 代码 → canonical symbol
```

回测同理：把任务的 `--data-provider` 留成 `auto`（或显式 `akshare`）即可用真实历史行情。

**能力边界（免费源）**：以日线为主（baostock/akshare 提供分钟聚合 K 线，均为非实时历史查询）；无实盘账户 / 持仓 / 下单（执行仍是模拟盘）；实时行情、盘中监控、实盘交易需要梯度三的 QMT。有 [Tushare Pro](https://tushare.pro) token 时，设 `TUSHARE_TOKEN` 或 `data.tushare.token` 即自动纳入降级链。

### 梯度三：QMT 实时行情 / 实盘交易（可选）

只有需要**实时 / 分钟级推送行情**或**真实券商下单**时才走这一步。qmt-proxy 把 Windows-only 的 xtquant 封装为带鉴权的 REST 服务，链路：

```
DoYouTrade ── REST（base_url + Bearer token） ──▶ qmt-proxy ──▶ miniQMT（券商 QMT 终端）
```

自 v0.x 起 qmt-proxy 已**内置进 DoYouTrade 可执行体**，按你在哪台机器跑分两种部署：

**A. 全部在一台 Windows（推荐，零额外配置）**

1. 在 Windows 上安装并登录券商 miniQMT / QMT 量化终端。
2. 用上面的 Windows 一键脚本安装 DoYouTrade（已内置 qmt-proxy），直接运行 `doyoutrade`。默认 `--mode both`：同进程起 DoYouTrade:8000 与 qmt-proxy:8001，并**自动**把默认账户的 `base_url` 指向本机 `http://127.0.0.1:8001`——无需手工 `account create`，登录 miniQMT 后实时行情即刻可用。
3. 要走真实下单，把该账户在网页 Accounts 页由 `mock` 改为 `live` 并填券商资金账号，确认风控 / 审批后生效。

**B. DoYouTrade 在 macOS / Linux，qmt-proxy 在远程 Windows**

1. 在 Windows 上装好 DoYouTrade 并 `doyoutrade --mode qmt-proxy`（只跑内置行情代理，默认 `:8001`），记下其地址 `http://<windows-ip>:8001` 与 token（配置项 `qmt_proxy.local_token`，默认 `embedded-local`）。
2. 在你的 Mac / Linux 上运行 `doyoutrade`（`--mode doyoutrade`）。**首启向导**会问远程 qmt-proxy 地址，填入即自动登记默认账户；也可稍后手工登记（`accounts` 表是 QMT 连接的唯一来源）：

```bash
uv run doyoutrade-cli account create \
  --name "我的实盘" --mode live \
  --base-url http://<windows-ip>:8001 --token <qmt-proxy API token> \
  --qmt-account-id <券商资金账号> --default
```

要点：`--default` 账户同时供应全局行情连接，QMT 自动升为第一优先级数据源；`--mode mock` + `--base-url/--token` 是实用的中间形态——**行情走真实 QMT，交易仍是内存模拟组合**；`--mode live` 走真实下单。独立部署 / 多终端等进阶场景仍可用 [`qmt-proxy/`](qmt-proxy/) 自带的 `qmt-proxy/installer/install.ps1`（详见 [`qmt-proxy/README.md`](qmt-proxy/README.md)），常规路径用上面的合并安装即可。

### Windows / QMT 常见疑问

- **我必须有 Windows 机器吗？** 不必须。只有需要 QMT（实时行情 / 实盘）时才需要，因为 xtquant 只跑在 Windows。梯度一、二在 macOS / Linux / Windows 上都完整可用。
- **qmt-proxy 还要单独装吗？** 不用了——它已内置进 DoYouTrade。Windows 上 `doyoutrade` 默认 `--mode both` 会在同进程内一起启动；也可 `--mode qmt-proxy` 只跑行情代理。
- **DoYouTrade 本体要装在 Windows 上吗？** 不需要。可跑在任意机器上，通过局域网访问 Windows 上的 qmt-proxy（默认 `:8001`）即可。
- **端口是不是变了？** 内置 qmt-proxy 默认 `:8001`（DoYouTrade 占 `:8000`），可用 `qmt_proxy.port` 或 `--qmt-port` 改。
- **不配 QMT 会报错吗？** 不会。`auto` 链发现没有带 `base_url` 的默认账户时会静默跳过 QMT。

---
## ⚡ 你能用它做什么

| 你说 | 它产出 |
|------|--------|
| **问一个交易问题** | 结合真实行情、指标、研报、新闻的市场研究 |
| **写 & 回测一个策略想法** | 策略源码、回测指标、基准对比、迭代建议 |
| **拉数据 / 算指标 / 找形态** | 日线 / 分钟线 OHLCV、RSI/MACD/KDJ/布林、K 线形态、因子 IC/IR |
| **筛股 / 扫贴板股** | 超卖、均线金叉、放量突破、近似涨跌停（主板 10% / 创业科创 20% / 北交所 30% 自动分档）、形态匹配 |
| **看今天打板情绪** | 涨停 / 跌停 / 炸板家数、连板梯队、炸板率 → 情绪温度计（冰点↔高潮，透明阈值规则、单日快照、**非预测**）|
| **查龙虎榜 / 游资席位** | 每日上榜股 + 上榜原因 + 净买额；`--symbol` 拉营业部买卖席位明细（含游资标签库） |
| **看资金流 / 题材热度** | 个股 / 板块主力资金流排名、题材（概念 / 行业）板块涨幅热度榜与领涨股 |
| **盯盘异动（打板党刚需）** | 盘口盯盘规则：涨停 / 封单缩量 / 炸板打开命中即推飞书，tick 级、rising-edge 去重，不用一直守屏 |
| **每日收盘复盘** | 定时抓当日账户对账单 + 市场四维（情绪 / 主线 / 龙虎）+ 私有记忆，自动生成结构化复盘并存档 |
| **沉淀 & 唤起交易记忆** | 情绪周期 / 个股角色 / 交割单归因 / 打板战法，本地私有；下次聊到该票自动召回你的历史判断 |
| **组多智能体投研团** | `investment_committee`（多空辩论 + 风控）/ `quant_strategy_desk`（筛选 + 因子 + 回测 + 风险审计） |
| **接管调度 / 提醒** | Cron 定时任务（`--in` / `--at` / `--cron-expression`），到点自动跑一段对话或分析 |
| **接入实盘（可选）** | 通过 QMT 拿实时 / 分钟级行情，风控 + 审批后真实下单 |

---

## 🔥 打板复盘看板

控制台「**打板复盘**」页（`/market_review`）把短线看盘的四维一页看清——数据全部来自真实行情、缺数据显示 `—` **绝不编造**，情绪标签只描述当日状态、**非预测非荐股**：

| 区块 | 你看到什么 | 数据 |
|---|---|---|
| 🌡 **情绪温度计** | 涨停 / 跌停 / 炸板家数、炸板率、最高连板 → 冰点 / 中性 / 发酵 / 高潮 / 分歧 状态标签（透明阈值规则）| 免费（akshare 涨停池）|
| 🪜 **连板梯队** | 1 板 / 2 板 / 3 板…各多少家，越高板越醒目 | 免费 |
| 🐯 **龙虎榜** | 今日上榜股 + 上榜原因 + 净买额（红买绿卖），支持近 3 日 | 免费（akshare）|
| 💰 **资金流排名** | 个股 / 板块主力净流入榜（超大 / 大 / 中 / 小单）| 免费 |
| 🎯 **题材热度榜** | 概念 / 行业板块涨幅榜 + 领涨股 + 上涨家数——"主线在哪" | 免费 |

> akshare 免费源以日线 / 盘后快照为主；**盘口级实时封单、竞价、L2 需接 QMT**（见梯度三）。

---

## 🧠 私有交易记忆：你的复盘工作台

控制台「**知识库**」页（`/knowledge`）是一个**只属于你的**交易记忆库——KOL 靠常年手写复盘积累的东西，这里帮你自动沉淀、结构化、并在关键时刻主动唤起。**完全存在本机 `~/.doyoutrade/knowledge`，绝不进 git / 会话导出 / 回测报告 / 任何外传通道。**

| 区块 | 沉淀什么 | 怎么来 |
|---|---|---|
| 📈 **情绪周期时间线** | 每个交易日的情绪状态色带（退潮→高潮），看清自己正处周期哪一段 | 每日复盘 **自动累积** |
| 🏷 **个股角色卡** | 这票是龙头 / 中军 / 杂毛 / 事件型 + 你的策略备注 | 对话里"把这票记成龙头" |
| 📒 **交割单归因** | 券商交割单 → FIFO 回合配对 → 胜率 / 盈亏比 / 最赚模式 vs 要规避的错误 | 导入 `trades/` 券商 CSV |
| 📕 **打板模式库** | 你自己的战法总结：哪种打法在什么情绪阶段有效 | 对话里"记进模式库" |

> **写入默认只读**——只有你明说"记到 knowledge 里 / 更新这票角色"时才写，AI 不会擅自落盘你的判断。下次聊到某只票、或问"现在情绪周期"，助手会**先召回你的历史记忆再作答**（是"你当时怎么看"，不是"所以现在买"）。

---

## 🎯 短线玩家：把你的战法接到 DoYouTrade

如果你是做**短线 / 打板 / 追龙头 / 每天复盘**的玩家，下面是你的日常动作到 DoYouTrade 现有能力的对照。**这里只列已经实装、跑得起来的功能**——用什么、免费还是需要 QMT，一清二楚：

| 你的动作 | 用它 | 数据要求 |
|---|---|---|
| 扫今天贴近涨停 / 近似封板的票 | `stock screen --limit-up-approx`（主板/创业科创/北交所阈值自动分档） | 免费日线即可 |
| 看今天大盘打板情绪 / 连板梯队 | `data breadth`（涨停/跌停/炸板家数 + 连板梯队 + 情绪温度计） | 免费（akshare） |
| 查龙虎榜 / 跟踪游资席位 | `data lhb`（每日上榜）/ `data lhb --symbol <票> --date`（营业部席位 + 游资标签） | 免费（akshare） |
| 看主力 / 板块资金流 | `data fund-flow --scope individual\|sector` | 免费（akshare） |
| 看题材 / 板块热度找主线 | `data sector-heat --sector-type concept\|industry`（涨幅榜 + 领涨股） | 免费（akshare） |
| 找板块龙头 / 按题材拉一篮子票 | `data sector-members "半导体,白酒"` → 生成 universe 再 `stock screen` | 免费（akshare） |
| 盯盘：涨停 / 封单缩量 / 炸板打开，命中推飞书 | `monitor create --preset limit_up_open\|limit_up_seal_shrink\|...`（tick 级、去重） | 需 QMT 实时行情 |
| 每天收盘自动复盘并存档 | `cron create --task-kind daily_review`（市场四维 + 账户 + 记忆 → `journal/`）| 复盘框架免费；当日成交对账单需 QMT |
| 沉淀情绪周期 / 个股角色 / 打板战法、复盘交割单 | 控制台「知识库」复盘工作台（对话里"记到 knowledge"）| 免费、本地私有 |
| 把"低吸次日反包 / N 连板断板出场"写成可回测策略 | `strategy authoring` + `backtest run`，用**真实回测数字**代替拍脑袋 | 免费日线回测 |
| 自选股盘中看盘口（涨跌幅 / 成交额 / 振幅 / 距涨停 / 封单） | 控制台「自选股」页 + `watchlist quotes` | 需 QMT 实时行情 |

> 完整的「战法 → DoYouTrade 怎么用」映射、示例命令与边界说明见 **[docs/short-term-playbook.md](docs/short-term-playbook.md)**。

**诚实的边界——免费源（akshare / baostock）以日线 / 盘后快照为主，目前还做不到**：集合竞价量能 / 弱转强、L2 逐笔 / 五档盘口实时封单、**tick / 竞价级回测**——这些微观结构都要接 **QMT**（见梯度三），其中竞价 / L2 仍在路线图上。eastmoney 免费接口偶发限流，命中时命令会优雅降级并如实报错，**绝不编造数据**。

**DoYouTrade 明确不做的事**：不荐股、不预测涨跌、不承诺收益（AI 只陈述工具查到的客观数据，关键数字不由模型编造）；不托管资金，实盘要你自己接 QMT、且每一笔都过风控 + 审批闸门；不做账户托管撮合 / 实盘收益排名 / 付费荐股 / 一键跟单——它是你自己的分析与回测工具，不是荐股社区。

---

## 🛡️ 为什么选它

散户被"荐股诈骗、黑 V 晒单造假、黑盒量化"坑怕了。DoYouTrade 的差异化恰恰是这几件**别人给不了**的事：

- **🚫 不编数字、可溯源** — AI 只转述工具查到的真实行情 / 回测，绝不自己造数；每次运行 `run_id` / `trace_id` 一线到底，能查到是哪个 symbol、什么区间、算出了什么。对比"截图晒单 / 隔空喊单"——这里**一切可复现**。
- **🔒 本地私有** — 你的自选、复盘、交割单、战法记忆只存在你自己机器（SQLite + 本地知识库），**不托管、不上传、不进 git**。
- **🕹️ 你握方向盘** — 不荐股、不预测、不承诺收益；实盘要你亲自接 QMT + 每单人工审批，随时可停。**不做**账户托管 / 收益排名 / 付费荐股 / 一键跟单。
- **📦 开箱即用 + 全开源** — 免 token 免费源克隆即用（baostock / akshare），MIT，每一行代码可审。

> 一句话：**它把关键事实交给工具、把决策权交给你、把你的记忆留在你手里。**

---

## 🧪 一次典型对话

打开控制台 `http://localhost:5173/assistant`，或用 CLI 一次性验证完整对话链路：

```bash
uv run doyoutrade-cli assistant run \
  --agent-id agent_default \
  --message "帮我写一个简单的双均线策略，用 mock 数据回测 600000.SH 2024 年全年" \
  --output /tmp/doyoutrade-chat-export.md
```

助手会自动串起「写策略 → 编译校验 → 创建回测任务 → 跑回测 → 汇报报告」全流程，并把本轮涉及的 span、模型调用、工具调用导出到 Markdown，方便复盘。

---

## 📡 数据源与自动降级

`data.default_provider` 默认 `auto`，取数链按优先级自动降级——**未配置鉴权的源自动跳过**，所以全新安装天然落在免 token 的 baostock / akshare，克隆完即可用真实 A 股行情。

| 数据源 | 覆盖 | 鉴权 | 角色 |
|--------|------|------|------|
| `qmt` | A 股 | 券商账户 + qmt-proxy | 实时 / 分钟级推送 + 实盘下单（梯度三） |
| `baostock` | A 股（+ 分钟聚合） | 无 | 免 token 免费源，开箱即用 |
| `akshare` | A 股（+ 1m 分钟线） | 无 | 免 token 免费源，开箱即用 |
| `tushare` | A 股 / 期货 / 基金 / 宏观 | token | 可选增强，配了 token 自动纳入降级链 |
| `mock` | 合成行情 | 无 | 确定性合成数据，仅用于验证链路 |

**默认降级链**：`qmt → baostock → akshare → tushare`。QMT 需要默认账户里带 `base_url`，tushare 需要 token；两者缺失时静默跳过，直接用免费源，**不会报错**。

> 除 OHLCV 外，还内置**短线数据轴**——打板情绪面板（涨停池 / 连板梯队 / 情绪温度计）、龙虎榜（含营业部席位 + 游资标签）、资金流排名（个股 / 板块）、题材 / 板块热度榜；以及个股新闻、券商研报（评级 / EPS·PE 盈利预测）、业绩预告 / 快报等只读工具，和 RSI/MACD/KDJ/CCI/布林/SuperTrend、近似涨跌停等指标与 K 线形态 / 趋势识别。

---

## 🔩 详细能力

<details>
<summary><b>内置能力模块</b> <sub>单循环运行时的完整链路</sub></summary>

- **策略层** — `strategy_sdk`（`Strategy` + `populate_indicators` + `on_bar → Signal`）、`strategy_runtime` 编译器（AST 校验、防前视、`required_history` 一致性检查）
- **执行层** — `execution`：订单意图校验 → 风控引擎 → 审批闸门 → 执行适配器（`PaperExecutionAdapter` 内存撮合 / `QmtExecutionAdapter` 真实下单）
- **回测引擎** — `backtest`：与实盘同构，支持 walk-forward / 样本外验证 / 迭代建议
- **AI 助手** — `assistant`：主 Agent + in-process 工具 + CLI 工具面 + Skill 加载 + Cron 调度
- **多智能体 swarm** — `swarm`：DAG 编排 + preset 投研团（investment_committee / quant_strategy_desk）
- **可观测性** — `observability` / `debug`：OpenTelemetry span 导出、调试会话、模型调用记录，全部按 `run_id` 贯穿

</details>

<details>
<summary><b>React 控制台页面</b> <sub>frontend/ · React 18 + Vite + TypeScript</sub></summary>

| 页面 | 用途 |
|------|------|
| Assistant | 对话式选股 / 写策略 / 回测，含 trace / 工具调用 / debug 导出 |
| **MarketReview（打板复盘）** | 情绪温度计 / 连板梯队 / 龙虎榜 / 资金流 / 题材热度一页看盘 |
| **Knowledge（复盘工作台）** | 情绪周期时间线 / 个股角色卡 / 交割单归因 / 打板模式库（本地私有）|
| Tasks / TaskDetail | 交易任务生命周期、回测配置与结果 |
| Strategies | 策略定义（`sd-…`）管理与源码查看 |
| Accounts | QMT 连接与账户配置（`accounts` 表唯一来源） |
| Agents / ModelSettings | 智能体配置、模型 Provider / Route |
| Settings（设置） | 系统全局配置（`~/.doyoutrade/config.yaml`）与 QMT 服务端配置（qmt-proxy）在网页里直接改 |
| Approvals | 需审批订单的人工审批 |
| Stocks / StockDetail / StockMonitor | 行情、个股详情、盘中监控 |
| Watchlist / CronJobs | 自选股（含盘口列）、定时任务 |
| ModelInvocations / Swarm | 模型调用日志、多智能体运行 |

</details>

<details>
<summary><b>可观测性贯穿关系</b> <sub>run_id 一线到底</sub></summary>

每一次 cycle / 回测 / 模型调用都可按 `run_id` 或 OpenTelemetry `trace_id` 追溯，贯穿以下持久化：

```
cycle_runs ↔ debug_sessions ↔ debug_session_spans ↔ model_invocations ↔ trade_fills
```

排查时用 `doyoutrade-cli debug get-run-view <run_id>` / `get-trace-view <trace_id>` 拉三层 span（HTTP / 数据层 / SDK 层），定位是哪个 symbol、什么区间、超时还是业务错误。

</details>

---

## 🖥 CLI 参考

`doyoutrade-cli` 与 AI 助手同款工具面，默认连接 `http://127.0.0.1:8000`（用 `DOYOUTRADE_API_URL` 覆盖）。建议先启动 API server 再用 CLI。

```bash
uv run doyoutrade-cli --help
```

| 命令域 | 用途 |
|--------|------|
| `task` | 交易任务生命周期：创建 / 启动 / 暂停 / 停止 / 克隆 |
| `strategy` | 策略定义查看、更新元数据、绑定 / 提升到任务 |
| `backtest` | 跑回测、盯 run、拉报告、迭代建议、walk-forward |
| `data` / `analysis` | 拉 OHLCV、算指标、形态识别、因子分析；短线数据轴 `data breadth`（打板情绪）/ `lhb`（龙虎榜）/ `fund-flow`（资金流）/ `sector-heat`（题材热度）|
| `stock` / `watchlist` | symbol 查询、筛股、自选股 |
| `account` | QMT 连接与账户配置 |
| `cron` | 定时 / 延时任务（`--in` / `--at` / `--cron-expression`） |
| `assistant` | 真实对话验证与会话导出 |
| `debug` / `cycle` / `route` | run 调试视图、trace 追踪、模型调用日志、模型路由 |
| `swarm` / `knowledge` | 多智能体投研团、私有知识库 |

### 常用 Make 目标

```bash
make install        # 安装 Python + 前端依赖
make migrate        # 手动执行主库迁移（server 启动时自动做）
make backend        # migrate + 启动 API server
make frontend       # 前端 dev server（:5173）
make build          # 前端 dist/ + Python wheel/sdist
make test           # Python 单元测试（stdlib unittest）
make test-e2e       # 端到端测试（isolated profile）
```

---

## ⚙️ 配置速览

- **查找顺序**：`DOYOUTRADE_CONFIG` → 当前目录 `config.yaml` → 仓库根 `config.yaml` → 内置 `doyoutrade/default_config.yaml`；深合并，只写需覆盖的键。
- **数据库**：主库 `database.url` 与行情仓库 `market_data.database_url` 均默认本地 SQLite（`./data/doyoutrade.db` / `./data/market_bars.db`），零外部依赖；行情仓库可选切换 PostgreSQL + TimescaleDB（全市场同步 / 重度 5m 历史推荐）。两个 URL 可指向同一 Postgres 库。
- **数据源**：`data.default_provider`（`auto` / `mock` / `qmt` / `akshare` / `baostock` / `tushare`），任务级可用 `--data-provider` 覆盖。
- **模型**：每条模型配置自包含（适配器类型 + base_url + api_key + 模型 ID），由 `route_name` 供实例 / 回测 / Agent 引用，存数据库（`/settings/models` 或 `POST /model-routes`），YAML 不再支持 `model` / `providers` 顶层键。
- **QMT 连接**：只存在 `accounts` 表（`doyoutrade-cli account` / 前端 Accounts 页 / `/accounts` API）。
- **飞书通道（可选）**：`feishu.enabled: true` + `app_id` / `app_secret` 后，可通过飞书对话、审批、告警。

---

## 📁 项目结构

```
doyoutrade/
├── core/            # 单循环 TradingWorker + CycleRunState（run_id 起点）
├── strategy_sdk/    # 策略 SDK：Strategy / populate_indicators / on_bar → Signal
├── strategy_runtime/# 策略编译器：AST 校验、防前视、required_history 一致性
├── execution/       # 订单意图 → 风控 → 审批 → 执行适配器（paper / QMT）
├── data/            # 多源数据层与 auto 降级（mock/baostock/akshare/tushare/qmt）
├── backtest/        # 回测引擎（与实盘同构）
├── assistant/       # AI 助手主 Agent、prompt、工具面
├── swarm/           # 多智能体 DAG 编排 + preset 投研团
├── tools/           # in-process 工具（含 _contract / _coercion / _identifier_kinds）
├── observability/   # OpenTelemetry 初始化 + debug span 导出
├── debug/           # 调试会话与结构化事件
├── models/          # 模型调用记录（model_invocations）
├── persistence/     # SQLAlchemy 模型 / repository / serializer
├── api/             # FastAPI app
└── cli/             # doyoutrade-cli（与助手同款工具面）

frontend/            # React 18 + Vite + TypeScript 控制台
qmt-proxy/           # Windows 端 QMT REST 代理（含一键安装脚本）
alembic/             # 数据库迁移
docs/                # 设计文档、E2E 测试指南
tests/               # stdlib unittest + E2E
```

---

## 🧭 测试与文档

```bash
make test                              # Python 单元测试
make test-e2e                          # E2E（isolated profile：临时 SQLite + mock + stub 模型）
npm --prefix frontend run build        # 前端 type check
npm --prefix frontend run test         # 前端 vitest
```

- 总体设计：[docs/design.md](docs/design.md)
- E2E 指南：[docs/e2e-testing.md](docs/e2e-testing.md)
- 贡献者 / Agent 工作规范：[AGENTS.md](AGENTS.md)

---

## ⚠️ 免责声明

DoYouTrade 是一个**研究与教育用途**的开源项目，不构成任何投资建议。平台不托管任何资金；实盘交易需你自行接入券商 QMT 并显式授权，且每一笔订单都会经过你配置的风控与审批闸门。市场有风险，据此操作的一切后果由使用者自行承担。

## 📄 License

[MIT](LICENSE)
