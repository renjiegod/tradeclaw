"""Structural typing for market-data providers.

Historically each provider (qmt / akshare / baostock) exposed the same
shape — ``get_bars`` / ``is_trading_day`` / ``get_trading_dates`` — by
convention only (duck-typing). That hid two real bugs:

* The factory's ``auto`` dispatch fell back on a hard-coded pair (akshare
  → qmt) instead of being driven by what each provider can actually
  serve. Adding tushare would have meant another hard-coded branch.
* Two assistant-tool code paths bypassed the factory entirely and called
  akshare/qmt directly, so swapping providers per request was impossible.

This module pins the contract so ``mypy`` and the factory dispatch can
both reason about it. New providers must:

1. Declare a class-level ``capabilities: ProviderCapabilities`` attribute
   describing what intervals they actually support.
2. Implement the four methods on :class:`HistoricalDataProvider`.

Capabilities are intentionally per-provider class, not per-instance:
nothing about a configured ``QmtLiveDataProvider`` instance changes what
intervals or adjust modes the upstream API serves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from doyoutrade.core.models import (
    Bar,
    EarningsExpress,
    EarningsForecast,
    EventItem,
    FundFlowRow,
    Fundamentals,
    LhbRow,
    LhbSeatRow,
    MarketBreadth,
    MarketContext,
    NewsArticle,
    QuoteSnapshot,
    ResearchReport,
    SectorHeatRow,
    SectorMember,
)
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST

# Canonical provider names (also used as the ``provider`` column on the
# persistent ``cached_bars`` table and as the ``data_source`` envelope
# field surfaced to the assistant / CLI / frontend).
PROVIDER_NAME_QMT = "qmt"
PROVIDER_NAME_AKSHARE = "akshare"
PROVIDER_NAME_BAOSTOCK = "baostock"
PROVIDER_NAME_TUSHARE = "tushare"
PROVIDER_NAME_MOOTDX = "mootdx"
PROVIDER_NAME_MOCK = "mock"
# Multi-engine web-search news source (Tavily / Bocha / …). News-only axis;
# never serves OHLCV, so it is not a HistoricalDataProvider name.
PROVIDER_NAME_WEBSEARCH = "websearch"


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a historical-data provider can actually serve.

    The factory's ``auto`` dispatch uses this to pick the next provider
    in the fallback chain for a given interval — e.g. tushare declares
    no minute support, so a ``--data-source auto --interval 1m`` request
    skips tushare without trying the upstream call.

    Attributes:
        name: Canonical id (``"qmt"`` / ``"akshare"`` / etc.). Used as
            the ``provider`` column on persistent cache rows and as the
            ``provider_used`` envelope field.
        supported_intervals: Bar intervals this provider can serve.
            Intervals not in this set must be filtered out by callers
            (factory dispatch, assistant tool fallback) instead of being
            sent upstream and failing late.
        default_adjust: Adjust mode (``"qfq"``/``"hfq"``/``"none"``)
            the provider uses when callers do not specify one. ``"qfq"``
            (前复权) is the runtime default so strategy, backtest, and
            chart semantics stay aligned by default.
        requires_auth: True when the provider needs a configured token /
            session (qmt / tushare). The factory skips unconfigured
            auth-required providers during ``auto`` selection rather
            than letting them fail at call time.
        is_realtime_capable: True when ``get_market_context`` returns
            real exchange ticks (qmt). False for backfill-only sources
            (akshare/baostock/tushare return last-close approximations).
        max_history_years: Maximum lookback the upstream API serves, in
            years. ``None`` when unbounded / unknown. Surfaced for the
            CLI so an operator querying 20 years of history sees the
            right provider being chosen.
        authoritative_calendar: True when ``get_trading_dates`` returns a
            real exchange trading calendar (qmt / baostock hit a calendar
            API). False for sources that approximate with a weekday
            heuristic (akshare / tushare / mock) — those would manufacture
            false gaps around 国庆/春节 if used as the continuity reference,
            so the write-time continuity check only runs its authoritative
            calendar comparison when this is True AND the calendar source is
            the same provider that served the bars.
    """

    name: str
    supported_intervals: frozenset[str] = field(default_factory=frozenset)
    default_adjust: str = DEFAULT_BAR_ADJUST
    requires_auth: bool = False
    is_realtime_capable: bool = False
    max_history_years: int | None = None
    authoritative_calendar: bool = False


