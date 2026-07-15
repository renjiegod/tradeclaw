# 从 daily_stock_analysis 移植 7 项功能 —— 实现方案与进度

> 状态时间：**7 项全部完成**（2026-07-13；剩余风险见 §6）。
> 关联计划：`~/.claude/plans/jaunty-scribbling-sparkle.md`（原始批准计划）。

## 1. 背景与目标

`daily_stock_analysis`（下称 **DSA**）在「对外分发、内容产品化、外围数据源、闭环验证」上比 doyoutrade 成熟；doyoutrade 的核心引擎（数据 / 指标 / 选股 / 回测 / LLM 助手）已不弱甚至更强。因此本次**只移植 doyoutrade 缺 / 弱的 7 项能力**，并全部按 doyoutrade 原生架构范式重写（异步 `BaseChannel`、`prompts/render.py`、`NewsProvider` 协议、async SQLAlchemy、`~/.doyoutrade/knowledge/`、skills/scorer），**不照抄** DSA 的同步单例代码。

**最终形态**：每天自动「分析 → 生成研报（可转图片）→ 多渠道推送」，把每次决策落库并事后回测验证效果，并补上视觉 / CSV 导入持仓与 15 套策略提示词素材。

### 用户决策（已确认）

| 维度 | 决策 |
|---|---|
| 通知渠道范围 | 邮件 / 企业微信 / 钉钉 / Telegram / Slack **5 个** + 补齐 Feishu 的 Image/File 发送 |
| md2img 引擎 | **纯 Python**（markdown→HTML→Playwright headless Chromium，无系统级二进制） |
| 决策信号来源 | **策略/回测 Signal + assistant 个股分析对话** 两条 |
| 交付方式 | **一次性大 PR**（内部按功能分 commit） |

## 2. 贯穿约束（CLAUDE.md）

- **错误可见性三条**：任何 `try/except` 至少 ① `logger.warning`+ 以上含异常类型+消息+上下文；② cycle/job 链路发 debug event（`<module>_<reason>`，payload 带 `hint`）；③ 调用方能从 `error_code`/`reason`/`status` 结构化区分失败模式。禁止裸 `except: pass`、silent continue、宽容类型转换。
- **调度唯一通道是 Cron**；定时推送走 cron executor + `_deliver.py`。
- **高风险区**（决策信号写 DB、改 cycle 归因、改模型调用链）必须跑 §测试要求全量集合 + E2E + 真实对话验证。
- assistant 行为若变（新工具 / CLI 子命令），同步改 `prompt_templates/main_agent.j2` 与对应 SKILL.md 的 error_code / payload 示例。

## 3. 进度矩阵

| # | 功能 | 状态 | 单测 |
|---|---|---|---|
| 2 | Markdown→图片（md2img） | ✅ 完成并验证 | 绿（`tests/test_md2img.py`） |
| 1 | 多渠道通知层（5 渠道） | ✅ 完成并验证 | 27 绿（`tests/test_notification_channels.py`） |
| 4 | 多引擎新闻搜索 NewsProvider | ✅ 完成并验证 | 绿（`tests/test_data_news_websearch.py`） |
| 7 | 15 套策略提示词素材 | ✅ 完成（纯新增） | scorer 编译+smoke 通过；15 SKILL.md 解析通过 |
| 3 | 模板化研报生成 + cron executor | ✅ 完成并验证 | 40 绿（`tests/test_report_builder.py` 17 + `tests/test_stock_report_executor.py` 23） |
| 5 | 决策信号落库→回测闭环（高风险） | ✅ 完成并验证 | 24 绿（`tests/test_decision_signals.py`）；高风险集 405 绿；E2E 33 绿 |
| 6 | 图片/CSV 导入持仓（高风险） | ✅ 完成并验证 | 47 绿（`tests/test_model_multimodal.py` 22 + `tests/test_portfolio_import.py` 25） |

> 注：本轮开工时发现上一会话产物部分丢失（`channels/telegram|slack`、`reporting/builder.py`、
> `prompts/report/*.j2`、`tests/test_notification_channels.py`、`tests/test_report_builder.py`
> 缺失且 `channels/__init__.py` 处于 ImportError 破损态），已全部重建并补测。

---

## 4. 各功能实现方案与落点

### 功能 2 —— Markdown → 图片（✅）

