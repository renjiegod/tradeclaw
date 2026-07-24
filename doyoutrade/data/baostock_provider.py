"""Baostock-based data provider for A-share (沪深) quotes, history, and trading calendar.

Uses `baostock` for exchange-backed trading dates (`query_trade_dates`) and OHLCV
(`query_history_k_data_plus`). Beijing exchange (`.BJ`) symbols are not supported by
baostock's equity K-line API — :meth:`get_bars` returns ``[]`` for those codes.

Account snapshots use :class:`~doyoutrade.account.StoreBackedAccountReader` with an
in-memory mock store (same pattern as :mod:`doyoutrade.data.akshare_provider`).

Usage via factory / config::

    data:
      provider: baostock
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import baostock as bs

from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST

logger = logging.getLogger(__name__)
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_BAOSTOCK, ProviderCapabilities
from doyoutrade.core.models import Bar, MarketContext

# RLock: login helper may run while a worker method already holds the lock.
_BS_LOCK = threading.RLock()
_bs_logged_in: bool = False


def _ensure_login_locked() -> None:
    global _bs_logged_in
    r = bs.login()
    if r is None:
        raise RuntimeError("baostock login returned no result (None)")
    if r.error_code != "0":
        raise RuntimeError(f"baostock login failed: {r.error_code} {r.error_msg}")
    _bs_logged_in = True


def _raise_if_result_error(rs: Any, *, op: str, code: str) -> None:
    """Surface a baostock query failure instead of crashing on a ``None`` result.

    baostock returns ``None`` for malformed requests (most often a date that is
    not ``YYYY-MM-DD`` — the API prints ``日期格式不正确`` and yields no object)
    and a ``ResultData`` carrying ``error_code != "0"`` for backend failures.
    Both must surface as a descriptive ``RuntimeError`` so the fallback chain
    records a real ``last_error`` and the CLI envelope reports the actual cause,
    rather than the misleading ``'NoneType' object has no attribute 'error_code'``
    or a silent empty result. An ``error_code == "0"`` result with zero rows is a
    legitimate "no data" answer and is left for the caller to handle.
    """
    if rs is None:
        raise RuntimeError(
            f"baostock {op} returned no result for {code}; "
            "check request arguments (dates must be YYYY-MM-DD)"
        )
    if rs.error_code != "0":
        raise RuntimeError(
            f"baostock {op} failed for {code}: {rs.error_code} {rs.error_msg}"
        )


def _to_baostock_date(value: str) -> str:
    """Normalize an ISO-ish date/datetime string to baostock's ``YYYY-MM-DD``.

    The upstream layer passes ISO dates (``2024-12-02``), but a timestamp form
    (``2024-12-02T00:00:00``) or a compact ``YYYYMMDD`` must also resolve to the
    dashed form baostock requires — passing ``YYYYMMDD`` makes the API reject the
    request and return ``None``.
    """
    digits = value.strip().replace("-", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return value.strip()[:10]


def _ensure_bs() -> None:
    """Log in once per process (baostock recommends a single session)."""
    global _bs_logged_in
    if _bs_logged_in:
        return
    with _BS_LOCK:
        if not _bs_logged_in:
            _ensure_login_locked()


def symbol_to_baostock(symbol: str) -> Optional[str]:
    """Map ``600000.SH`` / ``000001.SZ`` to ``sh.600000`` / ``sz.000001``."""
    s = symbol.strip().upper()
    if "." not in s:
        return None
    code, suffix = s.rsplit(".", 1)
    if suffix == "SH":
        return f"sh.{code}"
    if suffix == "SZ":
        return f"sz.{code}"
    return None


# baostock adjust flag mapping: "none" = 不复权, "qfq" = 前复权, "hfq" = 后复权
#
# Official baostock docs define:
#   1 = 后复权
#   2 = 前复权
#   3 = 不复权
# A previous mapping flipped 1/2, which poisoned local qfq history with
# back-adjusted prices roughly 10x above the intended front-adjusted series.
_ADJUST_FLAG_MAP: Dict[str, str] = {
    "none": "3",  # 不复权
    "qfq": "2",   # 前复权
    "hfq": "1",   # 后复权
}


def _adjust_flag_qfq() -> str:
    """Forward adjust (前复权), aligned with akshare stack default."""
    return "2"


# baostock ``query_history_k_data_plus`` frequency codes. Keys mirror
# ``BaostockDataProvider.capabilities.supported_intervals`` exactly — there is no
# ``1m`` entry because baostock's smallest aggregate is 5 minutes (mapping it to
# ``5`` would silently return 5-minute bars labelled as 1-minute). Missing
# ``1w``/``1mo`` previously fell through to the daily default, silently
# downgrading weekly/monthly requests; both are now explicit.
_INTERVAL_FREQ_MAP: Dict[str, str] = {
    "1d": "d",
    "1w": "w",
    "weekly": "w",
    "1mo": "m",
    "monthly": "m",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "60m": "60",
}


def _freq_for_interval(interval: str) -> str:
    freq = _INTERVAL_FREQ_MAP.get(interval)
    if freq is None:
        raise ValueError(
            f"baostock does not support interval {interval!r}; "
            f"supported: {sorted(_INTERVAL_FREQ_MAP)}"
        )
    return freq


def _fields_for_interval(interval: str) -> str:
    if _freq_for_interval(interval) in {"d", "w", "m"}:
        # tradestatus 用于识别停牌日：停牌日 baostock 用前收填 OHLC、量额留空串，
        # 必须跳过而非填 0（否则会造出一根假的可成交 bar）。
        return "date,open,high,low,close,volume,amount,tradestatus"
    return "date,time,open,high,low,close,volume,amount"


def _is_blank(value: object) -> bool:
    """True for None / NaN / 空串 —— baostock·akshare 停牌日的量额就是空串。"""
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    return str(value).strip() == ""


def _minute_timestamp(date_str: str, time_raw: str) -> str:
    t = str(time_raw).strip()
    if len(t) >= 14:
        try:
            dt = datetime.strptime(t[:14], "%Y%m%d%H%M%S")
            return dt.strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    return normalize_bar_timestamp(date_str)


class BaostockHistoricalProvider:
    #: Suspension days skipped by the most recent ``get_bars`` (see
    #: ``BaostockDataProvider.last_suspended_days``).
    last_suspended_days: set[str] = set()

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> List[Bar]:
        with data_span("baostock", "get_bars"):
            bars, suspended_days = await asyncio.to_thread(
                self._sync_get_bars, symbol, start_time, end_time, interval, adjust
            )
            # Instance attribute shadows the class-level default so concurrent
            # provider instances don't share state.
            self.last_suspended_days = suspended_days
            return bars

    def _sync_get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        interval: str,
        adjust: str,
    ) -> tuple[List[Bar], set[str]]:
        """Return ``(bars, suspended_days)``.

        ``suspended_days`` are the YYYY-MM-DD dates baostock had a calendar row
        for but the symbol did not trade (tradestatus==0 / blank volume) — i.e.
        a halt, not a gap. Surfacing them (instead of only counting) lets the
        write-time continuity check subtract legitimate suspensions from the
        expected trading calendar before judging a payload discontinuous.
        """
        bs_code = symbol_to_baostock(symbol)
        if bs_code is None:
            return [], set()
        # Defense in depth: refuse index + minute before the SDK call. Without
        # this guard, baostock's minute endpoint historically raised an opaque
        # ``not enough values to unpack`` that poisoned the auto fallback
        # chain's last_error when later providers returned empty.
        from doyoutrade.data.protocols import (
            ProviderIntervalUnsupportedError,
            supports_interval_for_symbol,
        )

        if not supports_interval_for_symbol(
            BaostockDataProvider.capabilities, interval, symbol
        ):
            raise ProviderIntervalUnsupportedError(
                f"baostock does not support interval={interval!r} for {symbol!r} "
                f"(index minute bars are unavailable on this provider)"
            )
        freq = _freq_for_interval(interval)
        fields = _fields_for_interval(interval)
        start_d = _to_baostock_date(start_time)
        end_d = _to_baostock_date(end_time)
        bars: List[Bar] = []
        suspended_days: set[str] = set()
        with _BS_LOCK:
            _ensure_bs()
            adjustflag = _ADJUST_FLAG_MAP.get(adjust, "1")
            rs = bs.query_history_k_data_plus(
                bs_code,
                fields,
                start_date=start_d,
                end_date=end_d,
                frequency=freq,
                adjustflag=adjustflag,
            )
            _raise_if_result_error(rs, op="query_history_k_data_plus", code=bs_code)
            skipped_suspended = 0
            if freq in {"d", "w", "m"}:
                while rs.error_code == "0" and rs.next():
                    row = rs.get_row_data()
                    if len(row) < 7:
                        continue
                    d, o, h, low, c, vol, amt = row[:7]
                    status = row[7] if len(row) > 7 else "1"
                    # 停牌日：tradestatus==0 或成交量为空 —— 跳过，不能填 0 造出假 bar。
                    if str(status).strip() == "0" or _is_blank(vol):
                        skipped_suspended += 1
                        day_norm = normalize_bar_timestamp(str(d))[:10]
                        if day_norm:
                            suspended_days.add(day_norm)
                        continue
                    # 正常交易日核心价格为空 = 数据损坏，必须暴露而不是静默跳过。
                    if _is_blank(o) or _is_blank(h) or _is_blank(low) or _is_blank(c):
                        raise ValueError(
                            f"baostock returned blank OHLC for {bs_code} on {d}: {row!r}"
                        )
                    bars.append(
                        Bar(
                            symbol=symbol,
                            timestamp=normalize_bar_timestamp(str(d)),
                            open=float(o),
                            high=float(h),
                            low=float(low),
                            close=float(c),
                            volume=float(vol),
                            amount=_try_float(amt),
                        )
                    )
            else:
                while rs.error_code == "0" and rs.next():
                    row = rs.get_row_data()
                    if len(row) < 8:
                        continue
                    d, tm, o, h, low, c, vol, amt = row[:8]
                    # 分钟线无 tradestatus，用空量/空价识别非交易 bar 跳过。
                    if _is_blank(vol) or _is_blank(c):
                        skipped_suspended += 1
                        day_norm = normalize_bar_timestamp(str(d))[:10]
                        if day_norm:
                            suspended_days.add(day_norm)
                        continue
                    ts = _minute_timestamp(str(d), str(tm))
                    bars.append(
                        Bar(
                            symbol=symbol,
                            timestamp=ts,
                            open=float(o),
                            high=float(h),
                            low=float(low),
                            close=float(c),
                            volume=float(vol),
                            amount=_try_float(amt),
                        )
                    )
        if skipped_suspended:
            logger.info(
                "baostock skipped %d suspended bars for %s [%s, %s] (tradestatus=0 / blank volume)",
                skipped_suspended, bs_code, start_d, end_d,
            )
        return bars, suspended_days


class BaostockRealtimeProvider:
    async def fetch_latest_price(self, symbol: str) -> Optional[float]:
        try:
            return await asyncio.to_thread(self._sync_latest_close, symbol)
        except Exception as exc:
            logger.error("baostock fetch_latest_price failed for %s: %s", symbol, exc)
            return None

    def _sync_latest_close(self, symbol: str) -> Optional[float]:
        bs_code = symbol_to_baostock(symbol)
        if bs_code is None:
            return None
        end = date.today()
        start = end - timedelta(days=40)
        with _BS_LOCK:
            _ensure_bs()
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,close",
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                frequency="d",
                adjustflag=_adjust_flag_qfq(),
            )
            _raise_if_result_error(rs, op="query_history_k_data_plus", code=bs_code)
            last: Optional[float] = None
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                if len(row) >= 2:
                    last = float(row[1])
            return last


class BaostockDataProvider:
    """TradingDataProvider: baostock calendar + K-line; mock account reader in factory."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_BAOSTOCK,
        # baostock's ``query_history_k_data_plus`` covers daily / weekly /
        # monthly and the 5-/15-/30-/60-minute aggregates. It does not
        # serve 1-minute bars; ``_freq_for_interval`` previously remapped
        # ``1m`` → ``5`` which masked the gap — capabilities exclude
        # ``1m`` so the factory falls through to QMT instead.
        supported_intervals=frozenset(
            {"1d", "1w", "1mo", "weekly", "monthly", "5m", "15m", "30m", "60m"}
        ),
        # baostock's minute K-line (`frequency in {5,15,30,60}`) only covers
        # 股票/ETF — 指数 (000001.SH 上证指数 etc.) has no minute-level history
        # on this source. Requesting it anyway doesn't come back empty; the
        # SDK's response parser chokes on the shape mismatch and raises a
        # bare ``ValueError: not enough values to unpack`` with zero context.
        # Declaring the carve-out here lets callers reject up front (see
        # ``supports_interval_for_symbol``) instead of surfacing that opaque
        # error to the user.
        unsupported_index_intervals=frozenset({"5m", "15m", "30m", "60m"}),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        is_realtime_capable=False,
        # baostock's public history reaches back to 1990; round to a safe
        # advertised lookback.
        max_history_years=30,
        # ``query_trade_dates`` returns the real exchange calendar (with a
        # per-date trade flag), and the K-line feed carries ``tradestatus`` so
        # per-symbol suspensions can be told apart from data defects — the two
        # signals the write-time continuity check needs to run authoritatively.
        authoritative_calendar=True,
    )

    def __init__(self, symbols: List[str]):
        self.symbols = list(symbols)
        self._historical = BaostockHistoricalProvider()
        self._realtime = BaostockRealtimeProvider()
        # Suspension (停牌) days skipped by the most recent ``get_bars`` call,
        # as YYYY-MM-DD strings. The write-time continuity check subtracts these
        # from the expected trading calendar so a legitimate halt is not flagged
        # as a missing-day defect. Per-call scoped (overwritten each get_bars);
        # the worker drives get_bars sequentially per symbol.
        self.last_suspended_days: set[str] = set()

    async def get_market_context(self) -> MarketContext:
        with data_span("baostock", "get_market_context"):
            symbol_to_price: Dict[str, float] = {}
            symbol_to_tick: Dict[str, dict] = {}

            async def fetch_one(sym: str) -> tuple[str, float]:
                price = await self._realtime.fetch_latest_price(sym)
                return (sym, price if price is not None else 0.0)

            results = await asyncio.gather(*(fetch_one(s) for s in self.symbols))
            for sym, price in results:
                symbol_to_price[sym] = price

            _emit_market_context_event(self.symbols, symbol_to_price)
            return MarketContext(
                symbol_to_price=symbol_to_price,
                symbol_to_tick=symbol_to_tick,
            )

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> List[Bar]:
        bars = await self._historical.get_bars(
            symbol, start_time, end_time, interval=interval, adjust=adjust
        )
        # Forward the per-call suspension set so the data stack (Fallback wrapper
        # / LocalHistoricalBarsDataProvider) can read it off the served provider.
        self.last_suspended_days = set(self._historical.last_suspended_days)
        _emit_get_bars_event(symbol, start_time, end_time, interval, len(bars), adjust=adjust)
        return bars

    async def is_trading_day(self, day: str) -> bool:
        with data_span("baostock", "is_trading_day"):
            return await asyncio.to_thread(self._sync_is_trading_day, day)

    def _sync_is_trading_day(self, day: str) -> bool:
        if len(day) < 10:
            return False
        d0 = day[:10]
        with _BS_LOCK:
            _ensure_bs()
            rs = bs.query_trade_dates(start_date=d0, end_date=d0)
            _raise_if_result_error(rs, op="query_trade_dates", code=d0)
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                if len(row) >= 2 and row[0] == d0:
                    return row[1] == "1"
            return False

    async def get_trading_dates(self, start: str, end: str) -> List[str]:
        with data_span("baostock", "get_trading_dates"):
            return await asyncio.to_thread(self._sync_get_trading_dates, start, end)

    def _sync_get_trading_dates(self, start: str, end: str) -> List[str]:
        out: List[str] = []
        with _BS_LOCK:
            _ensure_bs()
            rs = bs.query_trade_dates(start_date=start[:10], end_date=end[:10])
            _raise_if_result_error(
                rs, op="query_trade_dates", code=f"{start[:10]}..{end[:10]}"
            )
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                if len(row) < 2:
                    continue
                cal, flag = row[0], row[1]
                if flag != "1":
                    continue
                d = cal[:10] if len(cal) >= 10 else cal
                if start[:10] <= d <= end[:10]:
                    out.append(d)
        return sorted(out)


def _emit_get_bars_event(
    symbol: str,
    start_time: str,
    end_time: str,
    interval: str,
    bar_count: int,
    adjust: str = DEFAULT_BAR_ADJUST,
) -> None:
    _fire_event(
        "data_provider.get_bars",
        {
            "provider": "baostock",
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
            "provider": "baostock",
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


def _try_float(value: object) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
