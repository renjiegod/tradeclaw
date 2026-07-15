## Why

`qmt-proxy` 已经提供了订阅管理 REST API、实时行情 WebSocket 和多类数据查询接口，但当前只能通过 Swagger、测试脚本或手写请求进行操作，缺少一个可直接用于观察订阅状态、验证实时推送和执行常用数据查询的可视化入口。补充内置 Web UI 可以降低联调和日常运维成本，也为后续扩展交易与监控页面提供统一前端基础。

## What Changes

- 新增一个基于 React、Ant Design 的 Web UI，用于配置服务地址和 API Key，并进入统一的数据工作台。
- 提供订阅管理视图，展示当前订阅列表、订阅详情、订阅状态和最近收到的推送数据。
- 提供实时行情面板，支持创建订阅、连接对应 WebSocket、展示连接状态、心跳状态和滚动消息流。
- 提供数据查询面板，支持通过现有 REST API 发起市场数据查询，并展示结构化结果与错误反馈。
- 提供前端到后端的集成方式，包括本地开发代理、生产构建产物交付以及服务端静态资源挂载。
- 补充与 Web UI 相关的测试和文档，确保核心页面、数据流和错误处理可验证。

## Capabilities

### New Capabilities
- `market-data-web-ui`: 提供一个内置的前端控制台，用于订阅管理、实时推送查看和数据查询。

### Modified Capabilities
- None.

## Impact

- 新增前端工程与构建链路，例如 React、TypeScript、Vite、Ant Design 及其测试工具。
- FastAPI 应用需要增加静态资源交付入口，用于在单服务部署时托管前端构建产物。
- 复用现有 `POST /api/v1/data/subscription`、`GET /api/v1/data/subscriptions`、`GET /api/v1/data/subscription/{subscription_id}`、`DELETE /api/v1/data/subscription/{subscription_id}`、`POST /api/v1/data/market` 与 `GET /ws/quote/{subscription_id}`，不引入破坏性 API 变更。
- 会影响开发文档、启动方式和 CI 流程，因为仓库将首次引入 Node 前端依赖与前端测试。
