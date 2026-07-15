# QMT Proxy — HTTP / WebSocket API 参考

本文档描述本仓库 **REST API** 与 **WebSocket** 契约，供独立维护的 **SDK 仓库** 对齐实现与回归测试。本文 **不包含 gRPC**。

**权威来源（代码）**：`app/routers/data.py`、`app/routers/trading.py`、`app/routers/health.py`、`app/routers/websocket.py`、`app/models/data_models.py`、`app/models/trading_models.py`、`app/services/data_service.py`、`app/services/trading_service.py`、`app/dependencies.py`、`app/utils/helpers.py`。

**变更约定**：新增或修改路由时，应同步更新本文档；字段级 JSON Schema 以运行实例的 `/openapi.json` 及 Pydantic 模型为准。

**合约代码习惯写法**（与 `app/utils/helpers.validate_stock_code` 一致）：A 股常见 `000001.SZ`、`600000.SH`（6 位数字 + `.` + `SH`/`SZ`/`BJ`）；也支持港股等后缀如 `.HK`、`.US`。

---

## 1. 基础约定

### 1.1 Base URL

服务监听地址见 `config.yml` / 环境变量中的 `AppConfig`（`host`、`port`，常见为 `http://<host>:8000`）。下文路径均为相对路径。

### 1.2 内容类型

- REST：`application/json`（`GET` 无 body 除外）。
- WebSocket：客户端→服务端、服务端→客户端均使用 **文本帧**，负载为 **UTF-8 JSON**（可 `JSON.parse` / `json.loads`）。

### 1.3 认证（REST）

| 方式 | 说明 |
|------|------|
| `Authorization: Bearer <api_key>` | **当前实现使用的唯一方式**（`HTTPBearer`，见 `app/dependencies.get_api_key`） |

`<api_key>` 必须在配置项 `security.api_keys` 列表中，否则 `verify_api_key` 抛出认证异常。

> **说明**：`config.yml` 中 `security.api_key_header`（如 `X-API-Key`）**未被** `verify_api_key` 读取；SDK 以 **`Authorization: Bearer`** 为准。

**无需 API Key**：`GET /`、`GET /info`、`GET /health/*`、`GET /ws/test`、`GET /ui/*`。

**WebSocket** ` /ws/quote/{subscription_id}`：**当前不校验** API Key，仅校验订阅是否存在。

### 1.4 响应体两种形态

**A. 包装格式**（`format_response`，见 `app/utils/helpers.py`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | boolean | 是否成功 |
| `message` | string | 提示信息 |
| `code` | number | 业务/约定码，常与 HTTP 状态一致 |
| `timestamp` | string | ISO 8601 风格本地时间字符串 |
| `data` | any | 可选；成功载荷或省略 |

**B. 裸模型**（FastAPI 直接序列化 `response_model` 或路由返回的 dict/list）

响应体**没有**顶层 `success`/`code`/`data`，即为 Pydantic 模型或路由自定义对象的 JSON。

**SDK 建议**：若 JSON 顶层含 `success` 且为 boolean，按 **A** 解析；否则按接口固定契约解析为 **B**。

### 1.5 错误与 HTTP 状态码

- 路由内 `HTTPException`：经 `http_exception_handler` 转为 JSON，`message` 多为 `str(exc.detail)`（`detail` 为 dict 时可能被整体字符串化）。
- `DataServiceException` / `TradingServiceException` 经 `handle_xtquant_exception` 时，`detail` 常含 `message`、`error_code`。
- 未转换的 `XTQuantException`（含 `AuthenticationException`）可能被 `xtquant_exception_handler` 统一为 **HTTP 500** + 包装格式（见 `app/main.py`）。客户端应对 **4xx/5xx** 均尝试解析 JSON 的 `message`。

### 1.6 通用枚举（数据）

**`PeriodType`**（字符串枚举，用于 JSON）：

| 值 | 含义 |
|----|------|
| `tick` | 分笔 |
| `1m` `5m` `15m` `30m` | 分钟线 |
| `1h` | 小时 |
| `1d` | 日线 |
| `1w` | 周线 |
| `1mon` | 月线 |
| `1q` | 季线 |
| `1hy` | 半年线 |
| `1y` | 年线 |

**`MarketType`**（模型内定义，部分场景引用）：`SH`、`SZ`、`BJ`、`FUTURES`、`OPTION`。