**新增**
- `doyoutrade/assistant/rendering/md2img.py`：`async render_markdown_to_image(md, *, max_chars=15000, width=800) -> Md2ImgResult`。管线 markdown→HTML(`markdown` 库)→PNG(Playwright headless Chromium)。两依赖 lazy import；超长 / 依赖缺失 / 渲染失败 / 空字节都返回 `Md2ImgResult(image=None, reason=..., hint=..., detail=...)` 并 `logger.warning`，绝不抛。`MD2IMG_UNAVAILABLE_EVENT` + `REASON_*` 是调用方发 debug event 的稳定 token。
- `doyoutrade/assistant/rendering/__init__.py`：导出。

**修改**
- `doyoutrade/assistant/channels/base.py`：`ImageContent` 加 `data: bytes | None`、`mime_type`、`filename`、`caption`；`FileContent` 加 `data`、`mime_type`。让图片 bytes 能流经 channel，由 channel 内部完成「bytes→上传→id」。
- `doyoutrade/assistant/channels/feishu/channel.py`：补齐 `send()` 的 `ImageContent`/`FileContent` 分支（原 `NotImplementedError`）；新增 `_upload_image_bytes`/`_upload_file_bytes`（`asyncio.to_thread` 包同步 `im.v1.image/file.create`）+ `_send_image`/`_send_file`（图片失败回退 caption 文本，全程 log）。

### 功能 1 —— 多渠道通知层（✅）

范式：doyoutrade `BaseChannel` 异步、每渠道独立实例、DB 注册、bootstrap 硬编码 if/elif 分派。

**新增**
- `doyoutrade/assistant/channels/_push_common.py`：`OutboundPushChannel`（空 start/stop、入站抛未实现）+ `http_post`（httpx.AsyncClient，返回 `(ok,status,detail)` 不抛）+ `ChannelSendError(channel_type, reason, message)`。
- 5 个渠道目录 `channels/{email,wecom,dingtalk,telegram,slack}/{__init__.py,channel.py}`：
  - **email**：`aiosmtplib` 异步 SMTP，支持 markdown→HTML 正文 + 内联图片附件。
  - **wecom**：企业微信群机器人 webhook，markdown / image(base64+md5)。
  - **dingtalk**：群机器人 webhook + HMAC 加签；图片回退 caption。
  - **telegram**：bot `sendMessage` / `sendPhoto`（multipart）。
  - **slack**：incoming webhook 或 bot `chat.postMessage`；图片回退 caption。
- `doyoutrade/capabilities/builtins/channel_{email,wecom,dingtalk,telegram,slack}.json`：`kind:"channel"` + `metadata.channel_type`（缺此项 `POST /assistant/channels` 会被拒）。

**修改**
- `channels/config.py`：加 5 个 `XChannelConfig`。
- `channels/__init__.py`：导出 5 渠道 + 5 config。
- `bootstrap.py`：if/elif 链加 5 个分支，从 `config`(明文) + `secrets`(token/webhook/password) 构造。
- `pyproject.toml`：新增 `[report]` extra（`markdown` / `playwright` / `aiosmtplib`）。

失败模式统一 `ChannelSendError` + `reason`（`not_configured`/`no_recipients`/`no_chat_id`/`http_error`/`api_error`/`smtp_error`/`dependency_missing`）。cron 回推走 `_deliver.py` 通用 `TextContent` 自动生效；图片由研报 executor 主动构造 `ImageContent` 发送。

### 功能 4 —— 多引擎新闻搜索 NewsProvider（✅）

**新增**
- `doyoutrade/data/news_websearch.py`：`NewsWebSearchProvider` 实现 `NewsProvider` 协议。内部 `BaseSearchEngine`（多 key 轮询 + 错误计数 + 结构化失败）+ `TavilySearchEngine`/`BochaSearchEngine`（httpx REST，其余引擎经 `_do_search` 扩展点）。`SearchResult`→`core.models.NewsArticle`（url 去重、publish_time 规范化、闭区间客户端过滤、recent-first + limit）。
- `tests/test_data_news_websearch.py`：17 测试，网络全 mock。

**修改**
- `data/protocols.py`：`PROVIDER_NAME_WEBSEARCH`。
- `config.py` / `config_store.py` / `default_config.yaml`：`data.news.websearch.{tavily_api_keys,bocha_api_keys,timeout_seconds,max_results_per_engine}`，secret 脱敏（GET 只暴露 `*_set` 布尔），env fallback `DOYOUTRADE_TAVILY_API_KEYS`/`DOYOUTRADE_BOCHA_API_KEYS`。
- `api/operations/data_news.py`：`_build_news_provider` 加 `websearch`/`tavily`/`bocha` 分支 + `_SUPPORTED_NEWS_SOURCES`（唯一注册点，不动 `data/factory.py`）。

