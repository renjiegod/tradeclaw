"""
双均线交叉量化交易策略示例（含仓位管理）
========================================

本示例展示如何使用 qmt-proxy SDK 完成一个完整的量化交易流程：

1. 连接服务 & 健康检查
2. 获取候选股票池，通过历史行情数据进行选股
3. 建立交易会话 & 初始化仓位管理器（查询资金、分配仓位）
4. 通过 WebSocket 订阅目标股实时行情
5. 基于双均线（MA5/MA20）交叉策略生成买卖信号
6. 根据仓位管理器动态计算下单数量，执行买入 / 卖出 / 空仓操作

仓位管理规则::

    - 总仓位上限：不超过总资产的 80%（可配置）
    - 单只个股上限：不超过总资产的 30%（可配置）
    - 可用资金按目标股数量均分，每笔买入量 = 分配资金 / 现价，向下取整到 100 股
    - 卖出时按实际持仓数量全部卖出
    - 每笔交易后实时刷新账户资产和持仓

运行方式::

    # 确保 qmt-proxy 服务已启动
    python examples/ma_crossover_strategy.py

环境变量（可选）::

    QMT_PROXY_URL   服务地址，默认 http://localhost:8000
    QMT_API_KEY     API 密钥（必填，见 config.yml 的 api_keys）
    QMT_ACCOUNT_ID  交易账户；未设置时使用占位值 test_account 并在连接失败时给出提示
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from qmt_proxy_sdk import AsyncQmtProxyClient, QmtProxyError

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("策略")

# ---------------------------------------------------------------------------
# 策略参数
# ---------------------------------------------------------------------------

EXAMPLE_ENV_PATH = Path(__file__).with_name(".env")
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_API_KEY = "your-api-key"
DEFAULT_ACCOUNT_ID = "test_account"


def resolve_runtime_settings(env_path: Path | None = None) -> dict[str, str]:
    """加载 examples/.env，并保留已存在的环境变量优先级。"""
    load_dotenv(dotenv_path=env_path or EXAMPLE_ENV_PATH, override=False)
    return {
        "base_url": os.getenv("QMT_PROXY_URL", DEFAULT_BASE_URL),
        "api_key": os.getenv("QMT_API_KEY", DEFAULT_API_KEY),
        "account_id": os.getenv("QMT_ACCOUNT_ID", DEFAULT_ACCOUNT_ID),
    }

SHORT_MA_PERIOD = 5
LONG_MA_PERIOD = 20
SCREENING_DAYS = 60
MAX_POSITIONS = 3                   # 最大同时持仓数

# 仓位管理参数
MAX_TOTAL_POSITION_RATIO = 0.80     # 总仓位上限（占总资产比例）
MAX_SINGLE_POSITION_RATIO = 0.30    # 单只个股仓位上限（占总资产比例）
MIN_ORDER_VOLUME = 100              # A 股最小交易单位（股）

CANDIDATE_STOCKS = [
    "600519.SH",  # 贵州茅台
    "000858.SZ",  # 五粮液
    "601318.SH",  # 中国平安
    "000333.SZ",  # 美的集团
    "600036.SH",  # 招商银行
    "000001.SZ",  # 平安银行
    "601166.SH",  # 兴业银行
    "600276.SH",  # 恒瑞医药
    "000651.SZ",  # 格力电器
    "002415.SZ",  # 海康威视
]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class StockContext:
    """单只股票的运行时上下文，保存滑动窗口价格与状态。"""

    code: str
    prices: deque = field(default_factory=lambda: deque(maxlen=LONG_MA_PERIOD))
    prev_short_ma: float | None = None
    prev_long_ma: float | None = None
    held: bool = False
    held_volume: int = 0       # 当前持仓数量
    held_cost: float = 0.0     # 当前持仓成本价


def calc_ma(prices: deque, period: int) -> float | None:
    if len(prices) < period:
        return None
    return sum(list(prices)[-period:]) / period


def calc_change_pct(last_price: float | None, pre_close: float | None) -> float | None:
    """根据昨收计算涨跌幅百分比。"""
    if last_price is None or pre_close is None or pre_close <= 0:
        return None
    return (last_price - pre_close) / pre_close * 100


def format_connect_failure_message(account_id: str, message: str) -> str:
    """为常见的占位账户失败补充可操作的排查提示。"""
    base_message = f"交易连接失败: {message}"
    if account_id == DEFAULT_ACCOUNT_ID and "订阅交易账户失败" in message:
        return (
            f"{base_message}。当前示例正在使用默认占位账户 "
            f"`{DEFAULT_ACCOUNT_ID}`，请设置环境变量 `QMT_ACCOUNT_ID` 为 QMT 中可订阅的真实账户后重试。"
        )
    return base_message


def format_tick_log_line(
    tick_count: int,
    quote,
    short_ma: float | None,
    long_ma: float | None,
    position_str: str,
) -> str:
    """格式化实时 tick 日志，缺失的非关键字段统一显示为 N/A。"""
    change_pct = calc_change_pct(quote.last_price, getattr(quote, "pre_close", None))

    price_str = f"{quote.last_price:.2f}" if quote.last_price is not None else "N/A"
    change_pct_str = f"{change_pct:.2f}%" if change_pct is not None else "N/A"
    amount = getattr(quote, "amount", None)
    amount_str = f"{amount:.2f}" if amount is not None else "N/A"
    volume = getattr(quote, "volume", None)
    volume_str = str(volume) if volume is not None else "N/A"
    short_ma_str = f"{short_ma:.2f}" if short_ma is not None else "N/A"
    long_ma_str = f"{long_ma:.2f}" if long_ma is not None else "N/A"

    return (
        f"[TICK #{tick_count:04d}] {quote.stock_code} | "
        f"价格={price_str} | 涨跌幅={change_pct_str} | "
        f"成交额={amount_str} | 量={volume_str} | "
        f"MA{SHORT_MA_PERIOD}={short_ma_str} | "
        f"MA{LONG_MA_PERIOD}={long_ma_str} | 持仓={position_str}"
    )


class PositionManager:
    """仓位管理器：根据账户资金动态分配每只股票的下单数量。"""

    def __init__(
        self,
        total_asset: float,
        available_cash: float,
        market_value: float,
        target_count: int,
    ) -> None:
        self.total_asset = total_asset
        self.available_cash = available_cash
        self.market_value = market_value
        self.target_count = max(target_count, 1)
        self._log_allocation_plan()

    def _log_allocation_plan(self) -> None:
        max_position_value = self.total_asset * MAX_TOTAL_POSITION_RATIO
        remaining_capacity = max(max_position_value - self.market_value, 0)
        investable = min(self.available_cash, remaining_capacity)
        per_stock_budget = investable / self.target_count
        single_cap = self.total_asset * MAX_SINGLE_POSITION_RATIO

        log.info("仓位管理器初始化:")
        log.info("  总资产:       %.2f", self.total_asset)
        log.info("  可用资金:     %.2f", self.available_cash)
        log.info("  已用仓位:     %.2f (%.1f%%)", self.market_value, self.market_value / self.total_asset * 100 if self.total_asset else 0)
        log.info("  总仓位上限:   %.2f (%.0f%%)", max_position_value, MAX_TOTAL_POSITION_RATIO * 100)
        log.info("  剩余可投资金: %.2f", investable)
        log.info("  目标股数量:   %d", self.target_count)
        log.info("  每股预算:     %.2f", per_stock_budget)
        log.info("  单股仓位上限: %.2f (%.0f%%)", single_cap, MAX_SINGLE_POSITION_RATIO * 100)

    def refresh(self, total_asset: float, available_cash: float, market_value: float) -> None:
        """交易后刷新资金数据。"""
        self.total_asset = total_asset
        self.available_cash = available_cash
        self.market_value = market_value

    def calc_buy_volume(self, stock_code: str, price: float, current_position_value: float = 0) -> int:
        """计算买入数量（向下取整到 100 股）。

        Returns 0 表示不应买入（资金不足或超出仓位限制）。
        """
        if price <= 0 or self.total_asset <= 0:
            return 0

        # 检查总仓位上限
        max_position_value = self.total_asset * MAX_TOTAL_POSITION_RATIO
        remaining_capacity = max(max_position_value - self.market_value, 0)
        if remaining_capacity < price * MIN_ORDER_VOLUME:
            log.info(
                "  [仓位] 总仓位已达 %.1f%%，剩余容量 %.2f 不足买入 %s",
                self.market_value / self.total_asset * 100,
                remaining_capacity,
                stock_code,
            )
            return 0

        # 检查单股仓位上限
        single_cap = self.total_asset * MAX_SINGLE_POSITION_RATIO
        single_remaining = max(single_cap - current_position_value, 0)
        if single_remaining < price * MIN_ORDER_VOLUME:
            log.info(
                "  [仓位] %s 单股仓位已达 %.1f%%，上限 %.0f%%",
                stock_code,
                current_position_value / self.total_asset * 100,
                MAX_SINGLE_POSITION_RATIO * 100,
            )
            return 0

        # 按目标数量均分可用资金
        per_stock_budget = min(self.available_cash, remaining_capacity) / self.target_count
        # 取均分预算和单股上限中较小值
        investable = min(per_stock_budget, single_remaining)
        volume = int(investable / price)
        # 向下取整到 100 股
        volume = (volume // MIN_ORDER_VOLUME) * MIN_ORDER_VOLUME

        if volume < MIN_ORDER_VOLUME:
            log.info(
                "  [仓位] %s 可投资金 %.2f 不足买入 %d 股 (需 %.2f)",
                stock_code,
                investable,
                MIN_ORDER_VOLUME,
                price * MIN_ORDER_VOLUME,
            )
            return 0

        log.info(
            "  [仓位] %s 分配资金=%.2f | 计算买入=%d股 | 预计金额=%.2f",
            stock_code,
            investable,
            volume,
            volume * price,
        )
        return volume


# ---------------------------------------------------------------------------
# 阶段 1：健康检查
# ---------------------------------------------------------------------------


async def check_service(client: AsyncQmtProxyClient) -> None:
    log.info("=" * 60)
    log.info("阶段 1：服务健康检查")
    log.info("=" * 60)

    health = await client.system.check_health()
    log.info(
        "服务状态: %s | 版本: %s | 模式: %s | 时间: %s",
        health.status,
        health.app_version,
        health.xtquant_mode,
        health.timestamp,
    )

    info = await client.system.get_info()
    log.info(
        "服务详情: %s:%d | 调试=%s | 允许实盘=%s",
        info.host,
        info.port,
        info.debug,
        info.allow_real_trading,
    )


# ---------------------------------------------------------------------------
# 阶段 2：选股
# ---------------------------------------------------------------------------


async def screen_stocks(client: AsyncQmtProxyClient) -> list[str]:
    log.info("")
    log.info("=" * 60)
    log.info("阶段 2：基于历史行情选股")
    log.info("=" * 60)

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=SCREENING_DAYS)).strftime("%Y%m%d")

    log.info(
        "候选池: %d 只 | 数据区间: %s ~ %s | 周期: 1d",
        len(CANDIDATE_STOCKS),
        start_date,
        end_date,
    )

    market_results = await client.data.get_market_data(
        stock_codes=CANDIDATE_STOCKS,
        start_date=start_date,
        end_date=end_date,
        period="1d",
        fields=["close", "volume"],
    )

    selected: list[str] = []

    for item in market_results:
        code = item.stock_code
        rows = item.data
        if len(rows) < LONG_MA_PERIOD:
            log.info("  [跳过] %s — 数据不足 (%d 条，需 %d)", code, len(rows), LONG_MA_PERIOD)
            continue

        closes = [float(r["close"]) for r in rows if r.get("close") is not None]
        volumes = [float(r["volume"]) for r in rows if r.get("volume") is not None]

        if len(closes) < LONG_MA_PERIOD:
            log.info("  [跳过] %s — 有效收盘价不足", code)
            continue

        ma_short = sum(closes[-SHORT_MA_PERIOD:]) / SHORT_MA_PERIOD
        ma_long = sum(closes[-LONG_MA_PERIOD:]) / LONG_MA_PERIOD
        avg_volume = sum(volumes[-LONG_MA_PERIOD:]) / LONG_MA_PERIOD if volumes else 0
        latest_close = closes[-1]

        trend = "多头" if ma_short > ma_long else "空头"
        log.info(
            "  %s | 最新价: %.2f | MA%d: %.2f | MA%d: %.2f | "
            "均量: %.0f | 趋势: %s",
            code,
            latest_close,
            SHORT_MA_PERIOD,
            ma_short,
            LONG_MA_PERIOD,
            ma_long,
            avg_volume,
            trend,
        )

        # 选股条件：短期均线在长期均线之上（多头排列）且有成交量
        if ma_short > ma_long and avg_volume > 0:
            selected.append(code)

    log.info("")
    if selected:
        log.info("选股结果: %s （共 %d 只）", ", ".join(selected), len(selected))
    else:
        log.info("选股结果: 无符合条件的股票，将使用候选池前 %d 只", MAX_POSITIONS)
        selected = CANDIDATE_STOCKS[:MAX_POSITIONS]

    return selected


# ---------------------------------------------------------------------------
# 阶段 3：建立交易会话
# ---------------------------------------------------------------------------


async def connect_trading(
    client: AsyncQmtProxyClient, target_count: int, account_id: str
) -> tuple[str, PositionManager]:
    log.info("")
    log.info("=" * 60)
    log.info("阶段 3：建立交易会话 & 初始化仓位管理")
    log.info("=" * 60)

    resp = await client.trading.connect(account_id=account_id)
    log.info("连接结果: success=%s | message=%s", resp.success, resp.message)

    if not resp.success or not resp.session_id:
        raise RuntimeError(format_connect_failure_message(account_id, resp.message))

    session_id = resp.session_id
    log.info("会话 ID: %s", session_id)

    # 查询账户资产
    asset = await client.trading.get_asset(session_id)
    log.info(
        "账户资产: 总资产=%.2f | 可用资金=%.2f | 持仓市值=%.2f | 盈亏=%.2f (%.2f%%)",
        asset.total_asset,
        asset.available_cash,
        asset.market_value,
        asset.profit_loss,
        asset.profit_loss_ratio * 100,
    )

    # 查询当前持仓
    positions = await client.trading.get_positions(session_id)
    if positions:
        log.info("当前持仓 (%d 只):", len(positions))
        for pos in positions:
            log.info(
                "  %s %s | 数量=%d | 可用=%d | 成本=%.2f | 现价=%.2f | 市值=%.2f | 盈亏=%.2f",
                pos.stock_code,
                pos.stock_name,
                pos.volume,
                pos.available_volume,
                pos.cost_price,
                pos.market_price,
                pos.market_value,
                pos.profit_loss,
            )
    else:
        log.info("当前无持仓")

    log.info("")
    pm = PositionManager(
        total_asset=asset.total_asset,
        available_cash=asset.available_cash,
        market_value=asset.market_value,
        target_count=target_count,
    )

    return session_id, pm


# ---------------------------------------------------------------------------
# 阶段 4 & 5：实时监听 + 交易执行
# ---------------------------------------------------------------------------


async def sync_existing_positions(
    client: AsyncQmtProxyClient,
    session_id: str,
    contexts: dict[str, StockContext],
) -> None:
    """将账户中已有持仓同步到 StockContext，避免重复买入或遗漏卖出。"""
    positions = await client.trading.get_positions(session_id)
    for pos in positions:
        if pos.stock_code in contexts and pos.volume > 0:
            ctx = contexts[pos.stock_code]
            ctx.held = True
            ctx.held_volume = pos.available_volume
            ctx.held_cost = pos.cost_price
            log.info(
                "  [同步] %s 已持仓 %d 股 (可用 %d)，成本 %.2f",
                pos.stock_code,
                pos.volume,
                pos.available_volume,
                pos.cost_price,
            )


async def run_realtime_strategy(
    client: AsyncQmtProxyClient,
    session_id: str,
    targets: list[str],
    pm: PositionManager,
) -> None:
    log.info("")
    log.info("=" * 60)
    log.info("阶段 4：订阅实时行情 & 执行交易策略")
    log.info("=" * 60)
    log.info(
        "目标股票: %s | 策略: MA%d/MA%d 交叉 | 最大持仓=%d",
        ", ".join(targets),
        SHORT_MA_PERIOD,
        LONG_MA_PERIOD,
        MAX_POSITIONS,
    )
    log.info(
        "仓位规则: 总上限=%.0f%% | 单股上限=%.0f%% | 最小单位=%d股",
        MAX_TOTAL_POSITION_RATIO * 100,
        MAX_SINGLE_POSITION_RATIO * 100,
        MIN_ORDER_VOLUME,
    )

    contexts: dict[str, StockContext] = {code: StockContext(code=code) for code in targets}

    # 同步已有持仓到上下文
    await sync_existing_positions(client, session_id, contexts)

    tick_count = 0
    trade_count = 0

    log.info("正在建立 WebSocket 连接...")

    stream = client.data.subscribe_and_stream(symbols=targets)
    async with stream:
        log.info("WebSocket 已连接，开始接收实时行情\n")

        async for quote in stream:
            tick_count += 1
            code = quote.stock_code
            if code is None or code not in contexts:
                continue

            ctx = contexts[code]
            price = quote.last_price
            if price is None or price <= 0:
                continue

            ctx.prices.append(price)

            short_ma = calc_ma(ctx.prices, SHORT_MA_PERIOD)
            long_ma = calc_ma(ctx.prices, LONG_MA_PERIOD)

            position_str = f"{ctx.held_volume}股" if ctx.held else "无"
            log.info(
                format_tick_log_line(
                    tick_count=tick_count,
                    quote=quote,
                    short_ma=short_ma,
                    long_ma=long_ma,
                    position_str=position_str,
                )
            )

            if short_ma is None or long_ma is None:
                log.info(
                    "  → 数据积累中 (%d/%d)，暂不决策",
                    len(ctx.prices),
                    LONG_MA_PERIOD,
                )
                ctx.prev_short_ma = short_ma
                ctx.prev_long_ma = long_ma
                continue

            # 检测金叉 / 死叉
            signal = detect_signal(ctx, short_ma, long_ma)

            if signal == "BUY" and not ctx.held:
                held_count = sum(1 for c in contexts.values() if c.held)
                if held_count >= MAX_POSITIONS:
                    log.info("  → 金叉信号！但已达最大持仓数 %d，跳过", MAX_POSITIONS)
                else:
                    log.info(
                        "  ★ 金叉买入信号！MA%d(%.2f) 上穿 MA%d(%.2f)",
                        SHORT_MA_PERIOD,
                        short_ma,
                        LONG_MA_PERIOD,
                        long_ma,
                    )
                    current_pos_value = ctx.held_volume * price
                    volume = pm.calc_buy_volume(code, price, current_pos_value)
                    if volume > 0:
                        success = await execute_buy(client, session_id, ctx, price, volume)
                        if success:
                            trade_count += 1
                            await refresh_position_manager(client, session_id, pm)
                    else:
                        log.info("  → 仓位管理器判定: 不满足买入条件，跳过")

            elif signal == "SELL" and ctx.held:
                log.info(
                    "  ★ 死叉卖出信号！MA%d(%.2f) 下穿 MA%d(%.2f)",
                    SHORT_MA_PERIOD,
                    short_ma,
                    LONG_MA_PERIOD,
                    long_ma,
                )
                success = await execute_sell(client, session_id, ctx, price)
                if success:
                    trade_count += 1
                    await refresh_position_manager(client, session_id, pm)

            else:
                action = "持仓观望" if ctx.held else "空仓等待"
                log.info("  → %s（无交叉信号）", action)

            ctx.prev_short_ma = short_ma
            ctx.prev_long_ma = long_ma

    log.info("")
    log.info("实时行情流已结束，共处理 %d 个 tick，执行 %d 笔交易", tick_count, trade_count)


async def refresh_position_manager(
    client: AsyncQmtProxyClient, session_id: str, pm: PositionManager
) -> None:
    """交易后刷新仓位管理器中的资金数据。"""
    try:
        asset = await client.trading.get_asset(session_id)
        pm.refresh(asset.total_asset, asset.available_cash, asset.market_value)
        log.info(
            "  [仓位刷新] 总资产=%.2f | 可用=%.2f | 已用仓位=%.2f (%.1f%%)",
            asset.total_asset,
            asset.available_cash,
            asset.market_value,
            asset.market_value / asset.total_asset * 100 if asset.total_asset else 0,
        )
    except QmtProxyError as exc:
        log.warning("  [仓位刷新] 查询资产失败: %s", exc)


def detect_signal(ctx: StockContext, short_ma: float, long_ma: float) -> str:
    """检测均线交叉信号: BUY（金叉）/ SELL（死叉）/ HOLD。"""
    if ctx.prev_short_ma is None or ctx.prev_long_ma is None:
        return "HOLD"

    prev_diff = ctx.prev_short_ma - ctx.prev_long_ma
    curr_diff = short_ma - long_ma

    if prev_diff <= 0 < curr_diff:
        return "BUY"
    if prev_diff >= 0 > curr_diff:
        return "SELL"
    return "HOLD"


async def execute_buy(
    client: AsyncQmtProxyClient,
    session_id: str,
    ctx: StockContext,
    price: float,
    volume: int,
) -> bool:
    cost = volume * price
    log.info(
        "  → 提交买入委托: %s | 价格=%.2f | 数量=%d | 预计金额=%.2f",
        ctx.code, price, volume, cost,
    )
    try:
        order = await client.trading.submit_order(
            session_id=session_id,
            stock_code=ctx.code,
            side="BUY",
            volume=volume,
            price=price,
            order_type="LIMIT",
            strategy_name="ma_crossover",
        )
        ctx.held = True
        ctx.held_volume += volume
        ctx.held_cost = price
        log.info(
            "  ✓ 买入委托已提交 | 订单号=%s | 状态=%s | 时间=%s | 累计持仓=%d股",
            order.order_id,
            order.status,
            order.submitted_time,
            ctx.held_volume,
        )
        return True
    except QmtProxyError as exc:
        log.error("  ✗ 买入委托失败: %s", exc)
        return False


async def execute_sell(
    client: AsyncQmtProxyClient,
    session_id: str,
    ctx: StockContext,
    price: float,
) -> bool:
    sell_volume = ctx.held_volume
    if sell_volume <= 0:
        sell_volume = MIN_ORDER_VOLUME
    expected_amount = sell_volume * price
    log.info(
        "  → 提交卖出委托: %s | 价格=%.2f | 数量=%d（全部卖出）| 预计金额=%.2f",
        ctx.code, price, sell_volume, expected_amount,
    )
    try:
        order = await client.trading.submit_order(
            session_id=session_id,
            stock_code=ctx.code,
            side="SELL",
            volume=sell_volume,
            price=price,
            order_type="LIMIT",
            strategy_name="ma_crossover",
        )
        profit = (price - ctx.held_cost) * sell_volume if ctx.held_cost > 0 else 0
        log.info(
            "  ✓ 卖出委托已提交 | 订单号=%s | 状态=%s | 时间=%s | 预估盈亏=%.2f",
            order.order_id,
            order.status,
            order.submitted_time,
            profit,
        )
        ctx.held = False
        ctx.held_volume = 0
        ctx.held_cost = 0.0
        return True
    except QmtProxyError as exc:
        log.error("  ✗ 卖出委托失败: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 阶段 6：收尾 — 查询成交 & 断开连接
# ---------------------------------------------------------------------------


async def finalize(client: AsyncQmtProxyClient, session_id: str) -> None:
    log.info("")
    log.info("=" * 60)
    log.info("阶段 5：交易汇总 & 收尾")
    log.info("=" * 60)

    try:
        orders = await client.trading.get_orders(session_id)
        log.info("今日委托 (%d 笔):", len(orders))
        for o in orders:
            log.info(
                "  %s | %s %s | 量=%d | 价=%.2f | 成交量=%d | 状态=%s",
                o.order_id,
                o.side,
                o.stock_code,
                o.volume,
                o.price or 0,
                o.filled_volume,
                o.status,
            )
    except QmtProxyError as exc:
        log.warning("查询委托失败: %s", exc)

    try:
        trades = await client.trading.get_trades(session_id)
        log.info("今日成交 (%d 笔):", len(trades))
        for t in trades:
            log.info(
                "  %s | %s %s | 量=%d | 价=%.2f | 金额=%.2f | 佣金=%.2f",
                t.trade_id,
                t.side,
                t.stock_code,
                t.volume,
                t.price,
                t.amount,
                t.commission,
            )
    except QmtProxyError as exc:
        log.warning("查询成交失败: %s", exc)

    try:
        asset = await client.trading.get_asset(session_id)
        log.info(
            "最终账户: 总资产=%.2f | 可用=%.2f | 持仓市值=%.2f | 盈亏=%.2f",
            asset.total_asset,
            asset.available_cash,
            asset.market_value,
            asset.profit_loss,
        )
    except QmtProxyError as exc:
        log.warning("查询资产失败: %s", exc)

    try:
        await client.trading.disconnect(session_id=session_id)
        log.info("交易会话已断开")
    except QmtProxyError as exc:
        log.warning("断开会话失败: %s", exc)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


async def main() -> None:
    runtime_settings = resolve_runtime_settings()
    base_url = runtime_settings["base_url"]
    api_key = runtime_settings["api_key"]
    account_id = runtime_settings["account_id"]

    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║       QMT Proxy SDK — 双均线交叉量化交易策略示例        ║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    log.info("服务地址: %s", base_url)
    log.info("交易账户: %s", account_id)
    log.info(
        "策略参数: MA%d / MA%d | 选股天数=%d | 最大持仓=%d",
        SHORT_MA_PERIOD,
        LONG_MA_PERIOD,
        SCREENING_DAYS,
        MAX_POSITIONS,
    )
    log.info(
        "仓位管理: 总上限=%.0f%% | 单股上限=%.0f%% | 最小单位=%d股",
        MAX_TOTAL_POSITION_RATIO * 100,
        MAX_SINGLE_POSITION_RATIO * 100,
        MIN_ORDER_VOLUME,
    )
    log.info("")

    async with AsyncQmtProxyClient(base_url=base_url, api_key=api_key) as client:
        # 阶段 1：健康检查
        await check_service(client)

        # 阶段 2：选股
        targets = await screen_stocks(client)

        # 阶段 3：建立交易连接 & 初始化仓位管理
        session_id, pm = await connect_trading(client, target_count=len(targets), account_id=account_id)

        # 阶段 4 & 5：实时监听 + 策略执行
        try:
            await run_realtime_strategy(client, session_id, targets, pm)
        except KeyboardInterrupt:
            log.info("\n收到中断信号，正在退出...")
        except QmtProxyError as exc:
            log.error("策略运行异常: %s", exc)
        finally:
            # 阶段 6：收尾
            await finalize(client, session_id)

    log.info("")
    log.info("程序结束")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("用户中断，程序退出")