**订阅复权 `adjust_type`**（`SubscriptionRequest` 校验）：`none` | `front` | `back` | `front_ratio` | `back_ratio`。

**`SubscriptionType`**：`quote`（指定代码订阅）、`whole_quote`（全市场行情推送类订阅，受 `xtquant.data.whole_quote_enabled` 等配置约束）。

**`DownloadTaskStatus`**：`pending` | `running` | `completed` | `failed`。

---

## 2. 系统与元数据

### 2.1 `GET /`

| 项目 | 说明 |
|------|------|
| **功能** | 服务欢迎页元数据 |
| **认证** | 否 |
| **响应** | 包装格式 |

**`data` 字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `app_name` | string | 应用名（配置） |
| `app_version` | string | 版本 |
| `xtquant_mode` | string | `mock` / `dev` / `prod` |
| `description` | string | 描述文案 |
| `docs_url` | string | Swagger 路径，如 `/docs` |
| `redoc_url` | string | ReDoc 路径，如 `/redoc` |

### 2.2 `GET /info`

| 项目 | 说明 |
|------|------|
| **功能** | 返回进程/应用配置摘要 |
| **认证** | 否 |
| **响应** | 包装格式 |

**`data` 字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 应用名 |
| `version` | string | 版本 |
| `debug` | boolean | 调试模式 |
| `host` | string | 绑定地址 |
| `port` | number | 监听端口 |
| `log_level` | string | 日志级别 |
| `xtquant_mode` | string | 见上 |
| `allow_real_trading` | boolean | 是否允许真实交易（配置） |

---

## 3. 健康检查

前缀 `/health`，均为 **包装格式**，**无需认证**。

### 3.1 `GET /health/`

| 项目 | 说明 |
|------|------|
| **功能** | 综合健康信息 |
| **`data`** | `status`（如 `"healthy"`）、`app_name`、`app_version`、`xtquant_mode`、`timestamp` |

> 实现中 `timestamp` 为**固定占位字符串**，勿依赖其为实时时间。

### 3.2 `GET /health/ready`

| 项目 | 说明 |
|------|------|
| **功能** | Kubernetes 风格就绪探针 |
| **`data`** | `status`: `"ready"` |

### 3.3 `GET /health/live`

| 项目 | 说明 |
|------|------|
| **功能** | 存活探针 |
| **`data`** | `status`: `"alive"` |

---

## 4. WebSocket — 行情推送

### 4.1 URL

```
ws://<host>:<port>/ws/quote/{subscription_id}
wss://<host>:<port>/ws/quote/{subscription_id}
```

| 参数 | 位置 | 说明 |
|------|------|------|
| `subscription_id` | path | 由 `POST /api/v1/data/subscription` 返回的 `subscription_id` |

### 4.2 连接与业务逻辑

1. 服务端 `accept` 后查询 `SubscriptionManager`；**无此订阅**时发送 `type: error` 的 JSON，然后 **close code 1008**。
2. **有订阅**时先发 **`connected`**。
3. 之后对 `stream_quotes` 异步迭代：每条推送一条 **`quote`**，其中 `data` 为 xtdata 回调的 **原始 dict**（外层键多为合约代码，内层结构随周期/品种变化）。
4. 并行任务接收客户端文本消息：解析 JSON，若 `type === "ping"` 则回复 **`pong`**。

### 4.3 服务端 → 客户端消息

| `type` | 说明 | 字段 |
|--------|------|------|
| `connected` | 通道就绪 | `subscription_id` (string), `message` (string), `timestamp` (string, ISO) |
| `quote` | 行情 | `data` (object, xtquant 结构), `timestamp` (string) |
| `pong` | 心跳应答 | `timestamp` (string) |
| `error` | 异常 | `message` (string) |

发送或内部异常时也可能发 `error` 并 **close 1011**。

### 4.4 客户端 → 服务端

| 内容 | 说明 |
|------|------|
| 文本帧：`{"type":"ping"}` | 心跳；服务端返回 `pong` |

非法 JSON 可能导致接收协程记录错误日志，行为以服务端实现为准。

### 4.5 `GET /ws/test`

返回简单 HTML 页，用于浏览器内测 WebSocket（**无认证**）。

---

## 5. 数据服务 API（`/api/v1/data`）