@runtime_checkable
class HistoricalDataProvider(Protocol):
    """Structural type for the data layer (qmt / akshare / baostock / tushare).

    ``runtime_checkable`` so the factory can refuse to register a builder
    whose return value lacks the contract — a duck-typed mistake that
    would previously have surfaced as ``AttributeError`` mid-cycle.
    """

    capabilities: ProviderCapabilities

    async def get_bars(
        self,
        symbol: str,
        start_time: str,
        end_time: str,
        *,
        interval: str = "1d",
        adjust: str = DEFAULT_BAR_ADJUST,
    ) -> list[Bar]: ...

    async def get_market_context(self) -> MarketContext: ...

    async def is_trading_day(self, value: str) -> bool: ...

    async def get_trading_dates(self, start: str, end: str) -> list[str]: ...


@runtime_checkable
class SectorProvider(Protocol):
    """Structural type for a sector / industry / concept membership source.

    Sector membership is a separate axis from OHLCV and news — providers
    implement this Protocol independently. ``list_sectors`` returns the
    available board names (optionally filtered by ``sector_type`` ∈
    ``{"industry", "concept"}``); ``get_sector_members`` returns the
    canonical-symbol constituents of one board. Implementations normalize
    constituent codes to Doyoutrade form so a downstream universe file is
    directly screenable.
    """

    capabilities: ProviderCapabilities

    async def list_sectors(
        self, *, sector_type: str | None = None
    ) -> list[str]: ...

    async def get_sector_members(
        self, sector_name: str, *, sector_type: str | None = None
    ) -> list[SectorMember]: ...

    async def get_sector_heat(
        self, sector_type: str
    ) -> list[SectorHeatRow]:
        """Return whole-board heat rows for one board family.

        Reuses the board-name endpoints but keeps the 涨跌幅 / 总市值 / 换手率 /
        上涨·下跌家数 / 领涨股 columns the membership methods drop, returning one
        :class:`SectorHeatRow` per board. ``sector_type`` is required
        (``"industry"`` or ``"concept"``); heat is per family, never merged.
        A persistent upstream failure re-raises (→ ``sector_heat_fetch_failed``);
        an empty board list returns ``[]`` (→ ``sector_heat_empty``); numeric
        columns the upstream omits become ``None`` rather than raising / 0.
        """
        ...


@runtime_checkable
class FundamentalsProvider(Protocol):
    """Structural type for a valuation / market-cap source.

    A separate axis from OHLCV. ``get_fundamentals_batch`` is the primary
    entry point — screening a universe wants one upstream call (e.g. a
    market snapshot) rather than N per-symbol round-trips — and returns a
    ``{canonical_symbol: Fundamentals}`` map covering as many of the
    requested symbols as the source can serve (missing symbols are simply
    absent). ``get_fundamentals`` is the single-symbol convenience.
    """

    capabilities: ProviderCapabilities

    async def get_fundamentals_batch(
        self, symbols: list[str], *, asof: str | None = None
    ) -> dict[str, Fundamentals]: ...

    async def get_fundamentals(
        self, symbol: str, *, asof: str | None = None
    ) -> Fundamentals | None: ...


@runtime_checkable
class EventProvider(Protocol):
    """Structural type for a calendar / status event source.

    A separate axis from OHLCV. ``get_events_batch`` returns a
    ``{canonical_symbol: [EventItem, ...]}`` map for the requested symbols
    as of ``asof`` (``YYYY-MM-DD``) — currently it surfaces suspension
    (停牌) status and upcoming earnings-disclosure (财报预约披露) dates.
    Symbols with no known events are simply absent / empty. The screener
    consumes this to drop event-risk names (suspended, or reporting within
    N days); the date-window logic lives in the screener, not the provider.
    """

    capabilities: ProviderCapabilities

    async def get_events_batch(
        self, symbols: list[str], *, asof: str | None = None
    ) -> dict[str, list[EventItem]]: ...

    async def get_events(
        self, symbol: str, *, asof: str | None = None
    ) -> list[EventItem]: ...


@runtime_checkable
class NewsProvider(Protocol):
    """Structural type for a symbol-scoped news source.

    News is a separate axis from OHLCV — providers implement this Protocol
    independently of :class:`HistoricalDataProvider`. ``fetch_news`` takes
    an inclusive ``[start, end]`` date window (``YYYY-MM-DD``); providers
    whose upstream API has no date parameter (e.g. akshare's
    ``stock_news_em`` returns only recent items) must filter client-side
    so the returned list never leaks rows outside the window.
    """

    capabilities: ProviderCapabilities

    async def fetch_news(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        limit: int | None = None,
    ) -> list[NewsArticle]: ...


