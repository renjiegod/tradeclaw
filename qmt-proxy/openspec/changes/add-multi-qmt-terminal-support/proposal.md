## Why

`qmt-proxy` 此前假设一个进程只对接单个 QMT 终端：交易和行情都隐式绑定到唯一的 `xtquant.qmt_userdata_path`。但实际部署中，用户常同时持有分属不同券商 / 不同终端的多个 QMT 账户，需要同一个 qmt-proxy 进程为多个终端服务。交易必须按终端隔离（不同券商的下单不能共用一个 `XtQuantTrader`），而行情数据与券商无关，无需为每个终端各连一份 `xtdata`。补充“多 QMT 终端（多客户端）”能力，可以让多券商 / 多终端在单进程内同时交易，同时保持单终端部署完全向后兼容。

## What Changes

- 在 `config.yml` 的 `xtquant` 段新增 `clients` 列表，每项含 `client_id`（必填）、`name`、`qmt_userdata_path`、`mode`（可选，留空回退全局）、`allow_real_trading`（可选，留空回退全局）、`is_data_source`（标记行情数据源）；并新增 `default_client_id`、`data_source_client_id`。对应 `app/config.py` 的 `QmtClientConfig` / `XTQuantConfig`。
- 交易侧新增 `app/services/trading_manager.py` 的 `TradingClientManager`，为每个 `client_id` 维护独立的 `XtQuantTrader`（以组合方式持有多个单终端 `TradingService`，不改变 `TradingService` 自身的单终端语义），按 `client_id` 路由实现多券商 / 多终端同时交易。
- 数据侧维持单一数据源：`xtdata` 是进程级全局单例，行情与券商无关，因此只用一个“数据源终端”；历史取数仍走子进程隔离（`app/utils/xtdata_worker.py`），按所选终端的 `qmt_userdata_path` 设置 `data_dir`。
- 请求侧通过 HTTP 头 `X-QMT-Terminal: <client_id>` 选择终端（gRPC 用 metadata `x-qmt-terminal`）；缺省走 `default_client_id`，未知 `client_id` 返回 HTTP 400 且 `detail.error_code == "UNKNOWN_TERMINAL"`，不静默回退。
- 新增 `GET /api/v1/trading/clients` 列出所有终端及状态；诊断接口 `GET /api/v1/diagnostics/xtdata-ops` 与 `GET /api/v1/diagnostics/summary` 支持按 `client_id` 过滤，`summary` 不传 `client_id` 时附带分终端拆分。
- `libs/qmt_proxy_sdk` 的 `AsyncQmtProxyClient` 新增 `terminal_id` 参数，自动带上 `X-QMT-Terminal` 头。

## Capabilities

### New Capabilities
- `multi-qmt-terminal`: 在单个 qmt-proxy 进程内支持多个 QMT 终端：交易按 `client_id` 路由到独立的 `XtQuantTrader`，行情共用单一数据源终端，并通过 `X-QMT-Terminal` 头 / `x-qmt-terminal` metadata 选择终端。

### Modified Capabilities
- None.

## Contract

- **配置（`app/config.py`）**：`xtquant.clients` 可空；未配置时由 `xtquant.qmt_userdata_path` + 全局 mode 合成 `client_id="default"` 的单终端（同时作为默认与数据源终端），行为与旧版完全一致。`data_source_client_id` 解析优先级：显式配置 > `is_data_source` 标记 > 默认终端。
- **REST 路由**：`/api/v1/trading/*` 全部支持 `X-QMT-Terminal`；`POST /api/v1/data/market`、`POST /api/v1/data/download/history-data` 接受可选 `X-QMT-Terminal`，缺省数据源终端。
- **`/api/v1/trading/clients`**：返回每个终端的 `client_id` / `name` / `qmt_userdata_path` / `mode` / `allow_real_trading` / `is_data_source` / `is_default` / `loaded` / `initialized` / `init_failure_reason`，以及 `default_client_id`；列出状态不会强制初始化未加载的终端。
- **错误**：未知 `client_id` → HTTP 400，`detail.error_code == "UNKNOWN_TERMINAL"`。
- **gRPC**：交易服务按 metadata `x-qmt-terminal` 路由（与 REST 头等价）。
- **SDK**：`AsyncQmtProxyClient(base_url=..., api_key=..., terminal_id="<client_id>")` 自动注入 `X-QMT-Terminal` 头。

## Impact

- `app/config.py`（`QmtClientConfig` / `XTQuantConfig` 及解析方法）、`app/services/trading_manager.py`（`TradingClientManager`）、`app/dependencies.py`（`get_client_id` + `X-QMT-Terminal` 头）、`app/routers/trading.py`（`/clients` + 路由）、`app/routers/data.py`（数据接口接受终端）、`app/routers/diagnostics.py`（按 `client_id` 过滤 + 分终端拆分）、`app/grpc_services/trading_grpc_service.py`（metadata 路由）、`libs/qmt_proxy_sdk`（`terminal_id`）。
- 行情仍是进程级单数据源，不引入每终端独立 `xtdata` 连接。
- 不带 `X-QMT-Terminal` 头、未配置 `clients` 的现有调用方无需任何改动（向后兼容）。
- 功能已实现并通过单元测试；本变更仅记录契约，不改动代码。