**认证**：本节所有接口均需 `Authorization: Bearer <api_key>`（除非另行说明）。

---

### 5.1 `POST /market`

| 项目 | 说明 |
|------|------|
| **功能** | 按代码、周期、时间区间拉取 K 线/行情数据（经 `DataService.get_market_data`） |
| **请求体** | `MarketDataRequest` |
| **响应** | **裸**：`MarketDataResponse[]` |

**`MarketDataRequest`**（继承 `DataRequest`）：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `stock_codes` | string[] | 是 | — | 非空 |
| `start_date` | string | 否 | `""` | `""` 或 8/14 位数字：`YYYYMMDD` / `YYYYMMDDHHMMSS` |
| `end_date` | string | 否 | `""` | 同上 |
| `period` | `PeriodType` | 否 | `1d` | 数据周期 |
| `fields` | string[] | 否 | null | 请求字段子集 |
| `adjust_type` | string | 否 | `"none"` | 复权类型（与 xtdata 约定一致） |
| `fill_data` | boolean | 否 | true | 是否填充缺失 |
| `disable_download` | boolean | 否 | false | 是否禁止触发下载 |

**`MarketDataResponse`**（数组元素）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `stock_code` | string | 合约代码 |
| `data` | object[] | 每条记录为字段名→值的字典 |
| `fields` | string[] | 列名/字段顺序说明 |
| `period` | string | 周期 |
| `start_date` | string | 请求起始 |
| `end_date` | string | 请求结束 |

---

### 5.2 `POST /financial`

| 项目 | 说明 |
|------|------|
| **功能** | 查询财务表数据 |
| **请求体** | `FinancialDataRequest` |
| **响应** | **裸**：`FinancialDataResponse[]` |

**请求字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `stock_codes` | string[] | 是 | 股票代码列表 |
| `table_list` | string[] | 是 | 财务表名列表（xtdata 表名） |
| `start_date` | string | 否 | 开始日期 |
| `end_date` | string | 否 | 结束日期 |

**`FinancialDataResponse`**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `stock_code` | string | 代码 |
| `table_name` | string | 表名 |
| `data` | object[] | 行数据 |
| `columns` | string[] | 列名 |

---

### 5.3 `GET /sectors`

| 项目 | 说明 |
|------|------|
| **功能** | 板块列表及成分代码 |
| **响应** | **裸**：`SectorResponse[]` |

**`SectorResponse`**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `sector_name` | string | 板块名称 |
| `stock_list` | string[] | 成分股代码 |
| `sector_type` | string \| null | 板块类型 |

---

### 5.4 `POST /sector`

| 项目 | 说明 |
|------|------|
| **功能** | 按名称查找板块；在 `get_sector_list` 结果中匹配 `sector_name` |
| **请求体** | `SectorRequest` |
| **响应** | **包装格式** |

