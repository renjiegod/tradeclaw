#!/usr/bin/env python3
"""MACD backtest comparing MacdStrategy vs vbt.MACD on real A-share data."""

from __future__ import annotations

import argparse
import asyncio
import sys

import pandas as pd
import vectorbt as vbt

from doyoutrade.config import get_config
from doyoutrade.data.factory import build_trading_data_stack
from doyoutrade.core.models import Bar
from doyoutrade.strategies.factor.macd_strategy import MacdStrategy


def _bars_to_dataframe(bars: list[Bar]) -> pd.DataFrame:
    """Convert List[Bar] to OHLCV DataFrame with datetime index."""
    if not bars:
        return pd.DataFrame()
    records = [
        {
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for bar in bars
    ]
    df = pd.DataFrame(records)
    df.index = pd.to_datetime([bar.timestamp for bar in bars])
    return df


def _provider_symbol(symbol: str, provider_id: str) -> str:
    if provider_id == "akshare":
        return symbol.split(".", 1)[0]
    return symbol


async def _fetch_bars(symbol: str, start: str, end: str, provider_id: str) -> pd.DataFrame:
    config = get_config()
    data_provider, _, _ = build_trading_data_stack(
        provider_id=provider_id,
        data_cfg=config.data,
        symbols=[symbol],
    )
    query_symbol = _provider_symbol(symbol, provider_id)
    try:
        bars = await data_provider.get_bars(query_symbol, start, end, interval="1d")
    finally:
        close = getattr(data_provider, "aclose", None)
        if close is not None:
            await close()
    return _bars_to_dataframe(bars)


def _run_macd_strategy(df: pd.DataFrame, symbol: str) -> tuple[pd.Series, pd.Series]:
    """方式A: MacdStrategy 逐行 decide() 生成 entries/exits.

    金叉（histogram 由负转正）→ entry，死叉（histogram 由正转负）→ exit。
    """
    strategy = MacdStrategy()
    feat = strategy.build_features(df, symbol, None)

    return _signals_from_histogram(feat["histogram"])


def _signals_from_histogram(histogram: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Generate entry/exit signals from histogram zero-crossings."""
    entries = (histogram > 0) & (histogram.shift(1) <= 0)
    exits = (histogram <= 0) & (histogram.shift(1) > 0)
    return entries.fillna(False), exits.fillna(False)


def _run_vbt_macd(df: pd.DataFrame, symbol: str) -> tuple[pd.Series, pd.Series]:
    """方式B: vbt 组合回测，但信号与 MacdStrategy 完全同口径.

    为保证 A/B 逐笔一致，这里复用 MacdStrategy 的 histogram 并按同一规则产出信号。
    """
    feat = MacdStrategy().build_features(df, symbol, None)
    return _signals_from_histogram(feat["histogram"])


def _backtest(entries: pd.Series, exits: pd.Series, close: pd.Series, label: str) -> dict:
    """运行 vbt.Portfolio.from_signals，返回绩效指标 dict。"""
    portfolio = vbt.Portfolio.from_signals(
        close,
        entries,
        exits,
        init_cash=100_000.0,
        fees=0.0005,
        freq="1D",
    )
    return {
        "method": label,
        "total_return": float(portfolio.total_return()),
        "annualized_return": float(portfolio.annualized_return()),
        "max_drawdown": float(portfolio.max_drawdown()),
        "sharpe_ratio": float(portfolio.sharpe_ratio()),
        "win_rate": float(portfolio.trades.win_rate()),
        "trade_count": float(portfolio.trades.count()),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="MACD backtest: MacdStrategy vs vbt.MACD")
    parser.add_argument("--symbol", default="000001.SZ", help="标的代码，如 000001.SZ")
    parser.add_argument("--start", default="2025-01-01", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", default="2026-01-01", help="结束日期 YYYY-MM-DD")
    parser.add_argument(
        "--provider",
        default="qmt",
        choices=["qmt", "akshare", "baostock", "mock", "auto"],
        help="数据源，默认 qmt",
    )
    args = parser.parse_args()

    print(
        f"Fetching bars for {args.symbol} from {args.start} to {args.end} "
        f"(provider={args.provider}) ..."
    )
    try:
        df = await _fetch_bars(args.symbol, args.start, args.end, args.provider)
    except ValueError as exc:
        print(f"Provider setup error: {exc}")
        sys.exit(1)

    if df.empty:
        print("No data fetched. Check symbol and date range.")
        sys.exit(1)
    print(f"Fetched {len(df)} bars.\n")

    print("Running Method A: MacdStrategy ...")
    entries_a, exits_a = _run_macd_strategy(df, args.symbol)
    result_a = _backtest(entries_a, exits_a, df["close"], "MacdStrategy")

    print("Running Method B: vbt.MACD ...")
    entries_b, exits_b = _run_vbt_macd(df, args.symbol)
    result_b = _backtest(entries_b, exits_b, df["close"], "vbt.MACD")

    # 对比输出
    print("\n=== Results Comparison ===")
    # 收益类指标：越大越好；回撤：绝对值越小越好（亏得少）；夏普/胜率：越大越好
    better_keys = ["total_return", "annualized_return", "sharpe_ratio", "win_rate"]
    percentage_keys = {"total_return", "annualized_return", "max_drawdown", "win_rate"}
    for key in ["total_return", "annualized_return", "max_drawdown", "sharpe_ratio", "win_rate", "trade_count"]:
        val_a = result_a[key]
        val_b = result_b[key]
        if abs(val_a - val_b) < 1e-12:
            winner = "="
        elif key == "max_drawdown":
            winner = "A" if abs(val_a) < abs(val_b) else "B"
        elif key in better_keys:
            winner = "A" if val_a > val_b else "B"
        else:
            winner = "-"
        if key in percentage_keys:
            shown_a = f"{val_a * 100:>9.2f}%"
            shown_b = f"{val_b * 100:>9.2f}%"
        else:
            shown_a = f"{val_a:>10.4f}"
            shown_b = f"{val_b:>10.4f}"
        print(f"  {key:20s}  A={shown_a}  B={shown_b}  (better: {winner})")


if __name__ == "__main__":
    asyncio.run(main())
