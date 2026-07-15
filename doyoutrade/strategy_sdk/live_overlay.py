"""LiveBarOverlayHistoryFetcher — splice a live forming bar onto warehouse history.

The Strategy SDK's :class:`HistoryFetcher` returns *completed* OHLCV bars from
the local warehouse. A discipline-monitor that fires intraday (~14:50, before
the daily bar seals) needs the strategy's ``on_bar`` to see TODAY's forming
price so rules like 破5日线 / 大阴线 / 放量下跌 evaluate against the live close,
not yesterday's. This wrapper fetches the real history, then for the monitored
symbol at a daily timeframe appends (or replaces) today's forming bar built
from a realtime :class:`QuoteSnapshot`.

Design (per CLAUDE.md §错误可见性 — never let a missing live price silently
degrade into "history looks fine but is stale"):

* Only symbols present in the quote map with ``status == "ok"`` AND a usable
  ``price`` get an overlay. A missing / ``qmt_disconnected`` / ``no_data`` quote
  leaves history untouched; the caller (the deviation_monitor executor)
  separately emits a structured skip so the gap is visible, not hidden here.
* Non-daily frequencies and symbols absent from the quote map pass straight
  through — peers / index / informative data must not be mutated by the
  monitored symbol's live tick.
* The synthetic bar's index tz is aligned to the fetched frame so concatenation
  never raises on mixed tz-aware / tz-naive indexes. When the warehouse already
  carries a bar for ``as_of``'s calendar day (e.g. a re-run after close), that
  row is *replaced* by the live forming bar rather than duplicated.

This wrapper is intentionally a thin decorator: it owns no IO of its own and
delegates every fetch to the wrapped fetcher, so the runner's spans / debug
events / typed errors are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from doyoutrade.core.models import QuoteSnapshot

# Frequencies treated as "daily" for the purpose of appending a single forming
# day-bar. Intraday frequencies are passed through untouched — a point-in-time
# snapshot is not a sealed 1m/5m bar and must not masquerade as one.
_DAILY_FREQS: frozenset[str] = frozenset({"1d", "1day", "d", "day"})

_OVERLAY_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")


def build_forming_bar_row(snapshot: QuoteSnapshot) -> dict[str, float] | None:
    """Build a one-bar OHLCV row from a realtime quote, or ``None`` if unusable.

    ``price`` is the live last trade and becomes the forming bar's ``close``.
    Missing ``open`` / ``high`` / ``low`` fall back to a degenerate bar around
    the live price (so candle-body math stays well-defined); missing
    ``volume`` becomes ``0.0``. Returns ``None`` when there is no usable price
    — the caller then leaves history untouched rather than fabricating a bar.
    """
    if snapshot.status != "ok":
        return None
    close = snapshot.price
    if close is None:
        return None
    try:
        close_f = float(close)
    except (TypeError, ValueError):
        return None
    open_f = float(snapshot.open) if snapshot.open is not None else close_f
    high_f = float(snapshot.high) if snapshot.high is not None else max(open_f, close_f)
    low_f = float(snapshot.low) if snapshot.low is not None else min(open_f, close_f)
    volume_f = float(snapshot.volume) if snapshot.volume is not None else 0.0
    # Defensive: keep high/low consistent with open/close even if the upstream
    # snapshot is momentarily inconsistent (e.g. last print outside the day
    # range during a fast move). A degenerate-but-consistent bar is better than
    # one where high < close, which would break range/shadow math.
    high_f = max(high_f, open_f, close_f)
    low_f = min(low_f, open_f, close_f)
    return {
        "open": open_f,
        "high": high_f,
        "low": low_f,
        "close": close_f,
        "volume": volume_f,
    }


def _aligned_day_timestamp(index: pd.Index, as_of: datetime) -> pd.Timestamp:
    """Normalize ``as_of`` to a midnight day-stamp aligned to ``index`` tz."""
    ts = pd.Timestamp(as_of).normalize()
    idx_tz = getattr(index, "tz", None)
    if idx_tz is not None:
        ts = ts.tz_localize(idx_tz) if ts.tzinfo is None else ts.tz_convert(idx_tz)
    elif ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts


def splice_forming_bar(
    df: pd.DataFrame, as_of: datetime, row: dict[str, float]
) -> pd.DataFrame:
    """Return ``df`` with today's forming bar appended or replaced.

    If the last row already lands on ``as_of``'s calendar day it is overwritten
    (the warehouse's partial/early bar is superseded by the live one);
    otherwise the forming bar is appended and the frame re-sorted.
    """
    ts = _aligned_day_timestamp(df.index, as_of)
    out = df.copy()

    if len(out) and pd.Timestamp(out.index[-1]).normalize() == ts:
        for col, val in row.items():
            if col in out.columns:
                out.iloc[-1, out.columns.get_loc(col)] = val
        return out

    columns = list(out.columns) if len(out.columns) else list(_OVERLAY_COLUMNS)
    add = pd.DataFrame(
        [{col: row.get(col) for col in columns}],
        index=pd.Index([ts], name=out.index.name),
    )
    combined = pd.concat([out, add])
    return combined.sort_index()


@dataclass
class LiveBarOverlayHistoryFetcher:
    """Decorator over a :class:`HistoryFetcher` that splices a live forming bar.

    ``inner`` is the wrapped fetcher (typically
    :class:`doyoutrade.strategy_sdk.history_fetcher.BarsHistoryFetcher`).
    ``quotes`` maps symbol → latest :class:`QuoteSnapshot`; only symbols present
    here with an ``ok`` status are overlaid, and only at a daily ``freq``.
    """

    inner: Any
    quotes: dict[str, QuoteSnapshot]

    @property
    def data_provider(self) -> Any:
        """Expose the wrapped fetcher's provider so callers can ``aclose`` it."""
        return getattr(self.inner, "data_provider", None)

    async def fetch(
        self,
        symbol: str,
        *,
        as_of: datetime | None,
        lookback: int,
        freq: str = "1d",
    ) -> pd.DataFrame:
        df = await self.inner.fetch(
            symbol, as_of=as_of, lookback=lookback, freq=freq
        )
        if as_of is None:
            return df
        if str(freq or "1d").strip().lower() not in _DAILY_FREQS:
            return df
        snapshot = self.quotes.get(symbol)
        if snapshot is None:
            return df
        row = build_forming_bar_row(snapshot)
        if row is None:
            return df
        spliced = splice_forming_bar(df, as_of, row)
        if lookback and len(spliced) > lookback:
            return spliced.tail(lookback)
        return spliced


__all__ = [
    "LiveBarOverlayHistoryFetcher",
    "build_forming_bar_row",
    "splice_forming_bar",
]
