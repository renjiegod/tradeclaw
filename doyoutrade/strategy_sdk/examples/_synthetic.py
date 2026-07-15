"""Synthetic OHLCV generator shared by the example ``__main__`` blocks.

Strategy examples in this folder are designed to run without a worker /
data provider, so each ``__main__`` calls :func:`make_ohlcv` to fabricate
a DataFrame that matches the contract :class:`Strategy` sees in
production: lowercase ``open / high / low / close / volume`` columns,
``float64`` dtype, ascending ``DatetimeIndex``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_ohlcv(
    symbol: str,
    *,
    bars: int = 200,
    start_price: float = 100.0,
    drift: float = 0.0008,
    volatility: float = 0.015,
    seed: int | None = None,
    end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Build a synthetic daily OHLCV frame with a mild log-normal random walk.

    Distinct ``seed`` values produce distinct paths, which lets the example
    scripts demonstrate symbols that disagree on the same indicator.
    """

    if bars <= 1:
        raise ValueError(f"bars must be > 1, got {bars}")

    rng = np.random.default_rng(seed if seed is not None else hash(symbol) & 0xFFFF)
    returns = rng.normal(loc=drift, scale=volatility, size=bars)
    close = start_price * np.exp(np.cumsum(returns))

    intra_vol = volatility * 0.6
    high = close * (1.0 + np.abs(rng.normal(0.0, intra_vol, size=bars)))
    low = close * (1.0 - np.abs(rng.normal(0.0, intra_vol, size=bars)))
    # First open seeds at start_price; subsequent opens follow the previous close.
    open_ = np.concatenate([[start_price], close[:-1]])
    open_ = np.clip(open_, low, high)
    volume = rng.integers(low=100_000, high=2_000_000, size=bars).astype(float)

    end_ts = pd.Timestamp("2026-01-01") if end is None else end
    index = pd.date_range(end=end_ts, periods=bars, freq="B", name="timestamp")

    df = pd.DataFrame(
        {
            "open": open_.astype("float64"),
            "high": high.astype("float64"),
            "low": low.astype("float64"),
            "close": close.astype("float64"),
            "volume": volume,
        },
        index=index,
    )
    df.attrs["symbol"] = symbol
    return df


__all__ = ["make_ohlcv"]
