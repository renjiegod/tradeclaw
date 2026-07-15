from __future__ import annotations

import logging
from datetime import date
from typing import List

from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_QMT, ProviderCapabilities
from doyoutrade.core.models import Bar, MarketContext, QuoteSnapshot

logger = logging.getLogger(__name__)


def _normalize_trading_calendar_day(value: str) -> str:
    """Coerce proxy calendar entries to YYYY-MM-DD for comparisons with ref_time."""
    s = value.strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    # qmt-proxy often returns compact YYYYMMDD; ISO bounds use YYYY-MM-DD — lex compare must match.
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8])).isoformat()
        except ValueError:
            return s
    return s


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    """Coerce a seal-volume value (whole shares) to int, or None when absent/bad.

    Booleans are rejected (a bool is not a share count); non-numeric values
    return ``None`` so a downstream consumer skips visibly rather than acting on
    a fabricated count (CLAUDE.md §错误可见性 tolerant-fallback ban).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_level_seal(tick: dict, scalar_key: str, list_key: str) -> int | None:
    """Resolve a level-1 seal volume from a tick dict.

    Prefers an already-projected scalar (``bid_vol1`` from ``_quote_data_to_tick``);
    otherwise takes ``[0]`` of the order-book list (``bid_vol``) the REST TickData
    carries. Absent → ``None`` (the snapshot field stays None and intraday
    detectors that need it skip with ``seal_vol_missing``).
    """
    scalar = tick.get(scalar_key)
    if scalar is not None:
        return _optional_int(scalar)
    seq = tick.get(list_key)
    if isinstance(seq, (list, tuple)) and seq:
        return _optional_int(seq[0])
    return None


class QmtProxyHistoricalProvider:
    """Adapter that maps qmt-proxy historical payloads into internal Bar models."""

    def __init__(self, client):
        self.client = client

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> List[Bar]:
        rows = await self.client.fetch_bars(
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
            interval=interval,
            adjust=adjust,
        )
        bars: List[Bar] = []
        for row in rows:
            ts = row.get("ts") or row.get("time") or row.get("timestamp") or ""
            bars.append(
                Bar(
                    symbol=row["symbol"],
                    timestamp=normalize_bar_timestamp(ts),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                    amount=_optional_float(row.get("amount")),
                )
            )
        return bars


class QmtLiveDataProvider:
    """Live data provider: quotes, history, and trading calendar (no account/positions)."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_QMT,
        # QMT proxy serves daily and intraday minute bars from the broker
        # account, so it's the only auto-fallback provider for minute
        # intervals once tushare/akshare/baostock are filtered out.
        supported_intervals=frozenset({"1d", "1w", "1mo", "1m", "5m", "15m", "30m", "60m"}),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=True,
        is_realtime_capable=True,
        # QMT proxy serves the real SSE/SZSE trading calendar, so it is a
        # trustworthy reference for write-time continuity checks.
        authoritative_calendar=True,
    )

    def __init__(self, client, symbols: list[str]):
        self.client = client
        self.symbols = list(symbols)
        self._historical = QmtProxyHistoricalProvider(client)
        # Per-year trading-calendar cache. ``get_trading_calendar`` is an
        # uncached upstream round-trip; without this, a backfill that runs the
        # continuity check per symbol re-fetches the same year(s) once per
        # symbol — a large-universe backtest would hammer the proxy. A double
        # in-flight fetch on a cold cache is harmless (idempotent), so no lock.
        self._trading_calendar_cache: dict[int, frozenset[str]] = {}

    async def get_market_context(self) -> MarketContext:
        with data_span("qmt", "get_market_context"):
            quotes = await self.client.fetch_latest_quotes(self.symbols)
            symbol_to_price: dict[str, float] = {}
            symbol_to_tick: dict[str, dict] = {}
            for quote in quotes:
                symbol = quote["symbol"]
                price = quote.get("price")
                if price is None:
                    price = quote.get("last")
                symbol_to_price[symbol] = float(price)
                raw = quote.get("tick")
                if isinstance(raw, dict):
                    symbol_to_tick[symbol] = raw
            return MarketContext(symbol_to_price=symbol_to_price, symbol_to_tick=symbol_to_tick)

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> List[Bar]:
        with data_span("qmt", "get_bars"):
            return await self._historical.get_bars(
                symbol, start_time, end_time, interval=interval, adjust=adjust
            )

    async def aclose(self):
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()

    async def _trading_days_for_year(self, year: int) -> frozenset[str]:
        """Normalized YYYY-MM-DD trading days for ``year``, cached per instance."""
        cached = self._trading_calendar_cache.get(year)
        if cached is not None:
            return cached
        cal = await self.client.get_trading_calendar(year)
        days = frozenset(
            _normalize_trading_calendar_day(d) for d in cal.trading_dates
        )
        self._trading_calendar_cache[year] = days
        return days

    async def is_trading_day(self, date: str) -> bool:
        with data_span("qmt", "is_trading_day"):
            key = _normalize_trading_calendar_day(date)
            if len(key) < 10:
                return False
            year = int(key[:4])
            return key in await self._trading_days_for_year(year)

    async def get_trading_dates(self, start: str, end: str) -> list[str]:
        with data_span("qmt", "get_trading_dates"):
            start_year, end_year = int(start[:4]), int(end[:4])
            dates: list[str] = []
            for y in range(start_year, end_year + 1):
                dates.extend(await self._trading_days_for_year(y))
            return sorted(d for d in dates if start <= d <= end)


