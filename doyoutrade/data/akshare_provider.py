"""Akshare-based data provider for A-share market (沪深北交所).

Provides historical K-line data and real-time market quotes via akshare.
Account snapshots are supplied separately via :mod:`doyoutrade.account` (typically a
mock reader in the factory stack), since akshare has no broker account API.

Usage (register and build via factory):
    from doyoutrade.data.factory import register_trading_data_provider, build_trading_data_stack
    register_trading_data_provider("akshare", build_akshare_stack)
    provider, universe, account_reader = build_trading_data_stack("akshare", data_settings, symbols)

Or via config:
    data:
      provider: akshare
      akshare: {}   # currently no provider-specific settings
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import akshare as ak
import httpx

from doyoutrade.data.bar_timestamp import normalize_bar_timestamp
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrument_catalog.a_share_equity import (
    is_cn_a_share_etf_symbol,
    is_cn_a_share_index_symbol,
)

logger = logging.getLogger(__name__)
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities
from doyoutrade.core.models import Bar, MarketContext, QuoteSnapshot


# ─── Interval mapping ────────────────────────────────────────────────────────

# Map generic interval names (used in TradingDataProvider) to akshare parameters.
# Daily-shaped periods served by the eastmoney *daily* endpoints
# (stock_zh_a_hist / fund_etf_hist_em) — these accept only daily/weekly/monthly.
_INTERVAL_PERIOD_MAP: Dict[str, str] = {
    "1d": "daily",
    "1w": "weekly",
    "weekly": "weekly",
    "1mo": "monthly",
    "monthly": "monthly",
}

# Intraday periods served by the eastmoney *minute* endpoints
# (stock_zh_a_hist_min_em / fund_etf_hist_min_em). The daily endpoints reject
# these period values (raise KeyError('60') etc.), so intraday intervals MUST be
# routed here — mapping them into _INTERVAL_PERIOD_MAP silently returned zero
# bars (declared-but-broken support).
_INTRADAY_PERIOD_MAP: Dict[str, str] = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "60m": "60",
}


def _min_em_bound(value: str, *, is_end: bool) -> str:
    """Format a bound for the eastmoney minute endpoints (``YYYY-MM-DD HH:MM:SS``).

    Date-only bounds are widened to the full A-share session (09:30 open /
    15:00 close). Widening never drops bars — the local cache / backtest layers
    re-filter to the exact requested window — while a too-narrow bound would.
    """
    date_part = str(value).strip()[:10]
    session = "15:00:00" if is_end else "09:30:00"
    return f"{date_part} {session}"

_ADJUST_MAP: Dict[str, str] = {
    "none": "",
    "qfq": "qfq",
    "hfq": "hfq",
}


# ─── Historical K-line provider ─────────────────────────────────────────────

class AkshareHistoricalProvider:
    """Wraps akshare stock_zh_a_hist() into get_bars()."""

    def __init__(self):
        pass

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> List[Bar]:
        with data_span("akshare", "get_bars"):
            return await asyncio.to_thread(
                self._sync_get_bars, symbol, start_time, end_time, interval, adjust
            )

    def _sync_get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        interval: str,
        adjust: str,
    ) -> List[Bar]:
        # akshare hist endpoints expect the bare 6-digit code without suffix.
        ak_symbol = symbol.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")

        # normalize adjust to what akshare accepts
        adjust_param = _ADJUST_MAP.get(adjust, "")

        # Three instrument classes ride three different eastmoney endpoint
        # families — the wrong one silently returns zero rows:
        #   * 指数 (index):  index_zh_a_hist / index_zh_a_hist_min_em (NO adjust)
        #   * ETF (场内基金): fund_etf_hist_em / fund_etf_hist_min_em
        #   * 个股 (stock):   stock_zh_a_hist / stock_zh_a_hist_min_em
        # Daily and minute endpoints share the same 开盘/收盘/最高/最低/成交量/
        # 成交额 columns (daily keys the timestamp as 日期, minute as 时间), so the
        # row-parsing loop below is shared. is_etf / is_index are mutually
        # exclusive by prefix construction.
        is_index = is_cn_a_share_index_symbol(symbol)
        is_etf = is_cn_a_share_etf_symbol(symbol)
        intraday_period = _INTRADAY_PERIOD_MAP.get(interval)

        if intraday_period is not None:
            # Intraday → minute endpoints (period ∈ {1,5,15,30,60}). The daily
            # endpoints raise KeyError on these period values.
            start_arg = _min_em_bound(start_time, is_end=False)
            end_arg = _min_em_bound(end_time, is_end=True)

            if is_index:
                # Index minute endpoint takes no adjust (indices aren't adjusted).
                api_name = "index_zh_a_hist_min_em"

                def _call() -> Any:
                    return ak.index_zh_a_hist_min_em(
                        symbol=ak_symbol,
                        start_date=start_arg,
                        end_date=end_arg,
                        period=intraday_period,
                    )

            else:
                # akshare's 1-minute feed rejects a non-empty adjust; force raw.
                min_adjust = "" if intraday_period == "1" else adjust_param
                api_name = "fund_etf_hist_min_em" if is_etf else "stock_zh_a_hist_min_em"

                def _call() -> Any:
                    min_api = ak.fund_etf_hist_min_em if is_etf else ak.stock_zh_a_hist_min_em
                    return min_api(
                        symbol=ak_symbol,
                        start_date=start_arg,
                        end_date=end_arg,
                        period=intraday_period,
                        adjust=min_adjust,
                    )

        else:
            period = _INTERVAL_PERIOD_MAP.get(interval, "daily")
            start_arg = start_time.replace("-", "")
            end_arg = end_time.replace("-", "")

            if is_index:
                # Index daily endpoint takes no adjust either.
                api_name = "index_zh_a_hist"

                def _call() -> Any:
                    return ak.index_zh_a_hist(
                        symbol=ak_symbol,
                        period=period,
                        start_date=start_arg,
                        end_date=end_arg,
                    )

            else:
                api_name = "fund_etf_hist_em" if is_etf else "stock_zh_a_hist"

                def _call() -> Any:
                    if is_etf:
                        return ak.fund_etf_hist_em(
                            symbol=ak_symbol,
                            period=period,
                            start_date=start_arg,
                            end_date=end_arg,
                            adjust=adjust_param,
                        )
                    return ak.stock_zh_a_hist(
                        symbol=ak_symbol,
                        start_date=start_arg,
                        end_date=end_arg,
                        period=period,
                        adjust=adjust_param,
                    )

        df = None
        for attempt in range(3):
            try:
                df = _call()
                break
            except Exception as exc:
                logger.warning(
                    "akshare %s failed for %s (attempt %d/3): %s",
                    api_name, symbol, attempt + 1, exc,
                )
                if attempt == 2:
                    logger.error(
                        "akshare %s gave up for %s [%s, %s]: %s",
                        api_name, symbol, start_time, end_time, exc,
                    )
                    return []
                time.sleep(0.8 * (attempt + 1))

        if df is None or df.empty:
            logger.warning(
                "akshare %s returned no data for %s [%s, %s]",
                api_name, symbol, start_time, end_time,
            )
            return []

        bars: List[Bar] = []
        skipped_suspended = 0
        for _, row in df.iterrows():
            # axkshare returns columns: 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
            ts_raw = row.get("日期") or row.get("时间")
            o, h, low, c, vol = row["开盘"], row["最高"], row["最低"], row["收盘"], row["成交量"]
            # 停牌日量价为空（akshare 无 tradestatus 列），跳过而非填 0 造出假 bar。
            if _is_blank(vol) or _is_blank(o) or _is_blank(h) or _is_blank(low) or _is_blank(c):
                skipped_suspended += 1
                continue
            bars.append(
                Bar(
                    symbol=symbol,
                    timestamp=normalize_bar_timestamp(str(ts_raw)),
                    open=float(o),
                    high=float(h),
                    low=float(low),
                    close=float(c),
                    volume=float(vol),
                    amount=_try_float(row.get("成交额")),
                )
            )
        if skipped_suspended:
            logger.info(
                "akshare skipped %d suspended/blank bars for %s [%s, %s]",
                skipped_suspended, symbol, start_time, end_time,
            )
        return bars


# ─── Realtime market provider ───────────────────────────────────────────────

# Single-symbol HTTP quote endpoints used as a cascade behind the em snapshot.
# Both are the same lightweight, key-free endpoints used by other A-share
# retail tools (e.g. daily_stock_analysis's akshare_fetcher): a full-market
# ``stock_zh_a_spot_em()`` scan and these single-symbol feeds fail
# independently (observed in practice: em's upstream reset the connection
# while the tencent endpoint stayed reachable), so cascading is a real
# resilience gain, not just defense-in-depth on paper.
_SINA_QUOTE_URL = "http://hq.sinajs.cn/list={symbol}"
_TENCENT_QUOTE_URL = "http://qt.gtimg.cn/q={symbol}"
_REALTIME_HTTP_TIMEOUT = 5.0


def _to_market_prefixed_symbol(symbol: str) -> Optional[str]:
    """Map ``600519.SH`` -> ``sh600519`` etc. Sina/Tencent have no 北交所 feed."""
    if symbol.endswith(".SH"):
        return f"sh{symbol[:-3]}"
    if symbol.endswith(".SZ"):
        return f"sz{symbol[:-3]}"
    return None


def _make_realtime_http_client() -> httpx.AsyncClient:
    """Factory seam — tests patch this to inject an ``httpx.MockTransport``."""
    return httpx.AsyncClient(timeout=_REALTIME_HTTP_TIMEOUT)


def _parse_quote_payload(text: str, *, quote_char: str, sep: str) -> Optional[List[str]]:
    """Extract the ``field1<sep>field2<sep>...`` payload inside the quoted string.

    Both sina (``var hq_str_x="a,b,c";``) and tencent (``v_x="a~b~c";``) wrap
    their payload in a single quoted string; an empty/missing quote body means
    the upstream had nothing for this symbol (delisted, suspended, bad code).
    """
    start = text.find(quote_char)
    end = text.rfind(quote_char)
    if start == -1 or end == -1 or end <= start:
        return None
    body = text[start + 1:end]
    if not body:
        return None
    return body.split(sep)


class AkshareRealtimeProvider:
    """Realtime A-share quotes via an em -> sina -> tencent source cascade.

    ``stock_zh_a_spot_em()`` is a full-market snapshot: querying it once
    answers every requested symbol, so batch callers must use
    :meth:`fetch_quotes` rather than looping :meth:`fetch_latest_price`
    (which used to trigger one full-market scan per symbol). When em's
    upstream is unreachable, symbols it couldn't answer fall through to the
    single-symbol sina/tencent HTTP endpoints. Every fallback step and any
    symbol left unanswered after all three sources is surfaced via a
    ``data_provider.get_realtime_quote`` debug event plus a logger call, per
    the project's no-silent-fallback rule.
    """

    def __init__(self):
        pass

    async def fetch_latest_price(self, symbol: str) -> Optional[float]:
        """Return latest price for a single symbol, or None on failure."""
        quotes = await self.fetch_quotes([symbol])
        return quotes.get(symbol)

    async def fetch_quotes(self, symbols: List[str]) -> Dict[str, float]:
        """Batch latest-price lookup: one em snapshot, cascaded per-symbol fallback."""
        requested = list(dict.fromkeys(symbols))  # de-dupe, keep order
        if not requested:
            return {}

        results: Dict[str, float] = {}
        source_used: Dict[str, str] = {}

        em_prices, em_error = await asyncio.to_thread(self._sync_fetch_em_snapshot, requested)
        if em_error is not None:
            logger.warning(
                "akshare em realtime snapshot failed (%d symbols pending fallback): %s: %s",
                len(requested), type(em_error).__name__, em_error,
            )
        results.update(em_prices)
        for sym in em_prices:
            source_used[sym] = "em"
        missing = [s for s in requested if s not in results]

        if missing:
            async with _make_realtime_http_client() as client:
                for symbol in list(missing):
                    price, source = await self._fetch_single_symbol_cascade(client, symbol)
                    if price is not None:
                        results[symbol] = price
                        source_used[symbol] = source
                        missing.remove(symbol)

        if missing:
            logger.warning("akshare realtime quote: no source answered for %s", missing)

        _emit_realtime_quote_event(
            requested=requested,
            source_used=source_used,
            missing=missing,
            em_error=em_error,
        )
        return results

    async def _fetch_single_symbol_cascade(
        self, client: "httpx.AsyncClient", symbol: str
    ) -> tuple[Optional[float], str]:
        prefixed = _to_market_prefixed_symbol(symbol)
        if prefixed is None:
            logger.info(
                "akshare realtime quote: %s has no sina/tencent feed (北交所 not covered by these endpoints)",
                symbol,
            )
            return None, "unsupported"

        price = await self._fetch_sina(client, symbol, prefixed)
        if price is not None:
            return price, "sina"

        price = await self._fetch_tencent(client, symbol, prefixed)
        if price is not None:
            return price, "tencent"

        return None, "none"

    async def _fetch_sina(
        self, client: "httpx.AsyncClient", symbol: str, prefixed: str
    ) -> Optional[float]:
        # var hq_str_sh600519="贵州茅台,1866.000,1870.000,1866.500,...";
        try:
            resp = await client.get(_SINA_QUOTE_URL.format(symbol=prefixed))
            resp.encoding = "gbk"
            text = resp.text.strip()
        except Exception as exc:
            logger.info("akshare realtime fallback: sina request failed for %s: %s: %s", symbol, type(exc).__name__, exc)
            return None
        fields = _parse_quote_payload(text, quote_char='"', sep=",")
        if fields is None or len(fields) < 4:
            return None
        return _try_float(fields[3])

    async def _fetch_tencent(
        self, client: "httpx.AsyncClient", symbol: str, prefixed: str
    ) -> Optional[float]:
        # v_sh600519="1~贵州茅台~600519~1866.00~1870.00~...";
        try:
            resp = await client.get(_TENCENT_QUOTE_URL.format(symbol=prefixed))
            resp.encoding = "gbk"
            text = resp.text.strip()
        except Exception as exc:
            logger.info("akshare realtime fallback: tencent request failed for %s: %s: %s", symbol, type(exc).__name__, exc)
            return None
        fields = _parse_quote_payload(text, quote_char='"', sep="~")
        if fields is None or len(fields) < 4:
            return None
        return _try_float(fields[3])

    def _sync_fetch_em_snapshot(
        self, symbols: List[str]
    ) -> tuple[Dict[str, float], Optional[Exception]]:
        """Sync helper — one full-market snapshot, filtered to the requested symbols."""
        try:
            df = ak.stock_zh_a_spot_em()
        except Exception as exc:
            return {}, exc

        bare_to_full = {
            s.replace(".SH", "").replace(".SZ", "").replace(".BJ", ""): s for s in symbols
        }
        # Columns: 代码, 名称, 最新价, 涨跌幅, 涨跌额, 成交量, 成交额, 振幅, 最高, 最低, ...
        matched = df[df["代码"].isin(bare_to_full.keys())]
        out: Dict[str, float] = {}
        for _, row in matched.iterrows():
            full = bare_to_full.get(row["代码"])
            price = _try_float(row.get("最新价"))
            if full is not None and price is not None and price == price:  # exclude NaN
                out[full] = price
        return out, None


class AkshareRealtimeQuoteProvider:
    """RealtimeQuoteProvider (full ``QuoteSnapshot``) via the em -> sina -> tencent cascade.

    Companion to :class:`AkshareRealtimeProvider` (which only returns a bare
    price for ``get_market_context``): this class serves the watchlist's
    ``RealtimeQuoteProvider`` protocol (see :mod:`doyoutrade.data.protocols`)
    so akshare can act as a non-QMT realtime quote source — chained behind
    mootdx in :func:`doyoutrade.bootstrap._build_quote_stream_service` via
    :class:`doyoutrade.data.fallback_provider.FallbackRealtimeQuoteProvider`.

    The em snapshot (``stock_zh_a_spot_em``) carries the full field set
    (涨跌幅/涨跌额/成交量/成交额/最高/最低/今开/昨收) in one call. Symbols it
    misses (NaN price or absent from the snapshot — 北交所 sometimes, or a
    transient em outage) fall through to the single-symbol sina/tencent HTTP
    endpoints, which only expose price/prev_close/open (+ high/low for sina);
    those symbols' ``volume``/``amount`` stay ``None`` rather than fabricated.
    A symbol every source fails to answer comes back as a ``status="no_data"``
    placeholder — never silently dropped (per CLAUDE.md §错误可见性).
    """

    def __init__(self):
        pass

    async def fetch_quotes(self, symbols: List[str]) -> Dict[str, QuoteSnapshot]:
        with data_span("akshare", "fetch_quotes"):
            return await self._fetch_quotes(symbols)

    async def _fetch_quotes(self, symbols: List[str]) -> Dict[str, QuoteSnapshot]:
        requested = list(dict.fromkeys(symbols))
        result: Dict[str, QuoteSnapshot] = {
            s: QuoteSnapshot(symbol=s, status="no_data") for s in requested
        }
        if not requested:
            return result

        em_quotes, em_error = await asyncio.to_thread(self._sync_fetch_em_quotes, requested)
        if em_error is not None:
            logger.warning(
                "akshare em realtime snapshot failed (%d symbols pending sina/tencent fallback): %s: %s",
                len(requested), type(em_error).__name__, em_error,
            )
        result.update(em_quotes)
        source_used: Dict[str, str] = {s: "em" for s in em_quotes}
        missing = [s for s in requested if s not in em_quotes]

        if missing:
            async with _make_realtime_http_client() as client:
                for symbol in list(missing):
                    quote, source = await self._fetch_single_symbol_quote_cascade(client, symbol)
                    if quote is not None:
                        result[symbol] = quote
                        source_used[symbol] = source
                        missing.remove(symbol)

        if missing:
            logger.warning("akshare realtime quote (snapshot): no source answered for %s", missing)

        _emit_realtime_quote_snapshot_event(
            requested=requested, source_used=source_used, missing=missing, em_error=em_error,
        )
        return result

    def _sync_fetch_em_quotes(
        self, symbols: List[str]
    ) -> tuple[Dict[str, QuoteSnapshot], Optional[Exception]]:
        """Sync helper — one full-market snapshot, filtered to the requested symbols."""
        try:
            df = ak.stock_zh_a_spot_em()
        except Exception as exc:
            return {}, exc

        bare_to_full = {
            s.replace(".SH", "").replace(".SZ", "").replace(".BJ", ""): s for s in symbols
        }
        matched = df[df["代码"].isin(bare_to_full.keys())]
        out: Dict[str, QuoteSnapshot] = {}
        for _, row in matched.iterrows():
            full = bare_to_full.get(row["代码"])
            if full is None:
                continue
            price = _try_float(row.get("最新价"))
            if price is None or price != price:  # exclude NaN
                continue
            vol_lots = _try_float(row.get("成交量"))
            out[full] = QuoteSnapshot(
                symbol=full,
                price=price,
                prev_close=_try_float(row.get("昨收")),
                change=_try_float(row.get("涨跌额")),
                change_pct=_try_float(row.get("涨跌幅")),
                open=_try_float(row.get("今开")),
                high=_try_float(row.get("最高")),
                low=_try_float(row.get("最低")),
                volume=vol_lots * 100.0 if vol_lots is not None else None,  # 手 -> 股
                amount=_try_float(row.get("成交额")),
                status="ok",
            )
        return out, None

    async def _fetch_single_symbol_quote_cascade(
        self, client: "httpx.AsyncClient", symbol: str
    ) -> tuple[Optional[QuoteSnapshot], str]:
        prefixed = _to_market_prefixed_symbol(symbol)
        if prefixed is None:
            logger.info(
                "akshare realtime quote: %s has no sina/tencent feed (北交所 not covered by these endpoints)",
                symbol,
            )
            return None, "unsupported"

        quote = await self._fetch_sina_quote(client, symbol, prefixed)
        if quote is not None:
            return quote, "sina"

        quote = await self._fetch_tencent_quote(client, symbol, prefixed)
        if quote is not None:
            return quote, "tencent"

        return None, "none"

    async def _fetch_sina_quote(
        self, client: "httpx.AsyncClient", symbol: str, prefixed: str
    ) -> Optional[QuoteSnapshot]:
        # var hq_str_sh600519="名称,今开,昨收,当前价,最高,最低,...";
        try:
            resp = await client.get(_SINA_QUOTE_URL.format(symbol=prefixed))
            resp.encoding = "gbk"
            text = resp.text.strip()
        except Exception as exc:
            logger.info(
                "akshare realtime quote fallback: sina request failed for %s: %s: %s",
                symbol, type(exc).__name__, exc,
            )
            return None
        fields = _parse_quote_payload(text, quote_char='"', sep=",")
        if fields is None or len(fields) < 4:
            return None
        return _quote_from_sina_fields(symbol, fields)

    async def _fetch_tencent_quote(
        self, client: "httpx.AsyncClient", symbol: str, prefixed: str
    ) -> Optional[QuoteSnapshot]:
        # v_sh600519="未知~名称~代码~当前价~昨收~今开~...";
        try:
            resp = await client.get(_TENCENT_QUOTE_URL.format(symbol=prefixed))
            resp.encoding = "gbk"
            text = resp.text.strip()
        except Exception as exc:
            logger.info(
                "akshare realtime quote fallback: tencent request failed for %s: %s: %s",
                symbol, type(exc).__name__, exc,
            )
            return None
        fields = _parse_quote_payload(text, quote_char='"', sep="~")
        if fields is None or len(fields) < 4:
            return None
        return _quote_from_tencent_fields(symbol, fields)


def _quote_from_sina_fields(symbol: str, fields: List[str]) -> Optional[QuoteSnapshot]:
    price = _try_float(fields[3])
    if price is None:
        return None
    prev_close = _try_float(fields[2]) if len(fields) > 2 else None
    open_ = _try_float(fields[1]) if len(fields) > 1 else None
    high = _try_float(fields[4]) if len(fields) > 4 else None
    low = _try_float(fields[5]) if len(fields) > 5 else None
    change = change_pct = None
    if prev_close is not None and prev_close > 0:
        change = price - prev_close
        change_pct = change / prev_close * 100.0
    return QuoteSnapshot(
        symbol=symbol,
        price=price,
        prev_close=prev_close,
        change=change,
        change_pct=change_pct,
        open=open_,
        high=high,
        low=low,
        status="ok",
    )


def _quote_from_tencent_fields(symbol: str, fields: List[str]) -> Optional[QuoteSnapshot]:
    price = _try_float(fields[3])
    if price is None:
        return None
    prev_close = _try_float(fields[4]) if len(fields) > 4 else None
    open_ = _try_float(fields[5]) if len(fields) > 5 else None
    change = change_pct = None
    if prev_close is not None and prev_close > 0:
        change = price - prev_close
        change_pct = change / prev_close * 100.0
    return QuoteSnapshot(
        symbol=symbol,
        price=price,
        prev_close=prev_close,
        change=change,
        change_pct=change_pct,
        open=open_,
        status="ok",
    )


def _emit_realtime_quote_snapshot_event(
    *,
    requested: List[str],
    source_used: Dict[str, str],
    missing: List[str],
    em_error: Optional[Exception],
) -> None:
    _fire_event(
        "data_provider.get_realtime_quote_snapshot",
        {
            "provider": "akshare",
            "method": "fetch_quotes",
            "symbols_requested": requested,
            # per-symbol source that actually answered: "em" | "sina" | "tencent"
            "source_used": source_used,
            "missing": missing,
            "em_error_type": type(em_error).__name__ if em_error is not None else None,
            "em_error_message": str(em_error) if em_error is not None else None,
            "hint": (
                "some symbols got no quote from em/sina/tencent; check network "
                "reachability, or the symbol is 北交所 (unsupported by sina/tencent)"
                if missing
                else None
            ),
        },
    )


# ─── Composite data provider ────────────────────────────────────────────────

class AkshareDataProvider:
    """TradingDataProvider backed by akshare for market data (no broker account)."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        # Daily shapes go through ``_INTERVAL_PERIOD_MAP`` (stock_zh_a_hist),
        # intraday through ``_INTRADAY_PERIOD_MAP`` (stock_zh_a_hist_min_em).
        # ``1w``/``1mo`` aliases are kept in sync with the assistant tool's
        # interval surface so the same string normalizes the same way no
        # matter who calls ``get_bars``.
        supported_intervals=frozenset(
            {"1d", "1w", "1mo", "weekly", "monthly", "1m", "5m", "15m", "30m", "60m"}
        ),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        # akshare's ``stock_zh_a_spot_em`` snapshot is realtime-ish but is
        # a full-market scan rather than a per-symbol push; treat it as
        # non-realtime so the auto-dispatcher prefers QMT for live ticks.
        is_realtime_capable=False,
        max_history_years=30,
    )

    def __init__(self, symbols: List[str]):
        self.symbols = list(symbols)
        self._historical = AkshareHistoricalProvider()
        self._realtime = AkshareRealtimeProvider()

    # ── Market ───────────────────────────────────────────────────────────────

    async def get_market_context(self) -> MarketContext:
        with data_span("akshare", "get_market_context"):
            symbol_to_price: Dict[str, float] = {}
            symbol_to_tick: Dict[str, dict] = {}

            # One batched cascade covers every symbol; looping fetch_latest_price
            # here used to trigger one full-market em snapshot per symbol.
            quotes = await self._realtime.fetch_quotes(self.symbols)
            for sym in self.symbols:
                symbol_to_price[sym] = quotes.get(sym, 0.0)

            _emit_market_context_event(self.symbols, symbol_to_price)
            return MarketContext(
                symbol_to_price=symbol_to_price,
                symbol_to_tick=symbol_to_tick,
            )

    # ── Historical ───────────────────────────────────────────────────────────

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
        _emit_get_bars_event(symbol, start_time, end_time, interval, len(bars), adjust=adjust)
        return bars

    # ── Calendar ─────────────────────────────────────────────────────────────

    async def is_trading_day(self, date: str) -> bool:
        with data_span("akshare", "is_trading_day"):
            # Akshare does not expose an official is_trading_day API.
            # Approximate using Mon-Fri heuristic (same as mock provider).
            import datetime

            try:
                d = datetime.date.fromisoformat(date)
                return d.weekday() < 5
            except ValueError:
                return False

    async def get_trading_dates(self, start: str, end: str) -> List[str]:
        with data_span("akshare", "get_trading_dates"):
            import datetime

            result: List[str] = []
            try:
                d = datetime.date.fromisoformat(start)
                end_d = datetime.date.fromisoformat(end)
                while d <= end_d:
                    if d.weekday() < 5:
                        result.append(d.isoformat())
                    d += datetime.timedelta(days=1)
            except ValueError:
                pass
            return result