失败模式：单引擎失败 WARNING + `news_websearch.engine_failed` event（批次继续）；全失败 `websearch_all_engines_failed`→`news_fetch_failed`；未配置 `websearch_not_configured`；空窗口 `news_empty`。**无新第三方依赖**（httpx 已是核心依赖）。

### 功能 7 —— 15 套策略提示词素材（✅）

**新增（纯内容）**
- `.doyoutrade/skills/stockpick-*/SKILL.md` × 15：DSA `strategies/*.yaml` 的 `display_name/category/core_rules/required_tools/aliases/market_regimes/instructions` → doyoutrade 真实 SKILL.md frontmatter(`name`/`description`/`category`) + 正文；DSA 的评分规则（「确认龙头 +10」等）逐条保留；DSA 工具名映射到 doyoutrade CLI（`data run`/`analysis`/`stock screen`/`data sector-heat`/`data news`/`data fund-flow`/`data lhb` 等）。
- `examples/stockpick_scorers/*_scorer.py` × 6：形态/指标类（`ma_golden_cross`/`volume_breakout`/`shrink_pullback`/`bottom_volume`/`one_yang_three_yin`/`box_oscillation`）转 Strategy SDK scorer（真实 SDK：`indicators.sma/ema/volume_ratio/crossed_above`、`patterns.prior_high/prior_low/broke_above`、`Signal.buy/hold`、`IntParameter`/`DecimalParameter`），全部 `sdk validate` 编译+smoke 通过。供 `stock screen --scorer-file` / backtest。

**接入点（未改，可选后续）**：`skill_preload.py` 无需改（按 agent 的 `skill_names` 过滤 `load_skills()`）；要让主 agent 主动知道这 15 个名字，可在 `main_agent.j2` 加一条「选股 / 找票 / 板块龙头 / 情绪周期 → `load_skill stockpick-*`」，或把名字加进相关 agent 的 `skill_names`（DB，高风险区）。

### 功能 3 —— 模板化研报生成 + cron executor（✅）

**新增**
- `doyoutrade/assistant/reporting/builder.py`：doyoutrade 原生输入 `ReportItem`/`ReportRequest`（**不**照搬 DSA `AnalysisResult`）；`build_context`（按 score 降序 None 排最后、分桶 buy/watch/sell/hold 计数、两语 labels、价格格式化、`has_plan`、`summary_only`）；`render_report(request, template=...)` 委托 `prompts.render.render_prompt`。价格为**展示值**，不入执行路径；非法 language / price / item 类型一律 raise。
- `doyoutrade/prompts/report/markdown.j2` + `brief.j2`：section = 摘要条 + 每股（核心结论/趋势/关键指标/作战计划/逻辑/风险/新闻）。本地化 zh/en 内联在 builder。
- `doyoutrade/assistant/cron_executors/stock_report.py`：`KIND="stock_report"`。规则层纯 Python 确定性打分（close vs MA20 ±15、5 日涨跌 ±10、Wilder RSI14 超卖 +10/超买 −10，clamp 0–100；≥65 buy / ≤35 sell / 其余 watch），**不调 LLM**。`bars_provider` 可注入，缺省 lazy `build_trading_data_stack("auto")`。全程 `cron.task.run` span；debug events：`stock_report.symbol_failed`（逐 symbol 继续）/`.gathered`/`.rendered`/`md2img_unavailable`（回退文本）/`.image_delivery_failed`（区分 4 种 reason，回退文本）/`.delivered`/`.journal_failed`（非致命）。落盘知识库 `reports/<YYYY>/<YYYY-MM-DD>-<slug>.md`（sandbox 模式，重复 fire 加 `-2` 后缀）。
- `tests/test_report_builder.py`（17）+ `tests/test_stock_report_executor.py`（23）。

**修改**
- `cron_executors/__init__.py` 导出；`api/server.py` 注册进 `JobTaskRegistry`；`main_agent.j2`「调度与延时任务」加 `stock_report` task kind；`.doyoutrade/skills/doyoutrade-cron/SKILL.md` 加 `stock_report` 条目（minimal payload + error_code + fire-time events）。