@runtime_checkable
class ResearchReportProvider(Protocol):
    """Structural type for a symbol-scoped brokerage research-report source.

    Research reports are a separate axis from OHLCV and from news — they
    surface analyst opinion (rating / institution / EPS & PE forecasts)
    rather than market prices or media articles. ``fetch_research_reports``
    takes an inclusive ``[start, end]`` date window (``YYYY-MM-DD``);
    providers whose upstream API has no date parameter (e.g. akshare's
    ``stock_research_report_em`` returns all available reports for the
    symbol) must filter client-side so the returned list never leaks rows
    outside the window.
    """

    capabilities: ProviderCapabilities

    async def fetch_research_reports(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        limit: int | None = None,
    ) -> list[ResearchReport]: ...


@runtime_checkable
class EarningsProvider(Protocol):
    """Structural type for an earnings-data source (batch, period-scoped).

    Earnings data (业绩预告 / 业绩快报) is a separate axis from OHLCV and
    from symbol-scoped news/research — the upstream serves a *full-market*
    snapshot for one fiscal quarter-end (report period), so this Protocol
    is **batch**: callers pass the set of canonical symbols they care about
    plus the list of report-period tokens (``YYYYMMDD`` quarter-ends), and
    the provider pulls each period once for the whole market then filters
    to the requested symbols in memory. This avoids re-fetching the whole
    market once per symbol.

    Symbols with no row for a given period are simply absent from that
    period's slice; the returned map covers every requested symbol that had
    *any* row across *any* requested period.
    """

    capabilities: ProviderCapabilities

    async def fetch_earnings_forecasts(
        self,
        symbols: list[str],
        report_periods: list[str],
    ) -> dict[str, list[EarningsForecast]]: ...

    async def fetch_earnings_express(
        self,
        symbols: list[str],
        report_periods: list[str],
    ) -> dict[str, list[EarningsExpress]]: ...


@runtime_checkable
class MarketBreadthProvider(Protocol):
    """Structural type for an A-share limit-up / down / broken-board source.

    Market breadth is a separate axis from OHLCV, news, and research — it
    surfaces whole-market 打板 (limit-hitting) breadth for a single trading
    day rather than one symbol's price series. ``fetch_market_breadth``
    takes a ``YYYYMMDD`` trade-date token (the upstream akshare pool
    functions all require an explicit date) and returns a
    :class:`MarketBreadth` aggregating the day's 涨停 / 跌停 / 炸板 pools
    plus the derived 连板梯队 (ladder) and 炸板率 (broken-board rate).

    Failure-mode discipline (per CLAUDE.md §错误可见性): a single pool that
    fails upstream is recorded on ``MarketBreadth.pool_errors`` (never
    silently dropped) so the tool can report a ``partial`` status; a
    genuinely empty day (all three pools empty, likely a non-trading day)
    comes back with three empty lists so the tool maps it to a distinct
    ``market_breadth_empty`` rather than a fetch error.
    """

    capabilities: ProviderCapabilities

    async def fetch_market_breadth(self, trade_date: str) -> MarketBreadth: ...


@runtime_checkable
class DragonTigerProvider(Protocol):
    """Structural type for an A-share 龙虎榜 (dragon-tiger board) source.

    The 龙虎榜 is a separate axis from OHLCV, breadth, and news — it surfaces
    the exchange's daily large-order / abnormal-move disclosure list for a
    date range rather than one symbol's price series.
    ``fetch_dragon_tiger`` takes an inclusive ``[start_date, end_date]`` window
    of ``YYYYMMDD`` tokens (the upstream akshare ``stock_lhb_detail_em`` takes a
    start/end range) and returns a flat list of :class:`LhbRow` — one row per
    (name, 上榜日) that made the board inside the window.

    Failure-mode discipline (per CLAUDE.md §错误可见性): a *persistent* upstream
    failure (all retries exhausted) re-raises so the ``data_lhb`` tool can
    surface a distinct ``lhb_fetch_failed`` error_code with the exception type;
    a genuinely empty window (no name made the board, or before the after-hours
    snapshot updates) returns ``[]`` so the tool maps it to a distinct
    ``lhb_empty`` rather than a fetch error.

    The per-seat / 游资 detail mode is a *second* method on the same axis:
    ``fetch_seat_detail`` takes one canonical ``symbol`` + a single ``date``
    (``YYYYMMDD``) and returns that name's per-营业部 (trading desk) buy/sell
    席位明细 for that day (upstream ``stock_lhb_stock_detail_em``). Its
    failure-mode discipline mirrors the board method with one extra split: a
    symbol that did NOT make the board on the requested day raises
    :class:`LhbNoSeatDataError` (a *distinct* "no seat data" condition, not a
    transport failure) so the tool can surface ``lhb_no_seat_data`` separately
    from ``lhb_fetch_failed``.
    """

    capabilities: ProviderCapabilities

    async def fetch_dragon_tiger(
        self, start_date: str, end_date: str
    ) -> list[LhbRow]: ...

    async def fetch_seat_detail(
        self, symbol: str, date: str
    ) -> list[LhbSeatRow]: ...


