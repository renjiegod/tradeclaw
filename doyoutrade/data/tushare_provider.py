"""Tushare Pro-API data provider for A-share OHLCV.

Tushare's ``pro_bar`` endpoint covers daily / weekly / monthly bars plus
1-/5-/15-/30-/60-minute bars for **stocks / ETF**, with forward (qfq) /
backward (hfq) adjustment. Minute frequencies are gated behind a paid credit
tier — when the configured token lacks the entitlement Tushare raises, which
:meth:`_sync_get_bars` surfaces as a ``RuntimeError`` (visible in the CLI
envelope and recorded as the fallback chain's ``last_error``) rather than a
silent empty result; the factory's auto-chain then falls through to the
next provider (baostock / QMT).

**Indices** (``000xxx.SH`` / ``399xxx.SZ``) do not share the stock path:

* Minute bars must go through ``idx_mins`` — ``pro_bar`` routes to
  ``stk_mins``, which rejects 指数 codes (and many trial / proxy tokens lack
  ``stk_mins`` entirely even when ``idx_mins`` works).
* Daily / weekly / monthly bars use ``pro_bar(..., asset='I', adj=None)``.
  The default stock asset looks up ``adj_factor`` and fails on index codes.

Token / URL resolution lives in :class:`doyoutrade.config.TushareSettings`
(YAML ``data.tushare.token``/``url`` or ``TUSHARE_TOKEN``/``TUSHARE_URL``
env). The provider sets the token on every call (Tushare's SDK keeps it on
a module global) but skips re-creating the pro_api handle when one already
exists for the process. When a custom ``url`` is configured, the handle's
private ``_DataApi__http_url`` is overridden to point at it instead of
Tushare's official gateway; that handle is then passed explicitly into
``tushare.pro_bar(api=...)`` since ``pro_bar`` otherwise builds its own
default-gateway handle internally.

Endpoints not modeled here (financial indicators, money flow, lhb)
live behind their own ``data tushare ...`` CLI subcommands — see the
strategy authoring docs. Forcing those through the generic
``HistoricalDataProvider`` shape would erase the rich per-endpoint
schema Tushare exposes.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Dict, List, Optional

from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrument_catalog.a_share_equity import is_cn_a_share_index_symbol
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_TUSHARE, ProviderCapabilities
from doyoutrade.core.models import Bar, MarketContext

logger = logging.getLogger(__name__)


# Map our canonical interval names to Tushare ``pro_bar`` ``freq`` values.
# Minute freqs use Tushare's ``<n>min`` spelling; they require a paid credit
# tier (the provider surfaces the entitlement error rather than swallowing it).
_INTERVAL_FREQ_MAP: Dict[str, str] = {
    "1d": "D",
    "1w": "W",
    "weekly": "W",
    "1mo": "M",
    "monthly": "M",
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "60m": "60min",
}

_ADJUST_MAP: Dict[str, Optional[str]] = {
    "none": None,
    "qfq": "qfq",
    "hfq": "hfq",
}


def _freq_for_interval(interval: str) -> str:
    freq = _INTERVAL_FREQ_MAP.get(interval)
    if freq is None:
        raise ValueError(
            f"tushare does not support interval {interval!r}; "
            f"supported: {sorted(_INTERVAL_FREQ_MAP)}"
        )
    return freq


def _is_minute_freq(freq: str) -> bool:
    return freq.endswith("min")


def _compact_date(value: str) -> str:
    """Daily/weekly/monthly ``start_date``/``end_date`` form: ``YYYYMMDD``."""
    return value.strip().replace("-", "")[:8]


def _minute_datetime(value: str, *, end: bool) -> str:
    """Minute ``start_date``/``end_date`` form: ``YYYY-MM-DD HH:MM:SS``.

    Tushare's minute endpoint wants a full datetime; widen a bare date to the
    A-share session bounds (09:00:00 .. 15:00:00) so a day's bars are included.
    """
    digits = value.strip().replace("-", "")[:8]
    if len(digits) == 8 and digits.isdigit():
        day = f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    else:
        day = value.strip()[:10]
    return f"{day} 15:00:00" if end else f"{day} 09:00:00"


def _try_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TushareDataProvider:
    """``HistoricalDataProvider`` backed by Tushare Pro OHLCV endpoints."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_TUSHARE,
        # Daily / weekly / monthly plus 1-/5-/15-/30-/60-minute. Minute
        # frequencies require a paid Tushare credit tier; when the token
        # lacks it ``pro_bar`` / ``idx_mins`` raises and ``_sync_get_bars``
        # surfaces the error (visible) so the auto-chain falls through to
        # the next provider. ``weekly`` / ``monthly`` aliases mirror akshare /
        # baostock.
        supported_intervals=frozenset(
            {"1d", "1w", "1mo", "weekly", "monthly", "1m", "5m", "15m", "30m", "60m"}
        ),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=True,
        is_realtime_capable=False,
        max_history_years=20,
    )

    def __init__(self, symbols: List[str], *, token: str, url: Optional[str] = None):
        if not token or not str(token).strip():
            raise ValueError(
                "TushareDataProvider requires a non-empty token; "
                "set data.tushare.token or export TUSHARE_TOKEN."
            )
        self.symbols = list(symbols)
        self._token = str(token)
        self._url = str(url).strip() or None if url else None
        self._pro_api: Any = None

    def _ensure_pro_api(self) -> None:
        """Lazily initialise the Tushare Pro handle. Idempotent per process."""
        if self._pro_api is not None:
            return
        try:
            import tushare as ts  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "tushare is not installed; add it to the environment "
                "(uv add tushare) to use data_source='tushare'"
            ) from exc

        ts.set_token(self._token)
        pro = ts.pro_api()
        # Mirror Tushare's documented non-official-gateway pattern: set the
        # private attrs directly on the handle rather than via a public API
        # (Tushare exposes none for a custom base URL).
        pro._DataApi__token = self._token
        if self._url:
            pro._DataApi__http_url = self._url
        self._pro_api = pro

    async def get_market_context(self) -> MarketContext:
        with data_span("tushare", "get_market_context"):
            # Tushare has no free real-time push for the broker tier we
            # target — return an empty context. Auto-chain dispatch
            # prefers QMT for live data, so this path is only reached
            # when an operator explicitly selects ``--data-source tushare``
            # for a live cycle, in which case empty quotes is the
            # honest answer rather than a fabricated last-close.
            return MarketContext(symbol_to_price={}, symbol_to_tick={})

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> List[Bar]:
        with data_span("tushare", "get_bars"):
            return await asyncio.to_thread(
                self._sync_get_bars, symbol, start_time, end_time, interval, adjust
            )

    def _fetch_idx_mins(
        self, symbol: str, freq: str, start_d: str, end_d: str
    ) -> Any:
        """Fetch index minute bars via ``idx_mins`` (not ``pro_bar``/``stk_mins``)."""
        try:
            return self._pro_api.query(
                "idx_mins",
                ts_code=symbol,
                freq=freq,
                start_date=start_d,
                end_date=end_d,
            )
        except Exception as exc:
            raise RuntimeError(
                f"tushare idx_mins failed for {symbol} "
                f"[{start_d}..{end_d}] freq={freq}: {exc}"
            ) from exc

    def _fetch_pro_bar(
        self,
        symbol: str,
        *,
        start_d: str,
        end_d: str,
        freq: str,
        adjust: str,
        is_index: bool,
    ) -> Any:
        import tushare as ts  # type: ignore[import-untyped]

        # Indices: asset='I' and never request adj (no adj_factor series).
        # Stocks / ETF: default asset + caller-selected adjust mode.
        kwargs: Dict[str, Any] = {
            "ts_code": symbol,
            "api": self._pro_api,
            "start_date": start_d,
            "end_date": end_d,
            "freq": freq,
        }
        if is_index:
            kwargs["asset"] = "I"
            kwargs["adj"] = None
        else:
            kwargs["adj"] = _ADJUST_MAP.get(adjust)

        try:
            # ``api=self._pro_api`` is required, not optional: left as the
            # default ``None``, ``pro_bar`` builds its own handle pointed at
            # Tushare's official gateway, silently ignoring any custom
            # ``data.tushare.url``.
            return ts.pro_bar(**kwargs)
        except Exception as exc:
            # A real failure (bad token, rate limit, missing minute-credit
            # entitlement). Surface it — the fallback wrapper records it as
            # ``last_error`` and the next provider takes over. Swallowing it
            # to ``[]`` would hide the cause behind a fake "no data" result.
            raise RuntimeError(
                f"tushare pro_bar failed for {symbol} "
                f"[{start_d}..{end_d}] freq={freq}: {exc}"
            ) from exc

    def _sync_get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        interval: str,
        adjust: str,
    ) -> List[Bar]:
        # Unsupported interval is a caller/schema error, not "no data" — raise
        # so it is visible rather than masquerading as an empty result. In the
        # auto-chain the provider only ever receives intervals it advertises.
        freq = _freq_for_interval(interval)
        minute = _is_minute_freq(freq)
        is_index = is_cn_a_share_index_symbol(symbol)
        if minute:
            start_d = _minute_datetime(start_time, end=False)
            end_d = _minute_datetime(end_time, end=True)
        else:
            start_d = _compact_date(start_time)
            end_d = _compact_date(end_time)
        self._ensure_pro_api()

        if is_index and minute:
            df = self._fetch_idx_mins(symbol, freq, start_d, end_d)
        else:
            df = self._fetch_pro_bar(
                symbol,
                start_d=start_d,
                end_d=end_d,
                freq=freq,
                adjust=adjust,
                is_index=is_index,
            )
        if df is None or df.empty:
            logger.info(
                "tushare returned no data for %s [%s, %s] freq=%s",
                symbol, start_time, end_time, freq,
            )
            return []

        bar_adjust = "none" if is_index else "qfq"
        bars: List[Bar] = []
        for _, row in df.iterrows():
            try:
                # Daily/weekly/monthly carry ``trade_date`` (YYYYMMDD);
                # minute bars carry ``trade_time`` (``YYYY-MM-DD HH:MM:SS``).
                ts_raw = str(row.get("trade_time") or row.get("trade_date") or "")
                bars.append(
                    Bar(
                        symbol=symbol,
                        timestamp=normalize_bar_timestamp(ts_raw),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("vol", 0)),
                        amount=_try_float(row.get("amount")),
                        adjust_type=bar_adjust,
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "tushare row parse error for %s: %s; skipping row",
                    symbol, exc,
                )
                continue
        # ``pro_bar`` / ``idx_mins`` return newest-first; the cache merge
        # layer assumes chronological ascending order — flip once at the
        # boundary.
        bars.reverse()
        return bars

    async def is_trading_day(self, day: str) -> bool:
        with data_span("tushare", "is_trading_day"):
            # ``trade_cal`` is rate-limited even for paid tiers; for the
            # common backtest path we approximate with a Mon-Fri check
            # the same way ``akshare_provider`` does. Strategies that
            # need an authoritative calendar should pick baostock or QMT.
            try:
                d = datetime.date.fromisoformat(day[:10])
                return d.weekday() < 5
            except ValueError:
                return False

    async def get_trading_dates(self, start: str, end: str) -> List[str]:
        with data_span("tushare", "get_trading_dates"):
            try:
                d = datetime.date.fromisoformat(start[:10])
                end_d = datetime.date.fromisoformat(end[:10])
            except ValueError:
                return []
            result: List[str] = []
            while d <= end_d:
                if d.weekday() < 5:
                    result.append(d.isoformat())
                d += datetime.timedelta(days=1)
            return result


__all__ = ["TushareDataProvider"]
