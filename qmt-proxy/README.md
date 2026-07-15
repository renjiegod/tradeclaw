# qmt-proxy

QMT（miniQMT）xtquant SDK 的多协议代理服务：在 Windows 侧运行，把 xtquant 的行情与交易能力以 **REST / gRPC / WebSocket** 暴露出来，供 doyoutrade 或其他客户端跨平台调用。

> ⚠️ 本服务默认用于本地联调与策略验证，投入生产前请自行评估账户与网络安全。

## 功能

- **REST API**（FastAPI）：健康检查、行情数据、交易、诊断、配置管理，自动生成 Swagger 文档（`/docs`）
- **gRPC**：与 REST 共享同一业务逻辑层（proto 定义见 `proto/`，生成代码见 `generated/`）
- **WebSocket**：行情订阅实时推送，内置心跳与限流
- **Web 工作台**：`web/` 内置 React 控制台，可查看订阅、实时推送与市场数据
- **运行模式**：`mock` / `dev` / `prod` 三档（环境变量 `APP_MODE` 选择）；`dev` 模式自动拦截真实交易
- **API Key 认证**：所有业务接口要求 `Authorization: Bearer <api-key>`
- **可观测性**：每次 xtdata 操作的耗时 / 参数 / 成败写入内存环形缓冲与 `logs/xtdata_ops.jsonl`，经 `GET /api/v1/diagnostics/*` 查询

## 运行要求

- Windows + 已登录的 miniQMT / QMT 客户端（`mock` 模式除外）
- Python 3.12（依赖见 `pyproject.toml`，xtquant 仅 Windows 可装）

## 快速开始

```bash
# 安装依赖
uv sync

# 启动（默认 dev 模式）
python run.py

# 或指定模式
APP_MODE=prod python run.py
```

- REST：`http://<host>:8000`，文档 `http://<host>:8000/docs`
- 配置文件：`~/.doyoutrade/qmt-proxy.yml`（环境变量 `QMT_PROXY_CONFIG` 覆盖路径；按 `modes.<APP_MODE>` 分段）
- Windows 一键安装：见 `installer/install.ps1`

## 目录结构

```
qmt-proxy/
├── app/
│   ├── main.py           # FastAPI 入口
│   ├── grpc_server.py    # gRPC 服务入口
│   ├── config.py         # 配置模型与加载
│   ├── config_store.py   # YAML 配置读写（mode-aware，round-trip 保注释）
│   ├── routers/          # REST 路由（health / data / trading / websocket / diagnostics / config）
│   ├── services/         # 业务逻辑层（REST 与 gRPC 共享）
│   ├── grpc_services/    # gRPC servicer
│   └── models/           # Pydantic 模型
├── proto/                # protobuf 定义
├── generated/            # protoc 生成代码
├── web/                  # React 控制台
├── installer/            # Windows 安装脚本
└── tests/                # 单元 / SDK 测试
```

## 与 doyoutrade 集成

doyoutrade 启动时 `--mode both` 会在同进程内以 daemon 线程运行本服务（打包为 `doyoutrade/_qmt_proxy`），并把默认账户 `base_url` 指向本机；也可独立部署在 Windows 主机上，由 doyoutrade 通过数据账户的 `base_url` + token 远程访问。详见仓库根 `AGENTS.md`。

## 测试

```bash
make test        # 单元测试
```

## 许可证

Apache License 2.0，见 [LICENSE](LICENSE)。第三方许可声明见 [NOTICE](NOTICE) 与 `licenses/`。