def quote_snapshot_from_tick(
    symbol: str, tick: dict, *, timestamp: str | None = None
) -> QuoteSnapshot:
    """Map a qmt REST tick dict (or WS-derived dict) to a :class:`QuoteSnapshot`.

    ``tick`` is the ``model_dump`` of a qmt ``TickData`` (REST
    ``fetch_latest_quotes``) — snake_case keys ``last_price`` / ``last_close``
    / ``open`` / ``high`` / ``low`` / ``volume`` / ``amount``. ``prev_close``
    accepts either ``last_close`` (TickData, 昨收) or ``pre_close``
    (QuoteData / streamed) so the same mapper serves both surfaces.

    ``change`` / ``change_pct`` are derived only when both ``price`` and a
    positive ``prev_close`` are present (otherwise they stay ``None`` — a
    zero/negative prev_close is a schema-violating input we surface as
    "unknown" rather than dividing by it).

    **Suspended sentinel (停牌)**: an A-share last price is never ``<= 0`` (the
    floor is a few cents). qmt returns ``last_price == 0`` (alongside zero
    open/high/low/volume/amount) for a halted / no-trade-today symbol — a
    sentinel, not a real fill. Treating it as a real price yields
    ``(0 - prev_close) / prev_close = -100%`` and poisons the watchlist. Per
    CLAUDE.md §错误可见性 we make it visible instead of dividing by it: drop the
    sentinel ``price`` (``change`` / ``change_pct`` then stay ``None``) and set
    ``status="suspended"`` so the frontend renders 停牌 rather than -100%.
    ``prev_close`` and the derived limit prices are kept (still meaningful).
    Note: a pre-open tick with no fill yet is indistinguishable from a halt at
    the tick layer (both are last_price=0); distinguishing them needs the
    trading calendar and is out of this mapper's scope — "suspended" is the
    most accurate single label producible from a lone tick.
    """
    price = _optional_float(tick.get("last_price"))
    prev_close = _optional_float(tick.get("last_close"))
    if prev_close is None:
        prev_close = _optional_float(tick.get("pre_close"))
    # last_price <= 0 is the qmt halt/no-trade sentinel, not a real fill.
    # Discard it so we never derive a fake -100% move; flag it as suspended.
    suspended = price is not None and price <= 0
    if suspended:
        price = None
    status = "suspended" if suspended else "ok"
    change: float | None = None
    change_pct: float | None = None
    if price is not None and prev_close is not None:
        change = price - prev_close
        if prev_close > 0:
            change_pct = change / prev_close * 100
    # A-share limit prices derived from prev_close × the board pct (used by the
    # realtime monitoring daemon for 涨停/跌停/打开/大减 detection). Computed only
    # when prev_close is a positive number; left None otherwise (visible, never
    # fabricated). ST/*ST 5% names are not detectable from the code prefix — a
    # monitor rule may override via the preset's ``limit_pct`` param.
    limit_up_price: float | None = None
    limit_down_price: float | None = None
    if prev_close is not None and prev_close > 0:
        from doyoutrade.strategy_sdk.indicators import a_share_limit_pct

        pct = a_share_limit_pct(symbol)
        limit_up_price = round(prev_close * (1.0 + pct), 2)
        limit_down_price = round(prev_close * (1.0 - pct), 2)
    return QuoteSnapshot(
        symbol=symbol,
        price=price,
        prev_close=prev_close,
        change=change,
        change_pct=change_pct,
        open=_optional_float(tick.get("open")),
        high=_optional_float(tick.get("high")),
        low=_optional_float(tick.get("low")),
        volume=_optional_float(tick.get("volume")),
        amount=_optional_float(tick.get("amount")),
        timestamp=timestamp if timestamp is not None else tick.get("time"),
        status=status,
        bid_vol1=_first_level_seal(tick, "bid_vol1", "bid_vol"),
        ask_vol1=_first_level_seal(tick, "ask_vol1", "ask_vol"),
        limit_up_price=limit_up_price,
        limit_down_price=limit_down_price,
    )


class QmtRealtimeQuoteProvider:
    """One-shot realtime quote provider backed by qmt-proxy's REST tick API.

    Implements :class:`doyoutrade.data.protocols.RealtimeQuoteProvider`. Reuses
    the existing :class:`~doyoutrade.infra.qmt_proxy_client.QmtProxyRestClient`
    ``fetch_latest_quotes`` and maps each returned tick into a
    :class:`QuoteSnapshot`. Symbols the upstream omits come back as
    ``status="no_data"`` placeholders so no requested symbol is silently
    dropped (per CLAUDE.md §错误可见性).
    """

    def __init__(self, client) -> None:
        self.client = client

    async def fetch_quotes(self, symbols: list[str]) -> dict[str, QuoteSnapshot]:
        requested = [str(s) for s in symbols]
        with data_span("qmt", "fetch_quotes"):
            try:
                quotes = await self.client.fetch_latest_quotes(requested)
            except Exception as exc:  # noqa: BLE001 — visible, re-raised
                logger.warning(
                    "qmt_realtime_fetch_quotes_failed (%s): %s symbol_count=%d",
                    type(exc).__name__,
                    exc,
                    len(requested),
                )
                raise
        result: dict[str, QuoteSnapshot] = {}
        for quote in quotes:
            symbol = quote.get("symbol")
            if symbol is None:
                continue
            tick = quote.get("tick")
            if not isinstance(tick, dict):
                # No tick payload: surface as a no_data placeholder instead of
                # fabricating a partial snapshot from the bare price field.
                result[symbol] = QuoteSnapshot(symbol=symbol, status="no_data")
                continue
            result[symbol] = quote_snapshot_from_tick(
                symbol, tick, timestamp=quote.get("ts")
            )
        # Any requested symbol the upstream did not return gets a no_data
        # placeholder so the caller sees it explicitly (not a silent drop).
        for symbol in requested:
            if symbol not in result:
                result[symbol] = QuoteSnapshot(symbol=symbol, status="no_data")
        return result