# ─── Debug event helpers ────────────────────────────────────────────────────

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
            "provider": "akshare",
            "method": "get_bars",
            "symbol": symbol,
            "start_time": start_time,
            "end_time": end_time,
            "interval": interval,
            "bar_count": bar_count,
            "adjust": adjust,
        },
    )


def _emit_market_context_event(
    symbols: List[str],
    prices: Dict[str, float],
) -> None:
    _fire_event(
        "data_provider.get_market_context",
        {
            "provider": "akshare",
            "method": "get_market_context",
            "symbols": symbols,
            "prices": prices,
        },
    )


def _emit_realtime_quote_event(
    *,
    requested: List[str],
    source_used: Dict[str, str],
    missing: List[str],
    em_error: Optional[Exception],
) -> None:
    _fire_event(
        "data_provider.get_realtime_quote",
        {
            "provider": "akshare",
            "method": "get_realtime_quote",
            "symbols_requested": requested,
            # per-symbol source that actually answered: "em" | "sina" | "tencent"
            "source_used": source_used,
            "missing": missing,
            "em_error_type": type(em_error).__name__ if em_error is not None else None,
            "em_error_message": str(em_error) if em_error is not None else None,
            "hint": (
                "some symbols got no quote from em/sina/tencent; check network "
                "reachability, or the symbol is 北交所 (unsupported by sina/tencent)"
                if missing
                else None
            ),
        },
    )


def _fire_event(event_name: str, payload: dict) -> None:
    """Fire emit_debug_event as a fire-and-forget task."""
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        # No running event loop; skip
        pass


# ─── Factory helper ─────────────────────────────────────────────────────────

def _try_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_blank(value) -> bool:
    """True for None / NaN / 空串 —— akshare 停牌日的量价就是空串。"""
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    return str(value).strip() == ""
