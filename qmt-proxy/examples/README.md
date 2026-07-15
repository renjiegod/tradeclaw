# QMT Proxy SDK 示例

## 快速开始

### 方式一：pip（推荐）

```bash
# 在项目根目录的 venv 中安装 SDK
cd z:\code\qmt-proxy
.venv\Scripts\python.exe -m pip install -e libs/qmt_proxy_sdk

# 运行示例
.venv\Scripts\python.exe examples/ma_crossover_strategy.py
```

### 方式二：uv

```bash
cd examples
uv sync
uv run ma_crossover_strategy.py
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `QMT_PROXY_URL` | `http://localhost:8000` | qmt-proxy 服务地址 |
| `QMT_API_KEY` | `your-api-key` | API 密钥 |
| `QMT_ACCOUNT_ID` | `test_account` | 交易账户 ID |

## 示例列表

### `ma_crossover_strategy.py` — 双均线交叉量化交易策略

完整演示 SDK 全部核心功能：

1. **服务健康检查** — `client.system.check_health()`
2. **历史数据选股** — `client.data.get_market_data()` 拉取日线计算均线筛选多头股
3. **交易会话管理** — `client.trading.connect()` / `disconnect()` / `get_asset()`
4. **WebSocket 实时行情** — `client.data.subscribe_and_stream()` 订阅 tick 流
5. **策略执行** — MA5/MA20 金叉买入、死叉卖出、无信号空仓
6. **委托与成交查询** — `client.trading.get_orders()` / `get_trades()`
