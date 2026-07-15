"""BarsHistoryFetcher — default :class:`HistoryFetcher` implementation.

Wraps :class:`doyoutrade.core.protocols.TradingDataProvider` so the new
:class:`StrategyRunner` can ask for tail-windows of OHLCV bars by ``freq``
per call (whereas the underlying provider's API is positional by interval).

Why this lives in ``strategy_sdk``: it implements the
:class:`HistoryFetcher` protocol declared in ``data_provider.py``. Keeping
it next to the protocol means runner wiring code reads as one import from
``doyoutrade.strategy_sdk``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd

from doyoutrade.core.protocols import TradingDataProvider
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST


_OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
_INTRADAY_FREQS: frozenset[str] = frozenset({"1m", "5m", "15m", "30m", "60m"})


def _empty_ohlcv_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=list(_OHLCV_COLUMNS))  # type: ignore[arg-type]


@dataclass
class BarsHistoryFetcher:
    """Pull a tail window of OHLCV bars for one symbol at one frequency.

    For daily bars, the window-start is computed as
    ``as_of - (lookback * 1.7 + buffer_days)`` calendar days to absorb
    weekends / holidays, then the result is tail-sliced to ``lookback``
    rows. For finer intervals callers should pass an interval-aware
    ``buffer_days`` (the default 14 days is right for daily; minutes /
    hours should be smaller, but we accept the slight over-fetch for now
    because the data layer caches across calls).

    Returning an empty DataFrame is allowed when the underlying provider
    has no data — :class:`StrategyRunner` surfaces this as a
    ``data_insufficient`` typed error instead of silently producing NaN.
    """

    data_provider: TradingDataProvider
    buffer_days: int = 14

    @staticmethod
    def _query_end_bound(*, as_of: datetime | None, freq: str, end_date: date) -> str:
        freq_key = str(freq or "1d").strip().lower() or "1d"
        if as_of is None or freq_key not in _INTRADAY_FREQS:
            return end_date.isoformat()
        if as_of.tzinfo is not None:
            as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)
        return as_of.replace(microsecond=0).isoformat()

    async def fetch(
        self,
        symbol: str,
        *,
        as_of: datetime | None,
        lookback: int,
        freq: str = "1d",
    ) -> pd.DataFrame:
        if lookback <= 0:
            return _empty_ohlcv_frame()

        if as_of is None:
            end_date = date.today()
        elif as_of.tzinfo is not None:
            end_date = as_of.astimezone(timezone.utc).date()
        else:
            end_date = as_of.date()

        span_days = int(lookback * 1.7) + self.buffer_days
        start_date = end_date - timedelta(days=span_days)
        end_bound = self._query_end_bound(as_of=as_of, freq=freq, end_date=end_date)

        bars = await self.data_provider.get_bars(
            symbol,
            start_date.isoformat(),
            end_bound,
            interval=freq,
            adjust=DEFAULT_BAR_ADJUST,
        )
        if not bars:
            return _empty_ohlcv_frame()

        rows: list[dict[str, Any]] = []
        for bar in bars:
            rows.append(
                {
                    "timestamp": pd.to_datetime(bar.timestamp),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": float(bar.volume),
                }
            )
        df = pd.DataFrame(rows).set_index("timestamp").sort_index()
        return df.tail(lookback)


__all__ = ["BarsHistoryFetcher"]
