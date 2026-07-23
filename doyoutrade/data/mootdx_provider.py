"""mootdx-based data provider for A-share (沪深/北) OHLCV, with self-computed 复权.

``mootdx`` (a maintained fork of ``pytdx``) speaks the 通达信 protocol against
public 行情 servers. Relative to :mod:`doyoutrade.data.baostock_provider` its
edge is *minute bars* and *intraday freshness* (通达信 serves same-day bars,
baostock is T+1 close). Its cost: it has **no trading-calendar API** and its
bundled 复权 breaks on pandas>=3 (``fillna(method=)`` was removed), so this
provider:

* returns **不复权** OHLCV from ``client.bars`` and computes 前复权/后复权
  itself from the ``client.xdxr`` 除权除息 ledger (see :func:`compute_adjusted_ohlc`),
  so it honours the runtime default ``adjust="qfq"`` without depending on the
  library's broken path;
* converts 通达信 ``vol`` (单位=手, 1 手 = 100 股) to **股** so ``Bar.volume``
  matches the baostock / akshare / qmt convention (a silent 100x mismatch
  otherwise);
* approximates the trading calendar with a weekday heuristic and declares
  ``authoritative_calendar=False`` so the write-time continuity check never
  treats it as the authoritative reference (that stays baostock / qmt).

Usage via factory / config::

    data:
      provider: mootdx
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from doyoutrade.core.models import Bar, MarketContext, QuoteSnapshot
from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_MOOTDX, ProviderCapabilities

logger = logging.getLogger(__name__)

# ``mootdx``/通达信 K-line category codes accepted by ``client.bars(frequency=)``.
# 1-minute maps to 8 (通达信 "1分钟K线"); daily maps to 9. Intervals absent from
# this map are rejected up front rather than silently downgraded.
_INTERVAL_FREQ_MAP: Dict[str, int] = {
    "1m": 8,
    "5m": 0,
    "15m": 1,
    "30m": 2,
    "60m": 3,
    "1d": 9,
    "1w": 5,
    "weekly": 5,
    "1mo": 6,
    "monthly": 6,
}

# 通达信 vol 单位是 "手"; 1 手 = 100 股. Everything downstream (Bar.volume,
# order sizing, VWAP) works in 股, so convert once here.
_LOTS_TO_SHARES = 100.0

# xdxr ``category`` values that adjust the price series. category==1 is
# 除权除息 (the only one that shifts 流通股东 cost basis); the rest (股本变动,
# 增发, 回购, 权证 …) do not enter 复权 factor accumulation.
_XDXR_CATEGORY_EX_RIGHTS = 1

# Import serialization: mootdx server selection + first connect mutates a
# process-global config file; keep provider construction single-flighted.
_MOOTDX_LOCK = threading.RLock()


def symbol_to_tdx_code(symbol: str) -> Optional[str]:
    """Map ``600000.SH`` / ``000001.SZ`` / ``430047.BJ`` to the bare 通达信 code.

    mootdx infers the market from the numeric code, so only the 6-digit code is
    passed upstream. Returns ``None`` for a symbol without a recognizable
    6-digit code so :meth:`get_bars` can return ``[]`` instead of guessing.
    """
    s = symbol.strip().upper()
    code = s.split(".", 1)[0] if "." in s else s
    code = code.strip()
    if len(code) == 6 and code.isdigit():
        return code
    return None


def _freq_for_interval(interval: str) -> int:
    freq = _INTERVAL_FREQ_MAP.get(interval)
    if freq is None:
        raise ValueError(
            f"mootdx does not support interval {interval!r}; "
            f"supported: {sorted(_INTERVAL_FREQ_MAP)}"
        )
    return freq


def _to_iso_day(value: str) -> str:
    """Normalize ``YYYYMMDD`` / ``YYYY-MM-DD`` / ISO-timestamp to ``YYYY-MM-DD``."""
    digits = value.strip().replace("-", "").replace("/", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return value.strip()[:10]


# ---------------------------------------------------------------------------
# 复权 engine — pure functions (no network, no pandas) so they unit-test
# against a fixed 不复权 series + xdxr ledger and can be对拍'd with baostock qfq.
# ---------------------------------------------------------------------------


def _ex_ratio(
    prev_close: float,
    fenhong: float,
    songzhuangu: float,
    peigu: float,
    peigujia: float,
) -> Optional[float]:
    """Return the ex-date price ratio (除权日价格 / 除权前一日价格) for one event.

    通达信 除权 formula, per-10-shares 口径 (the units mootdx's xdxr columns
    use)::

        ex_price = (prev_close * 10 - 分红 + 配股价 * 配股数) / (10 + 送转股数 + 配股数)
        ratio    = ex_price / prev_close

    ``ratio < 1`` for a normal 分红/送股 (the stock "gaps down" mechanically on
    ex-date). Returns ``None`` when the denominator is non-positive or
    ``prev_close`` is unusable, so the caller skips the event visibly instead of
    dividing by zero.
    """
    if prev_close <= 0:
        return None
    denom = (10.0 + songzhuangu + peigu) * prev_close
    if denom <= 0:
        return None
    ex_price = (prev_close * 10.0 - fenhong + peigujia * peigu) / (
        10.0 + songzhuangu + peigu
    )
    if ex_price <= 0:
        return None
    return ex_price / prev_close


def compute_adjusted_ohlc(
    rows: Sequence[dict],
    events: Sequence[dict],
    adjust: str,
) -> List[dict]:
    """Apply 前复权 (``qfq``) / 后复权 (``hfq``) to an ascending 不复权 OHLC series.

    Args:
        rows: ascending-by-date list of ``{"date": "YYYY-MM-DD", "open","high",
            "low","close": float, "volume","amount": float|None}`` (不复权,
            volume already in 股).
        events: 除权除息 events, each ``{"ex_date": "YYYY-MM-DD", "fenhong",
            "songzhuangu", "peigu", "peigujia": float}`` (per-10-shares 口径,
            category==1 only — caller filters).
        adjust: ``"none"`` (identity), ``"qfq"`` (前复权, latest price is the
            anchor), or ``"hfq"`` (后复权, earliest price is the anchor).

    Returns a new list of rows with adjusted OHLC (volume/amount untouched —
    前/后复权 conventionally leave 成交量/成交额 in raw terms).

    Factor definition (multiplicative, exact):

    * ``qfq_factor(bar) = Π{ ratio_e : e.ex_date  > bar.date }`` — the most
      recent bar has no later ex-date so its factor is 1 (前复权 keeps the
      latest price identical to 不复权).
    * ``hfq_factor(bar) = Π{ 1/ratio_e : e.ex_date <= bar.date }`` — the
      earliest bar's factor is 1.

    Each ``ratio_e`` needs the 不复权 close of the last bar strictly *before*
    ``ex_date``; an event whose ex_date precedes every bar contributes to no
    factor (so a missing prev-close there is harmless). An event that lands on
    the very first bar (no earlier prev-close) is skipped with a warning +
    debug event rather than silently mis-adjusting.
    """
    if adjust == "none" or not rows:
        return [dict(r) for r in rows]

    dates = [r["date"] for r in rows]
    closes = [float(r["close"]) for r in rows]

    # Resolve each event's ratio using the raw prev-close.
    resolved: List[tuple[str, float]] = []  # (ex_date, ratio)
    for ev in events:
        ex_date = ev["ex_date"]
        # last bar strictly before ex_date
        pc: Optional[float] = None
        for i in range(len(dates) - 1, -1, -1):
            if dates[i] < ex_date:
                pc = closes[i]
                break
        if pc is None:
            # ex_date <= earliest bar. If it is strictly earlier than every
            # bar it affects no bar's factor (all bar.date >= ex_date), so
            # skipping is correct. If it equals the first bar's date we cannot
            # anchor it — surface that rather than mis-adjust.
            if ex_date in dates:
                logger.warning(
                    "mootdx qfq: ex-date %s lands on the first available bar; "
                    "cannot resolve prev-close, skipping this 除权 event "
                    "(widen the fetch window to include the prior trading day)",
                    ex_date,
                )
                _fire_event(
                    "mootdx_ex_right_unanchored",
                    {
                        "provider": PROVIDER_NAME_MOOTDX,
                        "ex_date": ex_date,
                        "reason": "prev_close_missing",
                        "hint": "extend fetch window before start_time so the "
                        "trading day before each ex-date is present",
                    },
                )
            continue
        ratio = _ex_ratio(
            pc,
            float(ev.get("fenhong") or 0.0),
            float(ev.get("songzhuangu") or 0.0),
            float(ev.get("peigu") or 0.0),
            float(ev.get("peigujia") or 0.0),
        )
        if ratio is None or ratio <= 0:
            logger.warning(
                "mootdx qfq: unusable 除权 ratio for ex-date %s (prev_close=%s); skipping",
                ex_date, pc,
            )
            continue
        resolved.append((ex_date, ratio))

    resolved.sort(key=lambda t: t[0])

    out: List[dict] = []
    for r in rows:
        d = r["date"]
        factor = 1.0
        if adjust == "qfq":
            for ex_date, ratio in resolved:
                if ex_date > d:
                    factor *= ratio
        elif adjust == "hfq":
            for ex_date, ratio in resolved:
                if ex_date <= d:
                    factor /= ratio
        else:
            raise ValueError(f"unknown adjust {adjust!r}; expected none/qfq/hfq")
        nr = dict(r)
        nr["open"] = float(r["open"]) * factor
        nr["high"] = float(r["high"]) * factor
        nr["low"] = float(r["low"]) * factor
        nr["close"] = float(r["close"]) * factor
        out.append(nr)
    return out


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class MootdxDataProvider:
    """TradingDataProvider: mootdx (通达信) OHLCV + self-computed 复权.

    Trading calendar is a weekday approximation (通达信 exposes no calendar
    API); ``authoritative_calendar=False`` keeps the continuity check from
    treating it as the reference source.
    """

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_MOOTDX,
        supported_intervals=frozenset(
            {"1d", "1w", "1mo", "weekly", "monthly", "1m", "5m", "15m", "30m", "60m"}
        ),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        # 通达信 serves same-day bars intraday.
        is_realtime_capable=True,
        # 通达信 history depth varies by server; leave unbounded/unknown.
        max_history_years=None,
        # Weekday-heuristic calendar — NOT authoritative (would manufacture
        # false gaps around 国庆/春节). baostock / qmt remain the reference.
        authoritative_calendar=False,
    )

    def __init__(self, symbols: List[str], *, client: Any | None = None):
        self.symbols = list(symbols)
        self._client = client
        self.last_suspended_days: set[str] = set()

    # -- client bootstrap ---------------------------------------------------

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with _MOOTDX_LOCK:
            if self._client is None:
                self._client = _make_std_client()
            return self._client

    # -- history ------------------------------------------------------------

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> List[Bar]:
        with data_span("mootdx", "get_bars"):
            bars = await asyncio.to_thread(
                self._sync_get_bars, symbol, start_time, end_time, interval, adjust
            )
            _emit_get_bars_event(symbol, start_time, end_time, interval, len(bars), adjust)
            return bars

    def _sync_get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        interval: str,
        adjust: str,
    ) -> List[Bar]:
        code = symbol_to_tdx_code(symbol)
        if code is None:
            return []
        freq = _freq_for_interval(interval)
        start_d = _to_iso_day(start_time)
        end_d = _to_iso_day(end_time)

        raw_rows = self._fetch_raw_rows(code, freq, start_d)
        if not raw_rows:
            return []

        # Pull xdxr once and compute 复权 over the *full* fetched window (so the
        # prev-close anchoring the earliest in-range ex-date is present), then
        # clip to [start, end].
        if adjust != "none":
            events = self._fetch_ex_rights(code)
            adjusted = compute_adjusted_ohlc(raw_rows, events, adjust)
        else:
            adjusted = raw_rows

        bars: List[Bar] = []
        for r in adjusted:
            d = r["date"]
            if d < start_d or d > end_d:
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=r["timestamp"],
                    open=float(r["open"]),
                    high=float(r["high"]),
                    low=float(r["low"]),
                    close=float(r["close"]),
                    volume=float(r["volume"]),
                    amount=r.get("amount"),
                    adjust_type=adjust,
                )
            )
        return bars

    def _fetch_raw_rows(self, code: str, freq: int, start_d: str) -> List[dict]:
        """Fetch 不复权 rows covering [start_d - buffer, latest], ascending.

        mootdx ``bars`` takes a bar *count* (offset), not a date range, so we
        pull an estimated count, and if the earliest row is still after
        ``start_d`` we grow the count and retry (bounded) rather than silently
        returning a short window that would misplace the qfq anchor.
        """
        client = self._get_client()
        offset = self._estimate_offset(freq, start_d)
        max_offset = 8000
        rows: List[dict] = []
        while True:
            df = client.bars(symbol=code, frequency=freq, offset=offset)
            rows = self._normalize_df(df)
            if not rows:
                return []
            earliest = rows[0]["date"]
            # Enough history (covers start), or we hit the server's depth
            # (returned fewer than asked), or the safety cap — stop.
            if earliest <= start_d or len(rows) < offset or offset >= max_offset:
                break
            offset = min(offset * 2, max_offset)
        return rows

    @staticmethod
    def _estimate_offset(freq: int, start_d: str) -> int:
        """Rough bar-count estimate from today back to ``start_d`` + buffer."""
        try:
            start = datetime.strptime(start_d, "%Y-%m-%d").date()
        except ValueError:
            return 800
        span_days = max((date.today() - start).days, 1)
        if freq in (9, 4):          # daily
            per_day = 0.7            # ~0.7 trading days per calendar day
        elif freq == 5:             # weekly
            per_day = 0.7 / 5
        elif freq == 6:             # monthly
            per_day = 0.7 / 21
        else:                        # minute frequencies — bounded, most recent
            return 800
        est = int(span_days * per_day) + 40  # +40 buffer for qfq prev-close anchor
        return max(60, min(est, 8000))

    @staticmethod
    def _normalize_df(df: Any) -> List[dict]:
        """Convert a mootdx bars DataFrame to ascending list-of-dict rows.

        Handles the vol(手)→volume(股) conversion and derives an ISO date +
        normalized timestamp. Rows with a blank/zero OHLC core are dropped as
        non-trading placeholders (never turned into a fake tradable bar).
        """
        if df is None or len(df) == 0:
            return []
        records = df.to_dict("records")
        rows: List[dict] = []
        for rec in records:
            dt_raw = str(rec.get("datetime") or "").strip()
            day = _to_iso_day(dt_raw) if dt_raw else ""
            if not day:
                continue
            close = rec.get("close")
            open_ = rec.get("open")
            if _is_blank(close) or _is_blank(open_):
                continue
            vol_lots = rec.get("vol")
            if _is_blank(vol_lots):
                vol_lots = rec.get("volume")
            volume_shares = (float(vol_lots) if not _is_blank(vol_lots) else 0.0) * _LOTS_TO_SHARES
            rows.append(
                {
                    "date": day,
                    "timestamp": normalize_bar_timestamp(dt_raw) if dt_raw else normalize_bar_timestamp(day),
                    "open": float(open_),
                    "high": float(rec.get("high")),
                    "low": float(rec.get("low")),
                    "close": float(close),
                    "volume": volume_shares,
                    "amount": _try_float(rec.get("amount")),
                }
            )
        rows.sort(key=lambda r: r["date"])
        return rows

    def _fetch_ex_rights(self, code: str) -> List[dict]:
        """Fetch category==1 除权除息 events as ascending ``{ex_date, …}`` dicts."""
        client = self._get_client()
        df = client.xdxr(symbol=code)
        if df is None or len(df) == 0:
            return []
        events: List[dict] = []
        for rec in df.to_dict("records"):
            if int(rec.get("category") or 0) != _XDXR_CATEGORY_EX_RIGHTS:
                continue
            try:
                ex_date = f"{int(rec['year']):04d}-{int(rec['month']):02d}-{int(rec['day']):02d}"
            except (KeyError, TypeError, ValueError):
                continue
            events.append(
                {
                    "ex_date": ex_date,
                    "fenhong": float(rec.get("fenhong") or 0.0),
                    "songzhuangu": float(rec.get("songzhuangu") or 0.0),
                    "peigu": float(rec.get("peigu") or 0.0),
                    "peigujia": float(rec.get("peigujia") or 0.0),
                }
            )
        events.sort(key=lambda e: e["ex_date"])
        return events

    # -- market context -----------------------------------------------------

    async def get_market_context(self) -> MarketContext:
        with data_span("mootdx", "get_market_context"):
            symbol_to_price: Dict[str, float] = {}

            async def fetch_one(sym: str) -> tuple[str, float]:
                try:
                    price = await asyncio.to_thread(self._sync_latest_close, sym)
                except Exception as exc:  # noqa: BLE001 — one symbol must not sink the cycle
                    logger.warning(
                        "mootdx get_market_context: latest price failed for %s (%s: %s)",
                        sym, type(exc).__name__, exc,
                    )
                    price = 0.0
                return (sym, price if price is not None else 0.0)

            results = await asyncio.gather(*(fetch_one(s) for s in self.symbols))
            for sym, price in results:
                symbol_to_price[sym] = price
            _emit_market_context_event(self.symbols, symbol_to_price)
            return MarketContext(symbol_to_price=symbol_to_price, symbol_to_tick={})

    def _sync_latest_close(self, symbol: str) -> Optional[float]:
        code = symbol_to_tdx_code(symbol)
        if code is None:
            return None
        client = self._get_client()
        df = client.bars(symbol=code, frequency=9, offset=1)
        rows = self._normalize_df(df)
        return rows[-1]["close"] if rows else None

    # -- calendar (weekday approximation; NOT authoritative) ----------------

    async def is_trading_day(self, day: str) -> bool:
        with data_span("mootdx", "is_trading_day"):
            return _is_weekday(day)

    async def get_trading_dates(self, start: str, end: str) -> List[str]:
        with data_span("mootdx", "get_trading_dates"):
            return _weekday_range(start[:10], end[:10])


# ---------------------------------------------------------------------------
# Realtime quotes (L1 + 5档 snapshot; polling, NO WebSocket push)
# ---------------------------------------------------------------------------


class MootdxRealtimeQuoteProvider:
    """RealtimeQuoteProvider over mootdx ``quotes()`` — a one-shot L1 snapshot.

    通达信 has no server push, so this is **polling only**: it backs the
    ``QuoteStreamService`` snapshot path (``fetch_once`` + the slow background
    poll) with ``ws_subscribe=None``. It fills every requested symbol — a code
    the upstream omits comes back as a ``status="no_data"`` placeholder (never
    silently dropped). ``vol`` (手) is converted to 股 to match the OHLCV
    provider's convention. Order-book 封单量 / limit prices stay ``None``: the
    mootdx snapshot carries generic L1 bid/ask, not the limit-price-anchored
    seal volumes ``QuoteSnapshot.bid_vol1`` specifically means.
    """

    def __init__(self, *, client: Any | None = None):
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with _MOOTDX_LOCK:
            if self._client is None:
                self._client = _make_std_client()
            return self._client

    async def fetch_quotes(self, symbols: list[str]) -> Dict[str, QuoteSnapshot]:
        with data_span("mootdx", "fetch_quotes"):
            return await asyncio.to_thread(self._sync_fetch_quotes, symbols)

    def _sync_fetch_quotes(self, symbols: list[str]) -> Dict[str, QuoteSnapshot]:
        # Placeholder for every requested symbol so callers can tell "unknown /
        # unsupported" apart from "provider down" (per RealtimeQuoteProvider).
        result: Dict[str, QuoteSnapshot] = {
            s: QuoteSnapshot(symbol=s, status="no_data") for s in symbols
        }
        code_to_symbol: Dict[str, str] = {}
        for s in symbols:
            code = symbol_to_tdx_code(s)
            if code:
                code_to_symbol[code] = s
        if not code_to_symbol:
            return result
        try:
            client = self._get_client()
            df = client.quotes(symbol=list(code_to_symbol))
        except Exception as exc:  # noqa: BLE001 — degrade visibly, never sink REST
            logger.warning(
                "mootdx fetch_quotes failed (%s): %s; returning no_data placeholders",
                type(exc).__name__, exc,
            )
            _fire_event(
                "mootdx_realtime_fetch_failed",
                {
                    "provider": PROVIDER_NAME_MOOTDX,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "symbol_count": len(code_to_symbol),
                    "hint": "mootdx quotes() failed or mootdx not installed; "
                    "watchlist renders — for these symbols until it recovers",
                },
            )
            return result
        if df is None or len(df) == 0:
            return result
        for rec in df.to_dict("records"):
            code = str(rec.get("code") or "").strip()
            sym = code_to_symbol.get(code)
            if sym is None:
                continue
            result[sym] = _quote_from_mootdx_rec(sym, rec)
        return result


def _quote_from_mootdx_rec(symbol: str, rec: dict) -> QuoteSnapshot:
    price = _try_float(rec.get("price"))
    prev_close = _try_float(rec.get("last_close"))
    change = change_pct = None
    if price is not None and prev_close is not None and prev_close > 0:
        change = price - prev_close
        change_pct = change / prev_close * 100.0
    vol_lots = _try_float(rec.get("vol"))
    volume = vol_lots * _LOTS_TO_SHARES if vol_lots is not None else None
    ts = str(rec.get("servertime") or "").strip() or None
    return QuoteSnapshot(
        symbol=symbol,
        price=price,
        prev_close=prev_close,
        change=change,
        change_pct=change_pct,
        open=_try_float(rec.get("open")),
        high=_try_float(rec.get("high")),
        low=_try_float(rec.get("low")),
        volume=volume,
        amount=_try_float(rec.get("amount")),
        timestamp=ts,
        status="ok",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_std_client() -> Any:
    """Build a mootdx std-market Quotes client, or raise a clear install hint."""
    try:
        from mootdx import config
        from mootdx.consts import HQ_HOSTS
        from mootdx.quotes import Quotes
    except Exception as exc:  # pragma: no cover - import guard
        raise RuntimeError(
            "mootdx is not installed; install the extra "
            "(`uv sync --extra mootdx` or `pip install 'doyoutrade[mootdx]'`). "
            "Its httpx<0.26 / older-tenacity pins are relaxed by "
            "[tool.uv] override-dependencies in pyproject.toml (verified to work "
            "on the project's httpx 0.28 + tenacity 9). Or use another data "
            "provider (baostock / akshare)."
        ) from exc
    errors: list[str] = []

    try:
        client = _build_default_std_client(Quotes)
        if _probe_std_client(client):
            return client
        errors.append("default discovery returned no bars for probe symbol")
        logger.warning(
            "mootdx std bootstrap: default discovery returned no bars; "
            "falling back to explicit HQ hosts"
        )
    except Exception as exc:
        msg = f"default discovery failed ({type(exc).__name__}: {exc})"
        errors.append(msg)
        logger.warning(
            "mootdx std bootstrap: %s; falling back to explicit HQ hosts",
            msg,
        )

    for server in _candidate_std_servers(
        bestip=_safe_config_get(config, "BESTIP"),
        configured=_safe_config_get(config, "SERVER"),
        builtin=HQ_HOSTS,
    ):
        try:
            client = _build_std_client_with_server(Quotes, server)
        except Exception as exc:
            errors.append(f"{server[0]}:{server[1]} build failed ({type(exc).__name__}: {exc})")
            logger.warning(
                "mootdx std bootstrap: server %s:%s build failed (%s: %s)",
                server[0],
                server[1],
                type(exc).__name__,
                exc,
            )
            continue
        if _probe_std_client(client):
            logger.info("mootdx std bootstrap: connected via explicit server %s:%s", server[0], server[1])
            return client
        errors.append(f"{server[0]}:{server[1]} probe returned no bars")
        logger.warning(
            "mootdx std bootstrap: server %s:%s probe returned no bars",
            server[0],
            server[1],
        )

    detail = "; ".join(errors[-6:]) if errors else "no candidates"
    raise RuntimeError(
        "mootdx std quotes bootstrap failed; all explicit HQ hosts were unusable. "
        f"Recent errors: {detail}"
    )


def _build_default_std_client(Quotes: Any) -> Any:
    return Quotes.factory(market="std", timeout=5)


def _build_std_client_with_server(Quotes: Any, server: tuple[str, int]) -> Any:
    return Quotes.factory(market="std", server=server, timeout=5, bestip=False)


def _probe_std_client(client: Any) -> bool:
    try:
        df = client.bars(symbol="000001", frequency=9, offset=1)
    except Exception as exc:
        logger.warning(
            "mootdx std bootstrap probe failed (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return False
    return df is not None and len(df) > 0


def _safe_config_get(config_module: Any, key: str) -> Any:
    try:
        return config_module.get(key)
    except Exception:
        return None


def _candidate_std_servers(
    *,
    bestip: Any,
    configured: Any,
    builtin: Sequence[Any],
) -> list[tuple[str, int]]:
    seen: set[tuple[str, int]] = set()
    out: list[tuple[str, int]] = []

    def add(raw: Any) -> None:
        server = _normalize_server_candidate(raw)
        if server is None or server in seen:
            return
        seen.add(server)
        out.append(server)

    if isinstance(bestip, dict):
        add(bestip.get("HQ"))
    elif bestip is not None:
        add(bestip)

    if isinstance(configured, dict):
        hq = configured.get("HQ")
        if isinstance(hq, Sequence):
            for raw in hq:
                add(raw)

    for raw in builtin:
        add(raw)
    return out


def _normalize_server_candidate(raw: Any) -> Optional[tuple[str, int]]:
    if not isinstance(raw, (list, tuple)):
        return None
    host: Any = None
    port: Any = None
    if len(raw) >= 2 and isinstance(raw[0], str) and raw[0].count(".") >= 1:
        host, port = raw[0], raw[1]
    elif len(raw) >= 3:
        host, port = raw[1], raw[2]
    if not isinstance(host, str) or not host.strip():
        return None
    try:
        port_int = int(port)
    except (TypeError, ValueError):
        return None
    if port_int <= 0:
        return None
    return (host.strip(), port_int)


def _is_weekday(day: str) -> bool:
    d0 = _to_iso_day(day)
    try:
        return datetime.strptime(d0, "%Y-%m-%d").weekday() < 5
    except ValueError:
        return False


def _weekday_range(start: str, end: str) -> List[str]:
    try:
        s = datetime.strptime(_to_iso_day(start), "%Y-%m-%d").date()
        e = datetime.strptime(_to_iso_day(end), "%Y-%m-%d").date()
    except ValueError:
        return []
    out: List[str] = []
    cur = s
    while cur <= e:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    return str(value).strip() == ""


def _try_float(value: object) -> Optional[float]:
    try:
        if _is_blank(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _emit_get_bars_event(
    symbol: str, start_time: str, end_time: str, interval: str, bar_count: int, adjust: str
) -> None:
    _fire_event(
        "data_provider.get_bars",
        {
            "provider": PROVIDER_NAME_MOOTDX,
            "method": "get_bars",
            "symbol": symbol,
            "start_time": start_time,
            "end_time": end_time,
            "interval": interval,
            "bar_count": bar_count,
            "adjust": adjust,
        },
    )


def _emit_market_context_event(symbols: List[str], prices: Dict[str, float]) -> None:
    _fire_event(
        "data_provider.get_market_context",
        {
            "provider": PROVIDER_NAME_MOOTDX,
            "method": "get_market_context",
            "symbols": symbols,
            "prices": prices,
        },
    )


def _fire_event(event_name: str, payload: dict) -> None:
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        pass