---

## 5. 两项高风险功能的落地实现

### 功能 5 —— 决策信号落库 → 回测验证闭环（✅ 高风险）

- **持久化**：`persistence/models.py` 加 `DecisionSignalRecord`（`dsig-` 主键；八态 action / source / status CheckConstraint；价格列按 `trade_fills` 惯例存十进制字符串；`dedupe_key` UNIQUE = 第一个非空 `(run_id|trace_id|session_id)` + symbol+action+horizon；对 `cycle_runs.run_id` 软引用+索引）与 `DecisionSignalOutcomeRecord`（真实 FK → `decision_signals.id` CASCADE；`(signal_id, horizon, engine_version)` UNIQUE）。`decision_signal_feedback` 本期未建（可选项）。
- **migration**：`alembic/versions/20260713_01_decision_signals.py`（`down_revision="20260704_01"`）。`make migrate` 在本机失败是因为配置的 Postgres `localhost:5432` 未运行（环境问题）；已用同一入口 `runtime_state.run_migrations` 对 SQLite 从空库升到 head 验证，且真实 server 启动（SQLite）migration 自动生效。
- **repository**：`SqlAlchemyDecisionSignalRepository`：`create_if_absent`（幂等 + IntegrityError 并发兜底）、`list_signals`、`get_signal`、`update_status`、`expire_due_signals`（懒过期）、`upsert_outcome`（幂等）、`list_outcomes`。
- **评估器**：`doyoutrade/backtest/decision_signal_eval.py` 纯函数：`infer_direction_expected`/`evaluate_decision_signal`/`_evaluate_targets`/`parse_horizon_days`；目标价/止损先触达判 hit/miss，无目标价按方向±1% 阈值判 neutral；数据不足返回结构化 `data_insufficient`。
- **信号来源①（回测）**：`platform/service.py::_persist_decision_signals_from_run` 挂在 `_persist_backtest_summary` 成功路径：从 run 的结构化 fills 抽信号（sell + exit_reason → take_profit/stop_loss）落库并即时评估。span `backtest.decision_signals.persist`；events `decision_signal.persisted`/`.outcome_evaluated`/`.outcome_failed`/`.persist_failed`；失败不阻断回测收尾。live 策略 runner 直挂本期未接（表/枚举已预留）。
- **信号来源②（assistant）**：`tools/decision_signal.py::RecordDecisionSignalTool`（`record_decision_signal`，三段契约校验 + `operation_record_decision_signal.*` events + deduped 语义），已注册进 `build_default_tool_registry`（bootstrap → AssistantService 穿透 `decision_signal_repository`）。
- **API/CLI**：`GET /decision-signals`、`GET /decision-signals/{id}`、`POST /decision-signals/{id}/evaluate`（数据不足 200 skipped）；`doyoutrade-cli decision-signal list/get/evaluate` + command_contracts + `.doyoutrade/skills/doyoutrade-decision-signal/SKILL.md`；`main_agent.j2` 同步（in-process 清单、CLI 域、速查表、`dsig-` 前缀入硬性约束）。

### 功能 6 —— 图片 / CSV 智能导入持仓（✅ 高风险）

- **模型层多模态**：`models/base.py` 加 frozen `ImagePart(data, mime_type)`（非空、≤8MB、mime 白名单，违反 raise）+ `ModelRequest.image_parts`（默认 None 全向后兼容）；`_common.py::build_*_messages` 支持 OpenAI `image_url` data-URL 块与 Anthropic `image` base64 块；**记录脱敏**：共享 `redact_image_blocks` 在两个 provider 的 body 构建器与 `recording.py` 全部 5 处 request_payload 赋值点套防护，base64 永不落 `model_invocations`。
- **视觉抽取**：`doyoutrade/portfolio_import/image_extractor.py`：中文 `EXTRACT_PROMPT` + 魔数校验 + 严格 JSON（失败截取首个 `[...]` 兜底，再失败 `extract_parse_failed` 透出原文前 500 字，**未引入 json_repair**）+ `search_instrument_universe` 归一化（解析不出保留原名标 `symbol_unresolved`）。error_code：`image_empty/image_too_large/image_mime_mismatch/model_error/extract_parse_failed/extract_empty`。
- **CSV 交割单**：`portfolio_import/csv_import.py` 复用 `knowledge/attribution.py` 解析件，按月写 `trades/<broker>/<YYYY-MM>.csv`（sandbox + dedupe hash 去重追加 + 刷新 `_index.md` + `read_trade_attribution` 冒烟）。
- **入口**：assistant 工具 `import_positions_from_image`（lazy `model_adapter_factory` 解析默认 route，失败结构化 `model_adapter_unavailable`）与 `import_trades_csv`，均已注册 registry；CLI `portfolio import-csv`（本地直调 envelope，已真实冒烟成功）与 `portfolio import-image`（恒 `not_available_via_cli` + hint 指向工具）；skill `.doyoutrade/skills/doyoutrade-portfolio-import/SKILL.md`。

