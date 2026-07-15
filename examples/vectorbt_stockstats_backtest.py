#!/usr/bin/env python3
"""Minimal vectorbt backtest where signals come from **stockstats** indicators.

Uses synthetic OHLCV (no network) so the script is reproducible. The same stockstats
column names apply when you build a ``pandas.DataFrame`` from real bars.

Install the optional extra (from repo root)::

    uv sync --extra examples

Run::

    uv run --extra examples python examples/vectorbt_stockstats_backtest.py
"""

from __future__ import annotations  # 允许在类型注解里先用尚未定义的类名（如本文件内的类型）

import numpy as np  # 数值计算、随机数
import pandas as pd  # DataFrame、时间索引
import vectorbt as vbt  # 向量化回测库（本例用 Portfolio.from_signals）
from stockstats import wrap  # 把 OHLCV DataFrame 包成可算技术指标的对象


def _synthetic_ohlcv(n: int = 600, seed: int = 42) -> pd.DataFrame:
    """生成 n 根「工作日」假 K 线，固定 seed 保证每次运行结果一致。"""
    rng = np.random.default_rng(seed)  # 可复现的随机数生成器（比 np.random.seed 更清晰）
    rets = rng.normal(0.0002, 0.015, size=n)  # 对数收益的近似：微小正漂移 + 波动
    close = 100.0 * np.exp(np.cumsum(rets))  # 从 100 元起，用累乘指数还原出收盘价序列
    noise = rng.normal(0.0, 0.002, size=n)  # 给开盘价相对昨收加一点小扰动
    open_ = np.roll(close, 1)  # 用「上一根收盘价」当初步开盘价（第一根稍后修正）
    open_[0] = close[0]  # 第一根没有前一日，开盘价等于收盘价避免 roll 带来的错位
    open_ *= 1.0 + noise  # 在开盘价上叠乘噪声，让开收不完全相同
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, 0.003, size=n)))  # 最高价不低于开/收，再略抬高
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, 0.003, size=n)))  # 最低价不高于开/收，再略压低
    vol = rng.integers(1_000_000, 5_000_000, size=n, dtype=np.int64)  # 随机整数成交量
    idx = pd.date_range("2020-01-01", periods=n, freq="B", tz="UTC")  # 工作日日历索引（B=business day）
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol.astype(float)},
        index=idx,  # stockstats / vectorbt 都依赖「有序时间索引」语义
    )


def main() -> None:
    ohlcv = _synthetic_ohlcv()  # 拿到 OHLCV DataFrame（列名需符合 stockstats 约定）
    sdf = wrap(ohlcv.copy())  # copy 避免 wrap 在原表上就地改列；sdf 可按列名懒算指标
    # 下面列名与平台 data_bars 的 indicators、stockstats 文档一致（如 rsi_6、kdjk 等）
    macd = sdf["macd"]  # DIF：快慢 EMA 差，访问时 stockstats 会算出整列
    macds = sdf["macds"]  # DEA：DIF 的 EMA，即信号线

    # 金叉：本根 DIF 上穿信号线（本根 macd>macds 且上一根 macd<=macds）→ 做多入场
    entries = (macd > macds) & (macd.shift(1) <= macds.shift(1))
    # 死叉：本根 DIF 下穿信号线 → 平仓/出场
    exits = (macd < macds) & (macd.shift(1) >= macds.shift(1))

    portfolio = vbt.Portfolio.from_signals(
        ohlcv["close"],  # 用收盘价序列作为成交计价基础
        entries,  # 布尔 Series：True 表示在该时刻尝试买入（入场）
        exits,  # 布尔 Series：True 表示在该时刻尝试卖出（出场）
        init_cash=100_000.0,  # 初始资金
        fees=0.0005,  # 单边手续费比例（0.05%），买卖都会按规则扣费
        freq="1D",  # 日频：影响年化类指标与持仓时间统计
    )

    print("=== Last rows (OHLCV + MACD from stockstats) ===")
    tail = pd.DataFrame(
        {
            "close": ohlcv["close"],  # 收盘价
            "macd": macd,  # DIF
            "macds": macds,  # DEA
            "macdh": sdf["macdh"],  # 柱状线 = 2*(DIF-DEA)，与技能文档里的「能量柱」一致
        }
    ).tail(5)  # 只看最后 5 行便于肉眼检查
    print(tail.to_string(), end="\n\n")  # 打印成对齐文本表

    print("=== Portfolio summary (vectorbt) ===")
    print(portfolio.stats())  # 汇总收益、回撤、胜率、Sharpe 等向量回测统计


if __name__ == "__main__":  # 仅在被 `python 本文件` 直接运行时执行，被 import 时不跑
    main()  # 脚本入口：构造数据 → 指标 → 信号 → 回测 → 打印
