# GitHub 开源项目调研汇总（AI / 量化 / 股票 / 加密 / 实盘）

> **说明**：本文档按调研方向各精选 **10** 个代表性仓库；**Stars** 为撰写时通过 GitHub API 读取的数值（约 2026-03-21），会随时间变化。  
> **技术栈**：以 GitHub 标注的主语言为主，并补充 `topics` 中与投研/交易相关的关键词。

---

## 1. AI / 机器学习 / 强化学习量化

面向用 **监督学习、深度学习、强化学习** 做策略研究、训练与实验的项目与框架。


| #   | 仓库                                                                                            | Stars  | 简介                                            | 技术栈（主语言 / 要点）                                    |
| --- | --------------------------------------------------------------------------------------------- | ------ | --------------------------------------------- | ------------------------------------------------ |
| 1   | [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL)                 | 14,243 | 金融强化学习（DRL）框架，面向股票等交易任务与算法实验。                 | Python / Jupyter；DRL、PyTorch、TensorFlow、Gym、股票交易 |
| 2   | [AI4Finance-Foundation/FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading) | 2,752  | FinRL-X：面向量化交易的模块化基础设施（PPO、DDPG、A2C 等）。       | Python；深度强化学习、OpenAI Gym、股票交易                    |
| 3   | [AI4Finance-Foundation/FinRL-Meta](https://github.com/AI4Finance-Foundation/FinRL-Meta)       | 1,814  | 为 FinRL 提供动态数据集与交易市场环境。                       | Python；DRL、Gym 环境                                |
| 4   | [microsoft/qlib](https://github.com/microsoft/qlib)                                           | 39,137 | AI 导向的量化投研平台：数据、模型、因子与多种 ML 管线（含 RL、市场建模等）。   | Python；深度学习、量化数据集、模型、研究                          |
| 5   | [tensortrade-org/tensortrade](https://github.com/tensortrade-org/tensortrade)                 | 6,069  | 强化学习交易智能体：训练、评估与部署流程。                         | Python；强化学习、交易 Agent                             |
| 6   | [Ceruleanacg/Personae](https://github.com/Ceruleanacg/Personae)                               | 1,399  | DRL 与监督学习在量化交易中的实现与环境。                        | Python；强化学习、监督学习、股价预测                            |
| 7   | [edtechre/pybroker](https://github.com/edtechre/pybroker)                                     | 3,240  | Python 算法交易框架，强调与机器学习结合。                      | Python；机器学习、回测、股票、加密货币                           |
| 8   | [AminHP/gym-anytrading](https://github.com/AminHP/gym-anytrading)                             | 2,372  | 通用 Gym 风格交易环境，便于对接 RL 算法。                     | Python；OpenAI Gym、强化学习、外汇、股票                     |
| 9   | [Yvictor/TradingGym](https://github.com/Yvictor/TradingGym)                                   | 1,852  | 交易与回测环境，用于训练 RL 或规则策略。                        | Python；回测、强化学习、交易模拟                              |
| 10  | [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent)                                   | 11,967 | 研发自动化 Agent（R&D Agent），与 qlib 等结合可加速数据与模型侧迭代。 | Python；LLM、Agent、自动化、数据科学                        |


---

## 2. 数据、分析与 Agent 基础设施

**行情与基本面数据获取、金融分析库、可视化与 AI Agent** 相关基础设施（偏「数据 + 工具」而非完整策略框架）。


| #   | 仓库                                                                                                                  | Stars  | 简介                             | 技术栈（主语言 / 要点）                      |
| --- | ------------------------------------------------------------------------------------------------------------------- | ------ | ------------------------------ | ---------------------------------- |
| 1   | [OpenBB-finance/OpenBB](https://github.com/OpenBB-finance/OpenBB)                                                   | 63,380 | 面向分析师、量化与 AI Agent 的金融数据平台。    | Python；股票、期权、固收、机器学习、AI            |
| 2   | [ranaroussi/yfinance](https://github.com/ranaroussi/yfinance)                                                       | 22,251 | 从 Yahoo Finance 拉取行情与财务数据。     | Python；pandas、市场数据                 |
| 3   | [pydata/pandas-datareader](https://github.com/pydata/pandas-datareader)                                             | 3,169  | 从多种互联网数据源拉取数据到 pandas。         | Python；FRED、股票数据、金融数据              |
| 4   | [akfamily/akshare](https://github.com/akfamily/akshare)                                                             | 17,547 | 中文财经数据接口库（A 股、期货、宏观等）。         | Python；量化、财经数据                     |
| 5   | [TA-Lib/ta-lib-python](https://github.com/TA-Lib/ta-lib-python)                                                     | 11,800 | TA-Lib 的 Python 封装，用于技术指标。     | Cython / Python；技术分析、量化            |
| 6   | [google/tf-quant-finance](https://github.com/google/tf-quant-finance)                                               | 5,264  | TensorFlow 量化金融高性能数值库。         | Python；TensorFlow、GPU、数值方法         |
| 7   | [alvarobartt/investpy](https://github.com/alvarobartt/investpy)                                                     | 1,811  | 从 Investing.com 提取金融数据。        | Python；历史行情、金融数据                   |
| 8   | [mariostoev/finviz](https://github.com/mariostoev/finviz)                                                           | 1,241  | Finviz 非官方 API（爬虫/抓取）。         | Python；筛选、图表、CSV                   |
| 9   | [lit26/finvizfinance](https://github.com/lit26/finvizfinance)                                                       | 1,270  | Finviz 分析 Python 库。            | Jupyter Notebook / Python；基本面、技术筛选 |
| 10  | [Barca0412/Introduction-to-Quantitative-Finance](https://github.com/Barca0412/Introduction-to-Quantitative-Finance) | 1,271  | 多因子与 AI+金融资料整理（LLM、Agent、评测等）。 | Python；量化、LLM、Agent                |


---

## 3. 回测、策略与量化工程框架

**回测引擎、事件驱动框架、多资产策略与工程化** 为主的仓库（研究到部署的「骨架」层）。


| #   | 仓库                                                                                                              | Stars  | 简介                                | 技术栈（主语言 / 要点）                 |
| --- | --------------------------------------------------------------------------------------------------------------- | ------ | --------------------------------- | ----------------------------- |
| 1   | [mementum/backtrader](https://github.com/mementum/backtrader)                                                   | 20,855 | Python 策略回测库，生态成熟。                | Python；回测、交易                  |
| 2   | [polakowo/vectorbt](https://github.com/polakowo/vectorbt)                                                       | 6,933  | 向量化高速回测与可视化。                      | Python；回测、机器学习、加密货币           |
| 3   | [quarkfin/qf-lib](https://github.com/quarkfin/qf-lib)                                                           | 902    | 事件驱动回测与量化工具，可对接多类数据与经纪商。          | Python；回测、股票、加密货币、期货          |
| 4   | [QuantConnect/Lean](https://github.com/QuantConnect/Lean)                                                       | 17,977 | QuantConnect 开源算法引擎（C# / Python）。 | C# / Python；算法交易、多市场          |
| 5   | [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader)                             | 21,323 | Rust 原生、事件驱动、生产级交易引擎（Python 绑定）。  | Rust / Python；实盘、外汇、加密、权益     |
| 6   | [hudson-and-thames/mlfinlab](https://github.com/hudson-and-thames/mlfinlab)                                     | 4,618  | 可复现的金融机器学习工具（标签、组合等）。             | Python；金融机器学习、组合管理            |
| 7   | [zvtvz/zvt](https://github.com/zvtvz/zvt)                                                                       | 4,030  | 模块化量化框架（数据、回测、因子等）。               | Python；回测、股票、加密货币             |
| 8   | [je-suis-tm/quant-trading](https://github.com/je-suis-tm/quant-trading)                                         | 9,484  | 多种经典策略与技术指标示例（Python）。            | Python；期权、配对交易、统计套利等          |
| 9   | [akfamily/akquant](https://github.com/akfamily/akquant)                                                         | 558    | Rust + Python 高性能量化研究与回测框架。       | Python / Rust；回测、与 AKShare 生态 |
| 10  | [coding-kitties/investing-algorithm-framework](https://github.com/coding-kitties/investing-algorithm-framework) | 700    | 开发、回测与部署自动化交易算法与机器人。              | Python；回测、加密货币、量化交易           |


---

## 4. 资源索引、教程与书籍配套代码

**Awesome 列表、在线课程式笔记、金工数值与中文资料**，适合系统浏览与按主题扩展阅读。


| #   | 仓库                                                                                                                | Stars  | 简介                                              | 技术栈（主语言 / 要点）                        |
| --- | ----------------------------------------------------------------------------------------------------------------- | ------ | ----------------------------------------------- | ------------------------------------ |
| 1   | [wilsonfreitas/awesome-quant](https://github.com/wilsonfreitas/awesome-quant)                                     | 25,025 | 量化金融库与资源精选列表。                                   | Jupyter Notebook；awesome-list、多类工具索引 |
| 2   | [paperswithbacktest/awesome-systematic-trading](https://github.com/paperswithbacktest/awesome-systematic-trading) | 7,344  | 系统化交易相关库、策略与读物。                                 | Python；awesome-list、期货、回测            |
| 3   | [firmai/financial-machine-learning](https://github.com/firmai/financial-machine-learning)                         | 8,453  | 金融机器学习工具与应用索引。                                  | Python；机器学习、量化                       |
| 4   | [georgezouq/awesome-ai-in-finance](https://github.com/georgezouq/awesome-ai-in-finance)                           | 5,500  | 金融市场中的 LLM、深度学习与工具列表。                           | 多语言；深度学习、强化学习                        |
| 5   | [thuquant/awesome-quant](https://github.com/thuquant/awesome-quant)                                               | 5,128  | 中国 Quant 相关资源索引。                                | 多语言；Python、R、C++                     |
| 6   | [wangzhe3224/awesome-systematic-trading](https://github.com/wangzhe3224/awesome-systematic-trading)               | 3,720  | 系统化交易资源（中英文）。                                   | HTML；加密、股票、期货                        |
| 7   | [EliteQuant/EliteQuant](https://github.com/EliteQuant/EliteQuant)                                                 | 3,759  | 量化建模、交易与组合管理在线资源列表。                             | 多语言；资产定价、机器学习                        |
| 8   | [stefan-jansen/machine-learning-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading)       | 16,796 | 《Machine Learning for Algorithmic Trading》配套代码。 | Jupyter Notebook；深度学习、交易策略           |
| 9   | [cantaro86/Financial-Models-Numerical-Methods](https://github.com/cantaro86/Financial-Models-Numerical-Methods)   | 6,731  | 量化金融模型与数值方法（交互式 Notebook）。                      | Jupyter Notebook；随机过程、期权定价、蒙特卡洛      |
| 10  | [hugo2046/QuantsPlaybook](https://github.com/hugo2046/QuantsPlaybook)                                             | 4,633  | 券商金工研报复现（中文量化研究）。                               | Jupyter Notebook；A 股、策略              |


---

## 5. 股票与多市场实盘 / 接入

偏 **券商与柜台 API、交易机器人框架、多市场执行引擎**（含 A 股工具与 IB/Alpaca 等）。


| #   | 仓库                                                                                                                  | Stars  | 简介                                                           | 技术栈（主语言 / 要点）              |
| --- | ------------------------------------------------------------------------------------------------------------------- | ------ | ------------------------------------------------------------ | -------------------------- |
| 1   | [vnpy/vnpy](https://github.com/vnpy/vnpy)                                                                           | 38,090 | 基于 Python 的量化交易开发框架，支持对接国内常见交易通道（需按网关与合规配置）。                 | Python；期货、期权、量化            |
| 2   | [ib-api-reloaded/ib_async](https://github.com/ib-api-reloaded/ib_async)                                             | 1,428  | Interactive Brokers API 的 Python 同步/异步封装（接替 ib_insync 维护路线）。 | Python； asyncio、IBKR       |
| 3   | [erdewit/ib_insync](https://github.com/erdewit/ib_insync)                                                           | 3,236  | IB API 经典封装（仓库已 archived，新项目优先考虑 ib_async）。                  | Python；Interactive Brokers |
| 4   | [alpacahq/alpaca-trade-api-python](https://github.com/alpacahq/alpaca-trade-api-python)                             | 1,861  | Alpaca 交易与行情 API 的 Python 客户端。                               | Python；REST、WebSocket、美股   |
| 5   | [alpacahq/alpaca-backtrader-api](https://github.com/alpacahq/alpaca-backtrader-api)                                 | 686    | Alpaca 与 backtrader 集成，便于回测与实盘衔接。                            | Python；backtrader、Alpaca   |
| 6   | [StockSharp/StockSharp](https://github.com/StockSharp/StockSharp)                                                   | 9,294  | .NET 算法交易与量化平台（股票、外汇、加密等）。                                   | C#；FIX、多经纪商                |
| 7   | [myhhub/stock](https://github.com/myhhub/stock)                                                                     | 11,981 | A 股数据、指标、选股、回测与自动交易相关（以项目文档为准）。                              | Python；回测、量化               |
| 8   | [brokermr810/QuantDinger](https://github.com/brokermr810/QuantDinger)                                               | 1,034  | AI 驱动、本地优先的量化研究与实盘执行平台（开源）。                                  | Python；回测、LLM、多 Agent      |
| 9   | [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader)                                 | 21,323 | 生产级 Rust 引擎 + Python，支持多资产实盘与回测。                             | Rust / Python；权益、加密、外汇     |
| 10  | [jamesmawm/High-Frequency-Trading-Model-with-IB](https://github.com/jamesmawm/High-Frequency-Trading-Model-with-IB) | 2,899  | 基于 IB API 的配对交易与均值回归示例（研究向）。                                 | Python；Interactive Brokers |


---

## 6. 加密货币与实盘

**交易所 API、开源机器人、做市/套利** 等加密方向（实盘前务必做好权限、风控与密钥安全）。


| #   | 仓库                                                                                  | Stars  | 简介                                   | 技术栈（主语言 / 要点）                       |
| --- | ----------------------------------------------------------------------------------- | ------ | ------------------------------------ | ----------------------------------- |
| 1   | [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade)                       | 47,879 | 开源加密货币交易机器人，支持回测与实盘部署。               | Python；Telegram、多交易所                |
| 2   | [ccxt/ccxt](https://github.com/ccxt/ccxt)                                           | 41,448 | 统一封装 100+ 交易所 API（多语言）。              | Python / JS / 等；REST、交易             |
| 3   | [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot)                   | 17,794 | 高频做市、套利等策略的开源机器人框架。                  | Python / Cython；DEX、做市、套利           |
| 4   | [jesse-ai/jesse](https://github.com/jesse-ai/jesse)                                 | 7,564  | Python 加密交易框架（策略、回测与实盘工作流）。          | Python（官方描述；仓库语言统计可能含较多前端 JS）；加密、回测 |
| 5   | [sammchardy/python-binance](https://github.com/sammchardy/python-binance)           | 7,104  | Binance API 的 Python 实现。             | Python；WebSocket、REST               |
| 6   | [Drakkar-Software/OctoBot](https://github.com/Drakkar-Software/OctoBot)             | 5,486  | 开源加密机器人：Grid、DCA、TradingView 等，多交易所。 | Python；AI 交易、回测                     |
| 7   | [51bitquant/howtrader](https://github.com/51bitquant/howtrader)                     | 890    | 量化框架：策略开发、回测与 Binance/OKX 等执行。       | Python；TradingView、vnpy 生态          |
| 8   | [51bitquant/bitquant](https://github.com/51bitquant/bitquant)                       | 1,128  | 数字货币量化教程与 CCXT 爬虫、交易机器人示例。           | Python；CCXT、加密货币                    |
| 9   | [freqtrade/freqtrade-strategies](https://github.com/freqtrade/freqtrade-strategies) | 4,954  | 社区 Freqtrade 策略集合。                   | Python；策略                           |
| 10  | [iterativv/NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity) | 2,973  | 针对 Freqtrade 的成熟策略之一（使用须自行评估风险）。     | Python；Freqtrade                    |


---

## 交叉说明

- 同一仓库可能同时出现在 **「框架」** 与 **「实盘」**（如 `nautilus_trader`），因兼具回测与生产执行能力。  
- **实盘** 依赖账户、交易所规则、合规与网络，开源项目仅提供技术参考，不构成投资建议。  
- 若需只维护一份列表，可在本文件基础上按资产类别（股票 / 加密）或阶段（数据 → 研究 → 回测 → 实盘）自行打标签。

---

## 7. 源码、原理与技术栈详解

> **分析方法**：2026-03-22 对文中仓库做了一轮源码级快速扫读，依据仓库 README、构建文件（`pyproject.toml` / `setup.py` / `Cargo.toml` / `package.json` / `.sln` 等）、顶层目录与核心模块命名进行归纳。  
> **阅读边界**：以下“实现原理”是结合源码结构做的工程化总结，适合判断项目是否值得深入，不等同于逐文件逐函数审计。  
> **重复项目**：`nautilus_trader` 同时出现在“框架”和“实盘”列表中；下文保留其在各自场景下的解释，但源码结构描述保持一致。

### 7.1 AI / 机器学习 / 强化学习量化


| 仓库                                                                                            | 核心源码结构                                                                                   | 实现原理                                                                                        | 技术栈补充                                                                                 |
| --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| [AI4Finance-Foundation/FinRL](https://github.com/AI4Finance-Foundation/FinRL)                 | `finrl/agents`、`finrl/meta`、`applications`、`train.py`、`trade.py` 组成“数据/环境/训练/交易”闭环。      | 把行情和账户状态封装成 Gym 风格环境，再把 PPO、DDPG、A2C 等 DRL 算法接到统一训练入口；本质是研究型 RL pipeline，而不是低延迟执行引擎。        | Python；Stable-Baselines 风格 RL 栈；PyTorch/TensorFlow；Gym/Gymnasium；含 CCXT、Alpaca 等接入信号。 |
| [AI4Finance-Foundation/FinRL-Trading](https://github.com/AI4Finance-Foundation/FinRL-Trading) | `src/backtest`、`src/strategies`、`src/trading`、`src/web` 明确拆分研究、风险、执行和界面。                 | 在 FinRL 思路上进一步产品化，用统一配置把 ML 选股、回测、风控、纸面盘/实盘串起来，强调模块化交易平台而非单一算法仓库。                           | Python；ML + 回测 + Alpaca 实盘；前端侧有 Plotly/Streamlit/Dash 信号。                             |
| [AI4Finance-Foundation/FinRL-Meta](https://github.com/AI4Finance-Foundation/FinRL-Meta)       | `meta/data_processor.py`、`meta/data_processors`、多个 `env_`* 市场环境、`agents/*_models.py`。    | 核心不是策略本身，而是“可复用市场环境库”：先做 DataOps，再把不同资产/任务包装成统一 RL 训练环境，服务 FinRL/FinAI 生态。                  | Python；Gym 环境；Stable-Baselines3、RLlib、ElegantRL；多市场数据处理器。                             |
| [microsoft/qlib](https://github.com/microsoft/qlib)                                           | `qlib/data`、`qlib/model`、`qlib/workflow`、`qlib/strategy`、`qlib/backtest`、`qlib/rl`。      | 典型“数据层 -> 表达式因子层 -> 训练/实验管理 -> 策略回测”架构，既能做传统因子研究，也能接 ML/RL；源码组织明显偏投研平台。                     | Python + Cython；LightGBM、PyTorch、Gym、Redis；强调离线数据缓存和实验工作流。                            |
| [tensortrade-org/tensortrade](https://github.com/tensortrade-org/tensortrade)                 | `tensortrade/env`、`feed`、`oms`、`agents`、`core`。                                          | 用可组合组件定义交易环境：数据 feed、action scheme、reward scheme、order management system 可自由拼装，适合做 RL 交易实验。 | Python；TensorFlow/NumPy/pandas；Gym 风格接口；TA-Lib 与可视化支持。                                |
| [Ceruleanacg/Personae](https://github.com/Ceruleanacg/Personae)                               | `algorithm/RL`、`algorithm/SL`、`base/env`、`base/model`、`playground`。                      | 以论文复现为导向，把强化学习和监督学习策略放在同一模拟市场上比较；工程成熟度一般，但很适合看“模型如何映射到交易任务”。                                | Python；RL + SL 混合；更偏实验仓库而非生产框架。                                                       |
| [edtechre/pybroker](https://github.com/edtechre/pybroker)                                     | `src/pybroker/data.py`、`indicator.py`、`model.py`、`strategy.py`、`portfolio.py`、`vect.py`。 | 把数据、指标、模型、组合和向量化计算集中在单包中，思路是让研究者快速把 ML 信号接到回测框架里；重心在研究效率而不是交易网关。                            | Python；pandas/NumPy/Numba；偏股票/加密研究；可对接 Alpaca。                                        |
| [AminHP/gym-anytrading](https://github.com/AminHP/gym-anytrading)                             | `gym_anytrading/envs` 下抽象出 `TradingEnv`、`ForexEnv`、`StocksEnv`，并附带样例数据集。                 | 目标非常单纯：提供最小可用的 RL 交易环境，把“观察/动作/奖励/持仓更新”规则标准化，方便外部算法直接训练。                                    | Python；OpenAI Gym；pandas/NumPy/Matplotlib。                                            |
| [Yvictor/TradingGym](https://github.com/Yvictor/TradingGym)                                   | `trading_env/envs`、`dataset`、回放动画资源。                                                     | 兼做训练环境和回测环境，强调 tick/OHLC 数据下的交易仿真；整体更像教学/实验型框架，设计上接近 Gym 但把回测可视化也放进来了。                      | Python；pandas/NumPy；RL 训练 + 回测。                                                       |
| [microsoft/RD-Agent](https://github.com/microsoft/RD-Agent)                                   | `rdagent/core`、`components`、`scenarios`、`oai`，另有 `web/src` 前端。                           | 这不是交易引擎，而是研发自动化框架：把 LLM、实验编排、代码生成和场景模板结合，用于自动做因子挖掘、模型优化和数据科学任务。                             | Python；LLM/Agent；OpenAI/LangChain 信号；带 Web UI。                                        |


### 7.2 数据、分析与 Agent 基础设施


| 仓库                                                                                                                  | 核心源码结构                                                                                               | 实现原理                                                                                   | 技术栈补充                                               |
| ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | --------------------------------------------------- |
| [OpenBB-finance/OpenBB](https://github.com/OpenBB-finance/OpenBB)                                                   | `openbb_platform/core`、`providers`、`extensions` 是后端核心，`cli`、`desktop`、`frontend-components` 提供多终端入口。 | 采用“统一数据抽象 + 多 provider 适配 + 多消费端输出”的平台模式，一次接入即可同时供 Python、Workspace、Excel、REST、MCP 使用。 | Python 为主；桌面端含 Tauri/Rust + Web 前端；明显是数据平台/应用平台双栈。  |
| [ranaroussi/yfinance](https://github.com/ranaroussi/yfinance)                                                       | `yfinance/data.py`、`scrapers`、`live.py`、`cache.py`、`multi.py`。                                       | 以 Yahoo Finance 的公开接口和网页数据为基础，封装成 Pythonic API；核心价值在“把零散端点规整成 DataFrame 友好接口”。         | Python；pandas/NumPy；支持历史、基础面、期权和部分实时流。              |
| [pydata/pandas-datareader](https://github.com/pydata/pandas-datareader)                                             | `pandas_datareader/base.py` 加上 `fred.py`、`famafrench.py`、`av/` 等 provider 模块。                        | 采用统一 reader 抽象，把多源互联网数据拉取逻辑映射成一致的 pandas 接口；适合宏观和学术数据接入，不是交易专用引擎。                      | Python；pandas；FRED、Fama/French、Alpha Vantage 等多数据源。 |
| [akfamily/akshare](https://github.com/akfamily/akshare)                                                             | `akshare/` 下按资产和主题分目录，如 `stock`、`bond`、`economic`、`futures`、`crypto`、`data`。                         | 本质是大规模接口适配层：围绕中文财经网站和公开接口做抓取、解析、清洗，再统一暴露为函数式 API。                                      | Python；pandas；中国市场覆盖广；有 API/服务化信号，但核心仍是数据适配。        |
| [TA-Lib/ta-lib-python](https://github.com/TA-Lib/ta-lib-python)                                                     | `talib/_ta_lib.pyx`、`abstract.py`、`stream.py`，配合 `tools/` 生成包装代码。                                    | 通过 Cython 绑定底层 TA-Lib C 库，把技术指标计算暴露给 Python；源码重点在类型桥接和批量生成包装层，而不是策略逻辑。                 | Cython + Python；底层依赖 TA-Lib；兼容 pandas/Polars。       |
| [google/tf-quant-finance](https://github.com/google/tf-quant-finance)                                               | `tf_quant_finance/black_scholes`、`models`、`rates`、`math`、`datetime`。                                 | 偏数值计算库而非交易平台，把定价、利率和随机过程放到 TensorFlow 张量图中，追求自动求导、GPU 加速和批量计算。                         | Python；TensorFlow；项目已 archived，适合作为量化数值方法参考而非新项目底座。 |
| [alvarobartt/investpy](https://github.com/alvarobartt/investpy)                                                     | `investpy/stocks.py`、`funds.py`、`indices.py`、`crypto.py`、`technical.py`、`search.py`。                 | 也是网站数据提取库，但按资产类别拆模块，辅以搜索和技术面接口；原理与 yfinance 类似，重点是网页/API 的规范化封装。                       | Python；面向 Investing.com 数据；偏研究和抓取。                  |
| [mariostoev/finviz](https://github.com/mariostoev/finviz)                                                           | `finviz/main_func.py`、`screener.py`、`portfolio.py`、`helper_functions/`。                              | 针对 Finviz 页面做轻量 API 封装，核心在筛选器参数拼装、HTML 解析和结果格式化，适合快速做美股条件选股。                           | Python；轻量爬取/封装；功能集中在 screener 与 portfolio。          |
| [lit26/finvizfinance](https://github.com/lit26/finvizfinance)                                                       | `finvizfinance/quote.py`、`news.py`、`insider.py`、`screener/`、`group/`、`crypto.py`、`forex.py`。         | 比 `finviz` 更偏“领域对象库”：把个股、新闻、行业、外汇、加密等查询分成独立模块，便于按场景调用。                                 | Python；pandas；非官方 Finviz 数据接口。                      |
| [Barca0412/Introduction-to-Quantitative-Finance](https://github.com/Barca0412/Introduction-to-Quantitative-Finance) | 仓库主体是文档站和资料索引，`data/papers`、`Old/*.md`、`pic/` 为主要内容。                                                 | 不是执行框架，而是把量化、AI、Agent、论文雷达等内容整理成知识库；原理上更接近“研究入口导航页”。                                   | Markdown/VitePress 风格站点资产；适合做资料地图，不适合当代码底座。         |


### 7.3 回测、策略与量化工程框架


| 仓库                                                                                                              | 核心源码结构                                                                                                                   | 实现原理                                                                                  | 技术栈补充                                               |
| --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------- | --------------------------------------------------- |
| [mementum/backtrader](https://github.com/mementum/backtrader)                                                   | `backtrader/cerebro.py`、`broker.py`、`feeds`、`analyzers`、`observers`、`strategies` 核心模块非常清晰。                               | 经典事件驱动回测框架：数据按 bar 推进，`Cerebro` 负责时钟、撮合、资金和指标更新，策略只实现 `next()` 等生命周期方法。               | Python；生态成熟；适合中低频研究与教学。                             |
| [polakowo/vectorbt](https://github.com/polakowo/vectorbt)                                                       | `vectorbt/base`、`data`、`indicators`、`portfolio`、`records`、`returns`。                                                     | 核心思想不是事件循环，而是向量化矩阵回测：一次性在数组上并行评估大量参数和策略组合，再用访问器做结果分析。                                 | Python；pandas/NumPy/Numba；Plotly 可视化强；速度优势明显。       |
| [quarkfin/qf-lib](https://github.com/quarkfin/qf-lib)                                                           | `qf_lib/backtesting`、`brokers`、`data_providers`、`portfolio_construction`、`analysis`，附带 `demo_scripts`。                   | 采用明确的事件驱动设计，模拟日内开闭市、成交和组合更新；相比 backtrader，更强调组合构建、券商和数据提供商抽象。                         | Python；可接 IB/Alpaca；研究与执行抽象分层较完整。                   |
| [QuantConnect/Lean](https://github.com/QuantConnect/Lean)                                                       | `Algorithm`、`Algorithm.Framework`、`Brokerages`、`Engine`、`Common`、`Data`、`Api`，并有 C#/Python 双语言算法样例。                      | 大型生产级事件驱动引擎，核心是 Alpha、Portfolio、Risk、Execution、Universe Selection 等插件点；回测与实盘共用同一套模型层。 | C# 核心 + Python 算法接口；多市场、多券商；工程复杂度高。                 |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader)                             | Rust `crates/{core,data,execution,backtest,live,adapters}` + Python `nautilus_trader/*` 绑定层。                             | 以 Rust 事件总线和执行核心保证性能与一致性，再通过 Python 暴露策略开发接口；典型“高性能内核 + 高生产力脚本层”设计。                   | Rust + PyO3/Python；支持回测与实盘；FIX、IB、Binance 等适配信号明显。  |
| [hudson-and-thames/mlfinlab](https://github.com/hudson-and-thames/mlfinlab)                                     | `mlfinlab/labeling`、`data_structures`、`feature_importance`、`microstructural_features`、`portfolio_optimization` 等按研究主题拆包。 | 实现《Advances in Financial Machine Learning》一类方法论，重点是标签、采样、特征、交叉验证和组合优化工具，而非完整交易引擎。     | Python；NumPy/pandas/Numba/Cython；更适合研究工具链拼装。        |
| [zvtvz/zvt](https://github.com/zvtvz/zvt)                                                                       | `src/zvt/api`、`recorders`、`factors`、`broker`、`trader`、`trading`、`ui`、`ml`。                                               | 把数据录制、因子计算、选股、交易和 API 服务放在同一框架内，目标是搭建一套可扩展的中低频全市场分析/交易系统。                             | Python；SQL/REST/UI 都在仓内；适合二次开发，但架构面较宽。              |
| [je-suis-tm/quant-trading](https://github.com/je-suis-tm/quant-trading)                                         | 以项目目录和单文件策略脚本为主，如配对交易、Monte Carlo、商品/外汇专题项目。                                                                             | 这是策略案例仓库，不是统一框架；价值在于把具体策略假设、数据处理和回测脚本放在一个最短路径里。                                       | Python；统计套利/期权/宏观专题示例丰富；工程规范弱于框架类项目。                |
| [akfamily/akquant](https://github.com/akfamily/akquant)                                                         | Rust `src/engine`、`execution`、`event_manager.rs`、`data` + Python `python/akquant` 包装层。                                   | 设计目标很明确：用 Rust 做时钟、事件和执行内核，用 Python 保留策略与 ML 迭代效率，并通过 zero-copy 思路提升回测吞吐。             | Rust + PyO3/Python；Polars/PyTorch 信号明显；偏下一代高性能研究框架。 |
| [coding-kitties/investing-algorithm-framework](https://github.com/coding-kitties/investing-algorithm-framework) | `investing_algorithm_framework/domain`、`infrastructure`、`services`、`analysis`、`cli`、`app`。                               | 采用 DDD/服务化思路，把策略领域模型、数据下载、执行和应用层拆开，强调从研究到部署的工程可维护性。                                   | Python；CLI + Web 文档站；pandas/Polars/SQLAlchemy/CCXT。 |


### 7.4 资源索引、教程与书籍配套代码


| 仓库                                                                                                                | 核心源码结构                                                                                                                                 | 实现原理                                                             | 技术栈补充                                       |
| ----------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------- |
| [wilsonfreitas/awesome-quant](https://github.com/wilsonfreitas/awesome-quant)                                     | 以 Quarto 站点内容为主，`site/index.qmd`、`projects.csv`、`projects.qmd` 负责生成资源目录。                                                               | 本质是 curated list，不提供统一执行框架；其“源码”价值在于资源分类和站点生成方式，而不是交易逻辑。         | Quarto/Markdown/CSV；适合做导航入口。                |
| [paperswithbacktest/awesome-systematic-trading](https://github.com/paperswithbacktest/awesome-systematic-trading) | 以 README 和 `static/` 资源为主，维护系统化交易相关资源目录。                                                                                               | 通过人工维护方式，把论文、软件、书籍和文章映射到策略开发全流程，适合做检索清单而非代码底座。                   | Markdown；资源索引仓库。                            |
| [firmai/financial-machine-learning](https://github.com/firmai/financial-machine-learning)                         | `generated_wiki/*.md` 为机器学习金融主题知识库，`raw_data/url_list.csv` 保存原始资料索引。                                                                   | 更像金融机器学习维基，核心是主题编排而非运行时系统；适合作为问题域拆解清单。                           | Markdown + CSV；研究导向。                        |
| [georgezouq/awesome-ai-in-finance](https://github.com/georgezouq/awesome-ai-in-finance)                           | 主体是 README 分类列表，`media/` 放站点/徽标资源。                                                                                                     | 把 AI in Finance 项目按 LLM、DL、RL、工具等维度归档，是趋势雷达型仓库，不是源码框架。           | Markdown；面向 AI + Finance 生态梳理。              |
| [thuquant/awesome-quant](https://github.com/thuquant/awesome-quant)                                               | 以 README 中文资源列表为主。                                                                                                                     | 中国量化资源索引，价值在中文社区项目筛选；没有统一业务代码。                                   | Markdown；中文量化导航。                            |
| [wangzhe3224/awesome-systematic-trading](https://github.com/wangzhe3224/awesome-systematic-trading)               | `docs/` + `mkdocs.yml` 做文档站，另有 `src/python`、`src/go`、`src/rust` 示例入口。                                                                  | 仍然是资源索引为主，但相比普通 awesome list，多了多语言最小示例和站点化组织，利于按技术栈检索。           | MkDocs/Markdown；附带 Python/Go/Rust 示例。       |
| [EliteQuant/EliteQuant](https://github.com/EliteQuant/EliteQuant)                                                 | 几乎完全由 README 资源条目组成。                                                                                                                   | 典型在线资源书签仓库，主要价值在主题覆盖和外部链接整理，不在本仓库代码本身。                           | Markdown；资源聚合。                              |
| [stefan-jansen/machine-learning-for-trading](https://github.com/stefan-jansen/machine-learning-for-trading)       | 章节目录非常清楚：`02_market_and_fundamental_data`、`04_alpha_factor_research`、`05_strategy_evaluation`、`07_linear_models`、`08_ml4t_workflow` 等。 | 这是书籍配套代码仓库，把数据获取、特征工程、模型训练、策略评估按章节拆成 Notebook/脚本，适合系统学习 ML 量化流程。 | Jupyter Notebook + Python；覆盖回测、因子、监督学习到 RL。 |
| [cantaro86/Financial-Models-Numerical-Methods](https://github.com/cantaro86/Financial-Models-Numerical-Methods)   | `src/FMNM`、`src/C`、`data/`、`latex/` 组成“模型实现 + 数据 + 讲义”结构。                                                                              | 核心是把随机过程、期权定价、FFT、Monte Carlo、PDE/PIDE 等数值方法做成交互式教材，偏金工数值库。      | Python Notebook + 部分 C；更适合模型学习，不是交易执行系统。    |
| [hugo2046/QuantsPlaybook](https://github.com/hugo2046/QuantsPlaybook)                                             | `SignalMaker`、`hugos_toolkit/BackTestTemplate`、`VectorbtStylePlotting` 等模块围绕研报复现组织。                                                    | 主要做中文金工研报和信号算法复现，强调把研究报告转成可运行 Notebook/脚本；适合因子与择时灵感验证。           | Python/Jupyter；偏 A 股策略研究与可视化。               |


### 7.5 股票与多市场实盘 / 接入


| 仓库                                                                                                                  | 核心源码结构                                                                            | 实现原理                                                                              | 技术栈补充                                          |
| ------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | ---------------------------------------------- |
| [vnpy/vnpy](https://github.com/vnpy/vnpy)                                                                           | `vnpy/trader`、`event`、`rpc`、`chart`、`alpha` 是主骨架。                                 | 典型“事件引擎 + 网关 + 应用模块”架构，交易终端、CTA、组合、AI/Alpha 模块都挂在同一事件总线上；国内量化工程实践色彩很强。            | Python；交易网关生态成熟；新版本明显在补多因子/ML 模块。              |
| [ib-api-reloaded/ib_async](https://github.com/ib-api-reloaded/ib_async)                                             | `ib_async/client.py`、`connection.py`、`wrapper.py`、`ib.py`、`order.py`、`ticker.py`。 | 延续 `ib_insync` 的对象模型，但以更现代的 asyncio 风格封装 TWS/IB Gateway 协议，把请求-响应和流式事件统一起来。       | Python；asyncio；IBKR 市场数据与下单接口。                 |
| [erdewit/ib_insync](https://github.com/erdewit/ib_insync)                                                           | 模块划分与 `ib_async` 基本一致：`client`、`wrapper`、`ib`、`order`、`ticker`。                   | 核心思想是把原始 IB API 回调式接口提升成同步/异步均易用的对象接口，极大降低 IB 二次开发门槛；但该项目已归档。                     | Python；IB API；历史地位高，但新项目更适合参考 `ib_async`。      |
| [alpacahq/alpaca-trade-api-python](https://github.com/alpacahq/alpaca-trade-api-python)                             | `alpaca_trade_api/rest.py`、`rest_async.py`、`stream.py`、`entity*.py`。              | 将 Alpaca 的 REST 和 WebSocket 行情/交易接口做统一客户端封装，主要解决鉴权、对象映射和异步流订阅。                    | Python；REST + WebSocket；官方旧版 SDK 路线。           |
| [alpacahq/alpaca-backtrader-api](https://github.com/alpacahq/alpaca-backtrader-api)                                 | `alpacabroker.py`、`alpacadata.py`、`alpacastore.py` 三件套直接嵌入 backtrader。            | 通过适配 `Broker/Data/Store` 接口，把 backtrader 的研究环境与 Alpaca 纸面盘/实盘接上，实现“回测代码尽量少改即可上线”。 | Python；backtrader 集成层；适合作为桥接适配器参考。             |
| [StockSharp/StockSharp](https://github.com/StockSharp/StockSharp)                                                   | 大型 .NET 解决方案，含 `Algo.*`、`Brokerages/Connectors`、分析脚本、导出、GPU 等多个子项目。               | 企业级组件化平台，交易连接、指标、分析、编译、导出、Designer 工具相互解耦；实盘能力和连接器广度是其核心。                         | C#/.NET；多经纪商、FIX/FAST、跨资产；桌面平台色彩强。             |
| [myhhub/stock](https://github.com/myhhub/stock)                                                                     | `instock/core`、`job`、`trade`、`web`，并配有 `cron`、`docker`、`supervisor`。              | 更像完整股票应用：定时抓取与清洗数据，计算技术指标/筹码分布，执行选股与回测，再通过 Web 与自动交易模块输出。                         | Python；SQLAlchemy/Bokeh/Docker；强调 A 股数据工程与应用化。 |
| [brokermr810/QuantDinger](https://github.com/brokermr810/QuantDinger)                                               | 前后端分离，`backend_api_python/app` + `frontend/`。                                     | 目标是 AI-native 量化平台：后端提供策略/执行 API，前端承载研究与控制界面；从结构看更接近产品化应用，而非单机脚本框架。               | Python 后端 + Web 前端；Docker 化；多 Agent/AI 平台方向。   |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader)                                 | Rust `crates/*` 负责核心交易内核，Python 包负责策略 API 和适配层。                                   | 在实盘语境下，它的关键价值是统一“回测/模拟/实盘”的同构执行模型，尽量保证订单、事件、账户与市场数据语义一致。                          | Rust + Python；低延迟、生产级、多市场。                     |
| [jamesmawm/High-Frequency-Trading-Model-with-IB](https://github.com/jamesmawm/High-Frequency-Trading-Model-with-IB) | `models/base_model.py`、`hft_model_1.py`、`util/order_util.py` 等，结构较轻。              | 这是基于 IB API 的研究原型，围绕高频数据、均值回归/配对交易逻辑快速验证，不是通用交易平台。                                | Python；IB API；适合作为研究样例和接口学习材料。                 |


### 7.6 加密货币与实盘


| 仓库                                                                                  | 核心源码结构                                                                                            | 实现原理                                                                          | 技术栈补充                                                |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------- |
| [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade)                       | `freqtrade/exchange`、`data`、`freqai`、`freqtradebot.py`、`commands`、`configuration`。                | 把交易所适配、策略、回测、超参搜索、风控和机器人进程集成在一套 CLI/Web 控制面内，是“可直接部署”的加密机器人框架。                | Python；CCXT、FastAPI、SQLAlchemy、TA-Lib；支持 ML/FreqAI。  |
| [ccxt/ccxt](https://github.com/ccxt/ccxt)                                           | `js/src` 为统一源头，随后转译/生成到 `python/ccxt`、`cs/`、`go/`、`php/` 等多语言包。                                   | 核心不是策略而是统一交易所 API 抽象：不同交易所的 REST/私有接口被映射成一致的方法名和数据结构。                         | 多语言；代码生成/转译色彩强；是加密生态最常见底层连接层之一。                      |
| [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot)                   | `hummingbot/connector`、`core`、`strategy`、`controllers`、`data_feed`、`client`。                      | 采用连接器 + 策略控制器架构，强调做市、套利和多市场自动化；Cython/异步组件用于提高事件处理效率。                         | Python + Cython；支持 CEX/DEX；高频策略导向明显。                 |
| [jesse-ai/jesse](https://github.com/jesse-ai/jesse)                                 | `jesse/exchanges`、`models`、`controllers`、`indicators`、`candle_pipelines`、`factories`。             | 重点是用较统一的数据模型和配置体验把研究、优化、回测、实盘放在同一工作流里；对用户来说比 Hummingbot 更“框架化”。               | Python；NumPy/Numba/Cython；内含 Web/API 组件信号。           |
| [sammchardy/python-binance](https://github.com/sammchardy/python-binance)           | `binance/client.py`、`async_client.py`、`ws/`、`helpers.py`。                                         | 作用和 `ib_async` 类似，但面向 Binance：封装 REST + WebSocket + 异步接口，适合作为更上层机器人或研究脚本的接入层。 | Python；WebSocket/REST；Binance 专用。                    |
| [Drakkar-Software/OctoBot](https://github.com/Drakkar-Software/OctoBot)             | `octobot/backtesting`、`automation`、`api`、`configuration_manager.py`，以及 `packages/tentacles` 插件体系。 | 倾向平台化和插件化：内核负责编排机器人生命周期，策略、连接器、评估器通过 tentacles 扩展；适合非纯程序员用户。                  | Python；插件生态明显；支持 AI/LLM、回测、TradingView 等集成。          |
| [51bitquant/howtrader](https://github.com/51bitquant/howtrader)                     | `howtrader/api`、`gateway`、`event`、`trader`、`app`、`chart`。                                         | 本质是面向加密场景裁剪过的 vn.py 风格框架，保留事件驱动内核和网关抽象，降低部署复杂度并强化交易所执行。                       | Python；继承 vn.py 思路；支持 Binance/OKX 等与 TradingView 协同。 |
| [51bitquant/bitquant](https://github.com/51bitquant/bitquant)                       | 目录直接按主题拆开，如 `backtest`、`ccxt_study`、`binance_api`、`bybit`、`crawl_exchanges_datas`。                | 这是教程/脚本集，不是统一框架；价值在于把交易所 API、CCXT、回测、数据抓取等主题拆成最小实验案例。                         | Python；CCXT + 交易所脚本；适合入门和素材复用。                       |
| [freqtrade/freqtrade-strategies](https://github.com/freqtrade/freqtrade-strategies) | `user_data/strategies`、`hyperopts`。                                                               | 不提供交易内核，只给 Freqtrade 策略样例；阅读重点是指标、买卖条件和参数搜索，而不是执行基础设施。                        | Python；Freqtrade 策略仓库。                               |
| [iterativv/NostalgiaForInfinity](https://github.com/iterativv/NostalgiaForInfinity) | `user_data/strategies`、`configs`、`legacy`、若干自动化工具脚本。                                              | 属于成熟单策略仓库，围绕 Freqtrade 的配置、黑名单、仓位和条件表达做工程化封装；适合研究“高复杂度策略如何组织配置与版本”。           | Python；强依赖 Freqtrade 生态；不是通用框架。                      |