@runtime_checkable
class FundFlowProvider(Protocol):
    """Structural type for an A-share 资金流排名 (fund-flow ranking) source.

    Fund flow is a separate axis from OHLCV — it surfaces main / super-large /
    large / medium / small net inflow rankings for a rolling window rather than
    one symbol's price series. ``fetch_fund_flow`` takes a ``scope``
    (``"individual"`` per-stock or ``"sector"`` per-board), a ``period`` window
    token (``今日`` / ``3日`` / ``5日`` / ``10日``; the ``sector`` scope has no 3日),
    and — for the ``sector`` scope only — a ``sector_type`` upstream token
    (``行业资金流`` / ``概念资金流`` / ``地域资金流``). Neither upstream function takes a
    date. Returns a flat list of :class:`FundFlowRow`.

    Failure-mode discipline (per CLAUDE.md §错误可见性): a *persistent* upstream
    failure (all retries exhausted; the 今日 endpoint intermittently
    ``RemoteDisconnected`` in test environments) re-raises so the
    ``data_fund_flow`` tool can surface a distinct ``fund_flow_fetch_failed``
    error_code with the exception type; a genuinely empty result returns ``[]``
    so the tool maps it to a distinct ``fund_flow_empty``. Columns are matched
    by 子串 (the individual endpoint prefixes每列 with the period, e.g.
    ``今日主力净流入-净额``); a column the upstream omits becomes ``None`` on the
    row rather than raising.
    """

    capabilities: ProviderCapabilities

    async def fetch_fund_flow(
        self,
        scope: str,
        period: str,
        *,
        sector_type: str | None = None,
    ) -> list[FundFlowRow]: ...


@runtime_checkable
class RealtimeQuoteProvider(Protocol):
    """Structural type for a one-shot realtime quote source (qmt, mootdx, akshare today).

    Realtime quotes are a separate axis from OHLCV history — ``fetch_quotes``
    takes a list of canonical symbols and returns a
    ``{symbol: QuoteSnapshot}`` map covering **every** requested symbol.
    Symbols the upstream cannot serve must come back as a placeholder
    ``QuoteSnapshot`` with ``status="no_data"`` (never silently dropped) so
    callers can tell "symbol unknown / suspended" apart from "qmt down".

    This Protocol is the *snapshot* surface (the REST endpoint and the
    initial frame on a new WebSocket subscription). Streaming fan-out lives
    in :class:`doyoutrade.data.quote_stream.QuoteStreamService`, which holds a
    ``RealtimeQuoteProvider`` for its cached snapshots.
    """

    async def fetch_quotes(
        self, symbols: list[str]
    ) -> dict[str, QuoteSnapshot]: ...


__all__ = [
    "HistoricalDataProvider",
    "NewsProvider",
    "ResearchReportProvider",
    "EarningsProvider",
    "SectorProvider",
    "FundamentalsProvider",
    "EventProvider",
    "MarketBreadthProvider",
    "DragonTigerProvider",
    "FundFlowProvider",
    "RealtimeQuoteProvider",
    "ProviderCapabilities",
    "PROVIDER_NAME_AKSHARE",
    "PROVIDER_NAME_BAOSTOCK",
    "PROVIDER_NAME_MOCK",
    "PROVIDER_NAME_MOOTDX",
    "PROVIDER_NAME_QMT",
    "PROVIDER_NAME_TUSHARE",
    "PROVIDER_NAME_WEBSEARCH",
]