---

## 6. 依赖 / 未做项 / 风险

**新增依赖**（均归入 `[report]` extra，lazy import，核心运行时不受影响）
- `markdown`、`playwright`（+ `playwright install chromium`）、`aiosmtplib`。
- 功能 4 **无**新依赖（httpx REST）。功能 6 视觉**未引入** `json_repair`（严格 JSON + `[...]` 截取兜底 + 结构化 `extract_parse_failed` 已足够，避免静默修出错误数据）。

**未做项 / 剩余风险**
- **真实 LLM 对话验证未完成**：本机 `~/.doyoutrade` 配置的 Postgres（`localhost:5432`）未运行，且无已配置的模型路由 / provider 凭据。已用临时 SQLite HOME 起真实 server 完成集成冒烟（bootstrap 新装配 + migration 自动升级 + `GET /assistant/agents`、`GET /decision-signals` 200 + CLI `decision-signal list` / `portfolio import-csv` 真实 envelope 成功），但 `record_decision_signal` / `import_positions_from_image` 的真实模型对话链路需在有模型路由的环境按 CLAUDE.md §Assistant 真实对话验证补跑。
- `make migrate` 对本机 Postgres 未验证（服务未运行）；SQLite 路径已验证到 head `20260713_01`。
- 决策信号**前端 UI / frontend types 未做**：debug event 与 CLI/API 可见，前端调试页无专用渲染。
- live 策略 runner 直挂 decision_signals 未接（仅回测收尾抽取 + assistant 工具两条来源）；`decision_signal_feedback` 表未建；reassess 仍 preview-only。
- Slack 图片仅 caption 回退（无免公网 URL 的字节上传面）；钉钉同。
- `stock_report` 缺省 bars provider（`build_trading_data_stack("auto")`）未在真实行情环境验证（测试注入 fake；真实失败会以 `stock_report.symbol_failed` 可见）。
- RSS 情报聚合、通知静默时段 / 路由分级 / 去重冷却（`dispatch_policy`）、非选定的 9 个通知渠道：按原计划不在本期范围。

**风险**
- 一次性大 PR 与仓库「一个 PR 一个语义改动」有张力，已按用户要求；建议 commit 粒度清晰以便 review。

## 7. 测试与验证清单（全部真实跑过）

```
uv run python -m unittest tests.test_md2img tests.test_data_news_websearch      # 23 绿
uv run python -m unittest tests.test_notification_channels                      # 27 绿
uv run python -m unittest tests.test_report_builder tests.test_stock_report_executor  # 40 绿
uv run python -m unittest tests.test_decision_signals                           # 24 绿
uv run python -m unittest tests.test_model_multimodal tests.test_portfolio_import tests.test_model_invocations  # 89 绿
uv run python -m unittest tests.test_assistant_prompt_templates tests.test_assistant_service_slash \
  tests.test_assistant_skill_preload tests.test_decision_signals tests.test_portfolio_import \
  tests.test_model_multimodal                                                   # 95 绿（工具注册/prompt 集成后）
# 高风险全量：
uv run python -m unittest tests.test_persistence tests.test_platform_service \
  tests.test_api_app tests.test_worker_signal_path tests.test_worker_code_root_pin \
  tests.test_observability tests.test_model_invocations tests.test_debug_overrides  # 405 绿
make test-e2e                                                                   # 33 绿
# migration：SQLite 空库 → head 20260713_01 验证通过；make migrate 需 Postgres 运行（见 §6）
# server 集成冒烟：临时 SQLite HOME 起 uv run doyoutrade，
#   GET /assistant/agents、GET /decision-signals 200；
#   doyoutrade-cli decision-signal list / portfolio import-csv 真实 envelope 成功
```