**请求字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sector_name` | string | 是 | 板块名称 |
| `sector_type` | string | 否 | 板块类型（当前路由逻辑主要按名称匹配） |

**`data`**：若找到板块，为 `SectorResponse` 的 dict（`sector.dict()`）；否则 `{"sector_name": "<请求名>", "stock_list": []}`。

---

### 5.5 `POST /index-weight`

| 项目 | 说明 |
|------|------|
| **功能** | 查询指数成分权重 |
| **请求体** | `IndexWeightRequest` |
| **响应** | **裸**：`IndexWeightResponse` |

**请求字段**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `index_code` | string | 是 | 指数代码 |
| `date` | string | 否 | 日期 |

**`IndexWeightResponse`**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `index_code` | string | 指数代码 |
| `date` | string | 权重日期 |
| `weights` | object[] | 权重明细（字典结构随数据源） |

---

### 5.6 `GET /trading-calendar/{year}`

| 项目 | 说明 |
|------|------|
| **功能** | 指定年份交易日与假日 |
| **路径参数** | `year`：整数年份 |
| **响应** | **裸**：`TradingCalendarResponse` |

**`TradingCalendarResponse`**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `trading_dates` | string[] | 交易日 |
| `holidays` | string[] | 假日 |
| `year` | number | 年份 |

---

### 5.7 `GET /instrument/{stock_code}`

| 项目 | 说明 |
|------|------|
| **功能** | 单合约基础信息（xtquant 字段映射） |
| **路径参数** | `stock_code` |
| **响应** | **裸**：`InstrumentInfo` |

**`InstrumentInfo` 主要字段**（均为可选，视数据源而定）：

| 字段 | 说明 |
|------|------|
| `ExchangeID`, `InstrumentID`, `InstrumentName` | 市场、代码、名称 |
| `ProductID`, `ProductName`, `ProductType` | 期货品种等 |
| `ExchangeCode`, `UniCode` | 交易所/统一代码 |
| `CreateDate`, `OpenDate`, `ExpireDate` | 日期类 |
| `PreClose`, `SettlementPrice`, `UpStopPrice`, `DownStopPrice` | 价格 |
| `FloatVolume`, `TotalVolume` | 股本 |
| `LongMarginRatio`, `ShortMarginRatio`, `PriceTick`, `VolumeMultiple`, `MainContract`, `LastVolume` | 期货相关 |
| `InstrumentStatus`, `IsTrading`, `IsRecent` | 状态 |
| `instrument_code`, `instrument_name`, `market_type`, `instrument_type`, `list_date`, `delist_date` | 兼容字段 |

---

### 5.8 `GET /etf/{etf_code}`

| 项目 | 说明 |
|------|------|
| **功能** | **占位实现**：未调用 `DataService`，返回写死的演示型 `ETFInfoResponse` |
| **路径参数** | `etf_code` |
| **响应** | **裸**：`ETFInfoResponse` |

**`ETFInfoResponse`**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `etf_code` | string | 请求代码 |
| `etf_name` | string | 当前为拼接占位 |
| `underlying_asset` | string | 占位 |
| `creation_unit` | number | 创设单位 |
| `redemption_unit` | number | 赎回单位 |

---

### 5.9 `GET /instrument-type/{stock_code}`

| 项目 | 说明 |
|------|------|
| **功能** | 合约品种分类（股/指/基/债/期/权等布尔标记） |
| **响应** | 包装格式，`data` 为 `InstrumentTypeInfo` |

**`InstrumentTypeInfo`**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `stock_code` | string | 代码 |
| `index` | boolean | 是否指数 |
| `stock` | boolean | 是否股票 |
| `fund` | boolean | 是否基金 |
| `etf` | boolean | 是否 ETF |
| `bond` | boolean | 是否债券 |
| `option` | boolean | 是否期权 |
| `futures` | boolean | 是否期货 |

---

### 5.10 `GET /holidays`

| 项目 | 说明 |
|------|------|
| **功能** | 节假日列表 |
| **响应** | 包装格式，`data` 为 `HolidayInfo` |

**`HolidayInfo`**：`holidays` string[]，`YYYYMMDD`。

---

### 5.11 `GET /convertible-bonds`

| 项目 | 说明 |
|------|------|
| **功能** | 可转债列表 |
| **响应** | 包装格式，`data` 为 `ConvertibleBondInfo[]` |

**`ConvertibleBondInfo` 主要字段**：

| 字段 | 说明 |
|------|------|
| `bond_code` | 可转债代码（必填） |
| `bond_name`, `stock_code`, `stock_name` | 名称与正股 |
| `conversion_price`, `conversion_value`, `conversion_premium_rate` | 转股相关 |
| `current_price`, `par_value` | 价格 |
| `list_date`, `maturity_date`, `conversion_begin_date`, `conversion_end_date` | 日期 |
| `raw_data` | object | xtdata 原始扩展字段 |

---

### 5.12 `GET /ipo-info`

| 项目 | 说明 |
|------|------|
| **功能** | 新股申购信息列表 |
| **响应** | 包装格式，`data` 为 `IpoInfo[]` |

**`IpoInfo` 主要字段**：

| 字段 | 说明 |
|------|------|
| `security_code` | 证券代码（必填） |
| `code_name`, `market` | 简称、市场 |
| `act_issue_qty`, `online_issue_qty`, `online_sub_code`, `online_sub_max_qty`, `publish_price` | 发行与申购 |
| `is_profit`, `industry_pe`, `after_pe` | 盈利与估值 |
| `subscribe_date`, `lottery_date`, `list_date` | 日期 |
| `raw_data` | object | 原始扩展 |

---

### 5.13 `GET /period-list`

| 项目 | 说明 |
|------|------|
| **功能** | 当前环境可用的 K 线周期列表 |
| **响应** | 包装格式，`data` 为 `PeriodListResponse`（`periods: string[]`） |

---

### 5.14 `GET /data-dir`

| 项目 | 说明 |
|------|------|
| **功能** | 本地数据目录路径 |
| **响应** | 包装格式，`data` 为 `DataDirResponse`（`data_dir: string`） |

---

### 5.15 `POST /local-data`

| 项目 | 说明 |
|------|------|
| **功能** | 读取本地已下载行情（`get_local_data`） |
| **响应** | 包装格式，`data` 为 `MarketDataResponse[]` |

**`LocalDataRequest`**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `stock_codes` | string[] | 是 | — | 代码列表 |
| `start_time` | string | 否 | `""` | `YYYYMMDD` |
| `end_time` | string | 否 | `""` | `YYYYMMDD` |
| `period` | string | 否 | `1d` | K 线周期字符串 |
| `fields` | string[] | 否 | null | 字段子集 |
| `adjust_type` | string | 否 | `none` | 复权 |

---

### 5.16 `POST /full-tick`

| 项目 | 说明 |
|------|------|
| **功能** | 最新 tick/全推快照（`get_full_tick`） |
| **响应** | 包装格式，`data` 为 **对象**：键为股票代码，值为 **`TickData[]`**（实现中每代码常为单元素列表） |

**`FullTickRequest`**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `stock_codes` | string[] | 是 | 代码列表 |
| `start_time` | string | 否 | 开始（透传/预留） |
| `end_time` | string | 否 | 结束（透传/预留） |

**`TickData` 字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `time` | string | 时间戳 |
| `last_price` | number | 最新价 |
| `open`, `high`, `low`, `last_close` | number | OHLC 相关 |
| `amount`, `volume`, `pvolume` | number | 额、量 |
| `stock_status`, `open_int`, `last_settlement_price` | number | 状态、持仓、结算 |
| `ask_price`, `bid_price` | number[] | 卖/买价档位 |
| `ask_vol`, `bid_vol` | number[] | 卖/买量档位 |
| `transaction_num` | number | 成交笔数 |

---

### 5.17 `POST /divid-factors`

| 项目 | 说明 |
|------|------|
| **功能** | 除权除息因子（仅使用 body 中 `stock_code`） |
| **请求体** | `DividFactorsRequest`：`stock_code` string |
| **响应** | 包装格式，`data` 为 `DividendFactor[]` |

**`DividendFactor`**：

| 字段 | 说明 |
|------|------|
| `time` | 除权日 |
| `interest` | 每股股利（税前，元） |
| `stock_bonus`, `stock_gift` | 红股、转增 |
| `allot_num`, `allot_price` | 配股数、配股价 |
| `gugai` | 是否股改 |
| `dr` | 除权系数 |

---

### 5.18 `POST /full-kline`

| 项目 | 说明 |
|------|------|
| **功能** | 带复权等的完整 K 线（`get_full_kline`） |
| **响应** | 包装格式，`data` 为 `MarketDataResponse[]` |

**`FullKlineRequest`**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `stock_codes` | string[] | 是 | — | 代码 |
| `start_time` | string | 否 | `""` | `YYYYMMDD` |
| `end_time` | string | 否 | `""` | `YYYYMMDD` |
| `period` | string | 否 | `1d` | 周期 |
| `fields` | string[] | 否 | null | 字段 |
| `adjust_type` | string | 否 | `none` | 复权 |

---

## 6. 数据下载 API（`/api/v1/data/download/*`）

均为 **包装格式**，`data` 为 **`DownloadResponse`**（或与之一致的 dict）。下载接口在真实模式下多调用 xtdata **同步下载**，返回的 `task_id`/`status`/`message` 表示本次调用结果摘要。

**`DownloadResponse`**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_id` | string | 任务或逻辑 ID |
| `status` | `DownloadTaskStatus` | 状态 |
| `progress` | number | 0–100 |
| `total` | number | 总数（批量场景） |
| `finished` | number | 已完成数 |
| `message` | string | 说明或错误信息 |
| `current_stock` | string \| null | 当前处理代码 |

### 6.1 `POST /download/history-data`

| 项目 | 说明 |
|------|------|
| **功能** | 单标的下载历史行情到本地 |
| **请求体** | `DownloadHistoryDataRequest` |

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `stock_code` | string | 必填 | 股票代码 |
| `period` | string | `1d` | 周期 |
| `start_time` | string | `""` | 起始 |
| `end_time` | string | `""` | 结束 |
| `incrementally` | boolean | false | 是否增量 |

### 6.2 `POST /download/history-data-batch`

| 项目 | 说明 |
|------|------|
| **功能** | 批量下载历史行情 |
| **请求体** | `DownloadHistoryDataBatchRequest`：`stock_list`, `period`, `start_time`, `end_time` |

### 6.3 `POST /download/financial-data`

| 项目 | 说明 |
|------|------|
| **功能** | 下载财务数据 |
| **请求体** | `DownloadFinancialDataRequest`：`stock_list`, `table_list`, `start_date`, `end_date` |

### 6.4 `POST /download/financial-data-batch`

| 项目 | 说明 |
|------|------|
| **功能** | 批量下载财务数据 |
| **请求体** | `DownloadFinancialDataBatchRequest` |

| 字段 | 说明 |
|------|------|
| `stock_list`, `table_list` | 股票与表 |
| `start_date`, `end_date` | 日期范围 |
| `callback_func` | 可选字符串；服务端语义以 `DataService` 为准 |

### 6.5 `POST /download/sector-data`

无 body。下载板块数据。

### 6.6 `POST /download/index-weight`

**请求体** `DownloadIndexWeightRequest`：`index_code` 可选，空表示全量指数权重下载（见服务实现）。

### 6.7 `POST /download/cb-data`

无 body。下载可转债数据。

### 6.8 `POST /download/etf-info`

无 body。下载 ETF 基础信息。

### 6.9 `POST /download/holiday-data`

无 body。下载节假日数据。

### 6.10 `POST /download/history-contracts`

**请求体** `DownloadHistoryContractsRequest`：`market` 可选，市场过滤。

---

## 7. 板块管理 API（`/api/v1/data/sector/*`）

均为 **包装格式**。成功时 `message` 为中文说明；`data` 可能为 `null` 或含 `created_name` 等。

### 7.1 `POST /sector/create-folder`

| 项目 | 说明 |
|------|------|
| **功能** | 创建板块文件夹 |
| **查询参数** | `parent_node`（默认 `""`）, `folder_name`（默认 `""`） |
| **`data`** | `{"created_name": string}` |

### 7.2 `POST /sector/create`

| 项目 | 说明 |
|------|------|
| **功能** | 创建板块 |
| **JSON Body** | `parent_node`（默认 `""`）, `sector_name`, `overwrite`（默认 true） |
| **`data`** | `{"created_name": string}` |

### 7.3 `POST /sector/add-stocks`

**Body**：`sector_name`, `stock_list`（string[]）。向板块追加成分。

### 7.4 `POST /sector/remove-stocks`

**Body**：`sector_name`, `stock_list`。从板块删除成分。

### 7.5 `POST /sector/remove`

**查询参数**：`sector_name`（**注意**：POST 仍使用 query）。删除整个板块。

### 7.6 `POST /sector/reset`

**Body**：`sector_name`, `stock_list`。重置板块成分为新列表。

---

## 8. Level2 API（`/api/v1/data/l2/*`）

均为 **包装格式**，`data` 为服务层返回结构。

### 8.1 `POST /l2/quote`

**功能**：Level2 十档快照。  
**请求体** `L2QuoteRequest`：`stock_codes`（必填）, `start_time`, `end_time`（可选，当前路由传入模型但服务侧主要使用 `stock_codes`）。  
**`data`**：`Dict[string, L2QuoteData]`（代码 → 快照对象）。

**`L2QuoteData` 主要字段**：`time`, `last_price`, `open`, `high`, `low`, `amount`, `volume`, `pvolume`, `open_int`, `stock_status`, `transaction_num`, `last_close`, `last_settlement_price`, `settlement_price`, `pe`, `ask_price`/`bid_price`（10 档）, `ask_vol`/`bid_vol`。

### 8.2 `POST /l2/order`

**功能**：逐笔委托。  
**`data`**：`Dict[string, L2OrderData[]]`。

**`L2OrderData`**：`time`, `price`, `volume`, `entrust_no`, `entrust_type`, `entrust_direction`。

### 8.3 `POST /l2/transaction`

**功能**：逐笔成交。  
**`data`**：`Dict[string, L2TransactionData[]]`。

**`L2TransactionData`**：`time`, `price`, `volume`, `amount`, `trade_index`, `buy_no`, `sell_no`, `trade_type`, `trade_flag`。

---

## 9. 行情订阅 API

### 9.1 `POST /subscription`

| 项目 | 说明 |
|------|------|
| **功能** | 创建 xtdata 行情订阅，返回 `subscription_id` 供 REST 查询与 WebSocket 连接 |
| **请求体** | `SubscriptionRequest` |
| **响应** | **裸 JSON 对象**（非 `format_response`） |

**`SubscriptionRequest`**：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `symbols` | string[] | 是 | — | 非空；去空白；`whole_quote` 时仍必填但可被服务忽略或特殊处理 |
| `period` | `PeriodType` | 否 | `tick` | 订阅周期 |
| `start_date` | string | 否 | `""` | `YYYYMMDD` 或 `YYYYMMDDHHMMSS` |
| `adjust_type` | string | 否 | `none` | 见 §1.6 |
| `subscription_type` | `SubscriptionType` | 否 | `quote` | `quote` / `whole_quote` |

**响应字段**：

| 字段 | 说明 |
|------|------|
| `subscription_id` | UUID 风格字符串 |
| `status` | 如 `active` |
| `created_at` | ISO 时间 |
| `symbols` | `quote` 为请求代码列表；`whole_quote` 实现中常为 `["*"]` |
| `period` | 周期字符串 |
| `start_date` | 请求的起始日期字符串 |
| `adjust_type` | 复权类型 |
| `subscription_type` | `quote` / `whole_quote` |
| `message` | 人类可读说明 |

### 9.2 `DELETE /subscription/{subscription_id}`

| 项目 | 说明 |
|------|------|
| **功能** | 取消订阅 |
| **响应** | 裸对象：`success` (boolean), `message` (string), `subscription_id` (string) |

### 9.3 `GET /subscription/{subscription_id}`

| 项目 | 说明 |
|------|------|
| **功能** | 查询订阅详情 |
| **错误** | 不存在时 **404** |
| **响应** | 裸对象（与 `SubscriptionManager.get_subscription_info` 一致） |

| 字段 | 类型 | 说明 |
|------|------|------|
| `subscription_id` | string | ID |
| `subids_xtquant` | number[] | xtdata 内部订阅 ID 列表 |
| `symbols` | string[] | 订阅代码 |
| `period` | string | 周期 |
| `start_date` | string | 起始日期 |
| `adjust_type` | string | 复权 |
| `subscription_type` | string | `quote` / `whole_quote` |
| `created_at` | string | ISO |
| `last_heartbeat` | string | ISO |
| `active` | boolean | 是否活跃 |
| `queue_size` | number | 异步队列积压 |

> 模型 `SubscriptionInfoResponse` 与运行时返回相比缺少部分字段时，**以运行时返回为准**。

### 9.4 `GET /subscriptions`

| 项目 | 说明 |
|------|------|
| **功能** | 列出当前进程内所有订阅 |
| **响应** | 裸对象：`subscriptions`（上表结构的对象数组）, `total`（number） |

---

## 10. 交易服务 API（`/api/v1/trading`）

**认证**：`Authorization: Bearer <api_key>`。

### 10.1 `POST /connect`

| 项目 | 说明 |
|------|------|
| **功能** | 连接交易账户，获取 `session_id` |
| **请求体** | `ConnectRequest` |

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `account_id` | string | 是 | 账户 ID |
| `password` | string | 否 | 密码 |
| `client_id` | number | 否 | 客户端 ID |

**响应**（**裸** `ConnectResponse`）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `success` | boolean | 是否成功 |
| `message` | string | 说明 |
| `session_id` | string \| null | 后续接口会话标识 |
| `account_info` | object \| null | 成功时可能返回 `AccountInfo` |

**`AccountInfo`**：

| 字段 | 说明 |
|------|------|
| `account_id`, `account_name`, `status` | 标识与状态 |
| `account_type` | 见下表 `AccountType` |
| `balance`, `available_balance`, `frozen_balance` | 资金 |
| `market_value`, `total_asset` | 市值与总资产 |

**`AccountType`**：`FUTURE`, `SECURITY`, `CREDIT`, `FUTURE_OPTION`, `STOCK_OPTION`, `HUGANGTONG`, `INCOME_SWAP`, `NEW3BOARD`, `SHENGANGTONG`。

---

### 10.2 `POST /disconnect/{session_id}`

断开连接。**响应** 包装格式，`data.success` boolean。

---

### 10.3 `GET /account/{session_id}`

**裸** `AccountInfo`。

---

### 10.4 `GET /positions/{session_id}`

**裸** `PositionInfo[]`。

**`PositionInfo`**：

| 字段 | 说明 |
|------|------|
| `stock_code`, `stock_name` | 代码与名称 |
| `volume`, `available_volume`, `frozen_volume` | 持仓量 |
| `cost_price`, `market_price`, `market_value` | 成本与市值 |
| `profit_loss`, `profit_loss_ratio` | 盈亏 |

---

### 10.5 `POST /order/{session_id}`

**请求体** `OrderRequest`：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `stock_code` | string | 是 | — | 合约代码 |
| `side` | `OrderSide` | 是 | — | `BUY` / `SELL` |
| `order_type` | `OrderType` | 否 | `LIMIT` | `MARKET`, `LIMIT`, `STOP`, `STOP_LIMIT` |
| `volume` | number | 是 | — | 大于 0 |
| `price` | number | 否 | null | 限价等场景；若提供须 > 0 |
| `strategy_name` | string | 否 | — | 策略名 |

**响应**（**裸** `OrderResponse`）：

| 字段 | 说明 |
|------|------|
| `order_id`, `stock_code`, `side`, `order_type`, `volume`, `price`, `status` | 订单核心字段 |
| `submitted_time` | datetime → JSON 多为 ISO 8601 |
| `filled_volume`, `filled_amount`, `average_price` | 成交进度 |

**`OrderStatus`**：`PENDING`, `SUBMITTED`, `PARTIAL_FILLED`, `FILLED`, `CANCELLED`, `REJECTED`。

---

### 10.6 `POST /cancel/{session_id}`

**请求体** `CancelOrderRequest`：`order_id` string。  
**响应** 包装格式，`data.success` boolean。

---

### 10.7 `GET /orders/{session_id}`

**裸** `OrderResponse[]`。

---

### 10.8 `GET /trades/{session_id}`

**裸** `TradeInfo[]`。

**`TradeInfo`**：`trade_id`, `order_id`, `stock_code`, `side`, `volume`, `price`, `amount`, `trade_time`, `commission`。

---

### 10.9 `GET /asset/{session_id}`

**裸** `AssetInfo`：`total_asset`, `market_value`, `cash`, `frozen_cash`, `available_cash`, `profit_loss`, `profit_loss_ratio`。

---

### 10.10 `GET /risk/{session_id}`

**裸** `RiskInfo`：`position_ratio`, `cash_ratio`, `max_drawdown`, `var_95`, `var_99`。

---

### 10.11 `GET /strategies/{session_id}`

**裸** `StrategyInfo[]`：`strategy_name`, `strategy_type`, `status`, `created_time`, `last_update_time`, `parameters` (object)。

---

### 10.12 `GET /status/{session_id}`

**包装格式**，`data.connected` boolean，表示该 `session_id` 是否仍连接。

---

## 11. Web UI 静态资源

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/ui`, `/ui/`, `/ui/{asset_path}` | 构建后的前端；无构建产物时 404（见 `app/web_ui.py`） |

不在 OpenAPI schema 中（`include_in_schema=False`）。

---

## 12. SDK 维护清单

1. **认证**：统一 `Authorization: Bearer`；与 `security.api_keys` 对齐测试密钥。
2. **双形态响应**：按接口固定判断包装 vs 裸模型。
3. **WebSocket**：处理 `connected` / `quote` / `pong` / `error`；`quote.data` 按动态 dict 解析。
4. **与 OpenAPI 同步**：发版前 diff 路由与本文档；冲突以 **本仓库实现** 为准。
5. **占位接口**：`GET /etf/{etf_code}` 行为可能随实现替换，SDK 宜单测隔离。

---

## 13. 文档版本

| 日期 | 说明 |
|------|------|
| 2026-04-03 | 初版；同日扩充为逐接口说明（参数/字段表，对齐 `DataService` / 交易服务返回类型） |
