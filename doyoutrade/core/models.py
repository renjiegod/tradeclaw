from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal
from typing import Any, Dict, List, Literal, Optional

from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.money.decimal_helpers import decimal_from_number

logger = logging.getLogger(__name__)


@dataclass
class MarketContext:
    """Per-cycle market snapshot.

    In a live cycle ``symbol_to_tick`` is populated by the data provider
    (e.g. QMT full-tick). In a backtest cycle the per-cycle snapshot is
    built from cached bars by ``merge_simulated_bar_marks_into_market``;
    ``CachedBarsDataProvider`` deliberately returns an empty context in
    ``scope == "backtest"`` so the live realtime API is not consulted.
    """

    symbol_to_price: Dict[str, float] = field(default_factory=dict)
    symbol_to_tick: Dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class InstrumentKey:
    symbol: str
    market: str


@dataclass
class Bar:
    """OHLCV bar. ``volume`` is traded quantity; ``amount`` is turnover (成交额) when present."""

    symbol: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: Optional[float] = None
    adjust_type: str = DEFAULT_BAR_ADJUST


@dataclass(frozen=True)
class NewsArticle:
    """A single news item attached to a symbol.

    News is a separate data shape from OHLCV — no interval / adjust /
    price fields. ``publish_time`` is normalized to ``YYYY-MM-DD HH:MM:SS``
    (or ``YYYY-MM-DD`` when the upstream only reports a date). ``provider``
    is the data source that served the row (``"akshare"``); ``source`` is
    the originating media outlet (文章来源). ``url`` is the canonical link
    used for de-duplication.
    """

    symbol: str
    title: str
    content: str
    publish_time: str
    source: str
    url: str
    provider: str
    keyword: str = ""


@dataclass(frozen=True)
class ResearchReport:
    """A single brokerage research report attached to a symbol.

    Research reports are a separate data shape from OHLCV and from news —
    they carry analyst opinion rather than market prices or media articles.
    ``report_date`` is normalized to ``YYYY-MM-DD`` (the upstream akshare
    ``日期`` column). ``rating`` is the Eastmoney rating (东财评级, e.g.
    买入 / 增持 / 中性). ``institution`` is the issuing brokerage (机构).
    ``recent_report_count`` is how many reports this symbol received in the
    trailing month (近一月个股研报数), a coarse attention proxy.

    ``eps_forecasts`` / ``pe_forecasts`` map a forecast year (str, e.g.
    ``"2026"``) to the analyst consensus EPS / PE for that year. The
    upstream columns are dynamic (``<year>-盈利预测-收益`` /
    ``<year>-盈利预测-市盈率``) and shift forward over time, so they are
    parsed into year-keyed dicts; a year absent upstream is simply absent
    from the dict. ``provider`` is the data source (``"akshare"``).
    """

    symbol: str
    title: str
    rating: str
    institution: str
    report_date: str
    pdf_url: str
    provider: str
    industry: str = ""
    recent_report_count: int = 0
    eps_forecasts: Dict[str, Optional[float]] = field(default_factory=dict)
    pe_forecasts: Dict[str, Optional[float]] = field(default_factory=dict)


@dataclass(frozen=True)
class LimitPoolStock:
    """A single stock row from an A-share limit-up / limit-down / broken-board pool.

    A separate data shape from OHLCV / news / research — it captures the
    intraday-打板 (limit-hitting) state of one name on one trading day.
    ``pool`` is which of the three upstream pools the row came from:
    ``"limit_up"`` (涨停池), ``"limit_down"`` (跌停池), or ``"broken_board"``
    (炸板池). ``code`` is the raw upstream 6-digit code (代码); ``symbol`` is
    the best-effort canonical Doyoutrade form (``600519.SH``) reconstructed by
    A-share exchange rules. ``streak`` (连板数, limit-up pool only) is the
    consecutive-limit height; it is ``None`` for the down / broken-board
    pools where the upstream carries no such column. All numeric fields are
    ``None`` when the upstream omits / can't-parse them — never silently
    coerced to 0 (per §错误可见性). ``provider`` is the data source
    (``"akshare"``).
    """

    pool: str
    code: str
    symbol: str
    name: str
    provider: str
    change_pct: Optional[float] = None
    latest_price: Optional[float] = None
    turnover: Optional[float] = None
    circulating_mv: Optional[float] = None
    total_mv: Optional[float] = None
    turnover_rate: Optional[float] = None
    industry: str = ""
    streak: Optional[int] = None
    broken_board_count: Optional[int] = None
    first_seal_time: str = ""
    last_seal_time: str = ""


@dataclass(frozen=True)
class MarketBreadth:
    """Aggregated A-share limit-up / limit-down / broken-board breadth for a day.

    A separate data shape — it summarises the whole-market 打板 breadth for a
    single trading day rather than one symbol's series. ``trade_date`` is the
    ``YYYYMMDD`` token the pools were pulled for. The three pool lists carry
    the per-stock rows (:class:`LimitPoolStock`). ``ladder`` maps a
    consecutive-limit height (str, e.g. ``"2"``) to the number of names at
    that height (连板梯队). ``max_streak`` is the tallest ladder rung.
    ``broken_board_rate`` = 炸板 / (涨停 + 炸板), the intraday-failure ratio
    (0.0 when the denominator is 0). ``pool_errors`` records which pools
    failed to fetch (pool name → error message) so a ``partial`` run stays
    visible instead of silently under-counting. ``provider`` is the data
    source (``"akshare"``).
    """

    trade_date: str
    provider: str
    limit_up: List[LimitPoolStock] = field(default_factory=list)
    limit_down: List[LimitPoolStock] = field(default_factory=list)
    broken_board: List[LimitPoolStock] = field(default_factory=list)
    ladder: Dict[str, int] = field(default_factory=dict)
    max_streak: int = 0
    broken_board_rate: float = 0.0
    pool_errors: Dict[str, str] = field(default_factory=dict)

    @property
    def limit_up_count(self) -> int:
        return len(self.limit_up)

    @property
    def limit_down_count(self) -> int:
        return len(self.limit_down)

    @property
    def broken_board_count(self) -> int:
        return len(self.broken_board)


@dataclass(frozen=True)
class LhbRow:
    """A single 龙虎榜 (dragon-tiger board) detail row for one trading day.

    A separate data shape from OHLCV / breadth — it captures one名字 that
    made the exchange's daily 龙虎榜 (large-order / abnormal-move disclosure)
    on a given ``on_date``. ``code`` is the raw upstream 6-digit code (代码);
    ``symbol`` is the best-effort canonical Doyoutrade form (``600519.SH``)
    reconstructed by A-share exchange rules. ``reason`` is the 上榜原因 (why the
    name was disclosed) and ``interpretation`` the 解读. All numeric fields
    (net / buy / sell amounts in 元, 涨跌幅 / 换手率 in %, 流通市值 in 元) are
    ``None`` when the upstream omits / can't-parse them — never silently
    coerced to 0 (per §错误可见性). ``provider`` is the data source
    (``"akshare"``).

    This is the *market-level* per-day board (``stock_lhb_detail_em``); the
    per-seat / 游资 detail (``stock_lhb_stock_detail_em``) is a separate shape,
    :class:`LhbSeatRow`.
    """

    code: str
    symbol: str
    name: str
    on_date: str
    provider: str
    reason: str = ""
    interpretation: str = ""
    change_pct: Optional[float] = None
    close_price: Optional[float] = None
    net_buy_amount: Optional[float] = None
    buy_amount: Optional[float] = None
    sell_amount: Optional[float] = None
    turnover_rate: Optional[float] = None
    circulating_mv: Optional[float] = None


@dataclass(frozen=True)
class FundFlowRow:
    """A single fund-flow ranking row (资金流排名) for one name or one board.

    A separate data shape from OHLCV — it captures the money-flow snapshot of
    one 个股 (individual stock) or one 板块 (industry / concept / region board)
    for a rolling window (``今日`` / ``3日`` / ``5日`` / ``10日``). ``code`` is the
    raw upstream code (empty for board rows, which carry no code); ``symbol`` is
    the canonical form for individual rows (empty for boards). ``name`` is the
    stock / board name. All net-flow amounts are in 元 and 净占比 / 涨跌幅 in %;
    every numeric is ``None`` when the upstream omits / can't-parse it — never
    silently coerced to 0. ``lead_stock`` (领涨股) is only present on board rows
    when the upstream supplies it. ``scope`` is ``"individual"`` or ``"sector"``;
    ``provider`` is the data source (``"akshare"``).
    """

    scope: str
    name: str
    provider: str
    code: str = ""
    symbol: str = ""
    latest_price: Optional[float] = None
    change_pct: Optional[float] = None
    main_net_amount: Optional[float] = None
    main_net_pct: Optional[float] = None
    super_large_net_amount: Optional[float] = None
    large_net_amount: Optional[float] = None
    medium_net_amount: Optional[float] = None
    small_net_amount: Optional[float] = None
    lead_stock: str = ""


@dataclass(frozen=True)
class LhbSeatRow:
    """A single 龙虎榜席位 (dragon-tiger trading-desk / 营业部) row for one symbol / day.

    This is the *per-seat* detail behind one name's 龙虎榜 appearance
    (``stock_lhb_stock_detail_em``) — distinct from :class:`LhbRow`, which is
    the *market-level* per-day board (``stock_lhb_detail_em``). Each row is one
    营业部 (brokerage sales desk) on either the 买入 (buy) or 卖出 (sell) side of
    a given name's board entry.

    ``seat_name`` is the 交易营业部名称 as reported upstream; ``side`` is
    ``"买入"`` or ``"卖出"``. All amounts are in 元 and percentages in %; every
    numeric is ``None`` when the upstream omits / can't-parse it — never
    silently coerced to 0 (per §错误可见性). ``seat_type`` is the upstream 类型
    column (机构专用 / 游资 / etc. when present).

    ``hot_money`` is a best-effort *static* label (游资名, e.g. 章盟主 / 赵老哥)
    assigned by substring-matching ``seat_name`` against the seat tag library
    (``doyoutrade/data/hot_money_seats.yaml``); it is ``None`` when no starter
    entry matched. That library is a non-authoritative starter set, so a
    ``None`` label means "not in our list", NOT "not a 游资". ``is_institution``
    is ``True`` when the seat is an 机构专用 (institutional) desk.

    ``symbol`` is the canonical Doyoutrade form (``600519.SH``); ``date`` is the
    ``YYYYMMDD`` trade date the board entry is for; ``provider`` is the data
    source (``"akshare"``).
    """

    side: str
    seat_name: str
    symbol: str
    date: str
    provider: str
    seat_type: str = ""
    buy_amount: Optional[float] = None
    sell_amount: Optional[float] = None
    net_amount: Optional[float] = None
    buy_pct: Optional[float] = None
    sell_pct: Optional[float] = None
    hot_money: Optional[str] = None
    is_institution: bool = False


@dataclass(frozen=True)
class ChipDistributionRow:
    """One day's 筹码分布 (chip distribution / 筹码集中度) snapshot for a symbol.

    A separate data shape from OHLCV / fundamentals — it summarizes how a
    name's outstanding shares are distributed across historical cost bases on
    a given ``date`` (upstream: akshare ``stock_cyq_em``, itself modeled on
    通达信's筹码分布). ``profit_ratio`` is the fraction of shares currently
    在成本之上 (獲利比例, 0-1); ``avg_cost`` is the volume-weighted average
    holding cost. ``cost_90_low``/``cost_90_high`` bound the cost range holding
    90% of chips (``concentration_90`` is that range's width as a fraction of
    ``avg_cost`` — smaller means more concentrated / 筹码集中); the ``_70``
    fields are the same at the tighter 70% band. Only A-share individual
    stocks have this upstream signal — ETFs, indices, and non-A-share markets
    return no rows, never a fabricated snapshot. Every numeric is ``None``
    when the upstream omits / can't-parse it — never silently coerced to 0
    (per §错误可见性). ``symbol`` is the canonical Doyoutrade form
    (``600519.SH``); ``date`` is ``YYYY-MM-DD``; ``provider`` is the data
    source (``"akshare"``).
    """

    symbol: str
    date: str
    provider: str
    profit_ratio: Optional[float] = None
    avg_cost: Optional[float] = None
    cost_90_low: Optional[float] = None
    cost_90_high: Optional[float] = None
    concentration_90: Optional[float] = None
    cost_70_low: Optional[float] = None
    cost_70_high: Optional[float] = None
    concentration_70: Optional[float] = None


@dataclass(frozen=True)
class EarningsForecast:
    """A single earnings preannouncement row (业绩预告) for a symbol.

    Earnings preannouncements are a separate data shape from OHLCV / news /
    research reports — they surface management's *forward* guidance on an
    upcoming report period, filed before the full financial statements.
    ``report_period`` is the fiscal quarter-end the guidance covers
    (``YYYYMMDD``, e.g. ``"20240930"``). ``preannounce_type`` is the Eastmoney
    classification (预告类型: 预增 / 略增 / 续盈 / 预减 / 续亏 / 首亏 / 扭亏 /
    预亏). ``forecast_indicator`` is the metric being guided (预测指标, usually
    净利润). Numeric fields are ``None`` when the upstream omits them;
    ``change_description`` carries the full free-text guidance sentence.
    ``announce_date`` is normalized to ``YYYY-MM-DD``.
    """

    symbol: str
    name: str
    report_period: str
    preannounce_type: str
    announce_date: str
    provider: str
    forecast_indicator: str = ""
    forecast_value: Optional[float] = None
    change_pct: Optional[float] = None
    prev_year_value: Optional[float] = None
    change_description: str = ""
    reason: str = ""


@dataclass(frozen=True)
class EarningsExpress:
    """A single earnings express report row (业绩快报) for a symbol.

    Earnings express reports are a separate data shape — they surface the
    *unaudited* headline numbers (营收 / 净利 / EPS / ROE) filed before the
    full audited statements, faster but less detailed. ``report_period`` is
    the fiscal quarter-end (``YYYYMMDD``). All numeric fields are ``None``
    when the upstream omits them (common for newly-listed names missing
    prior-year comparables). ``announce_date`` is normalized to
    ``YYYY-MM-DD``.
    """

    symbol: str
    name: str
    report_period: str
    announce_date: str
    provider: str
    eps: Optional[float] = None
    revenue: Optional[float] = None
    revenue_prev_yoy: Optional[float] = None
    revenue_qoq: Optional[float] = None
    net_profit: Optional[float] = None
    net_profit_prev_yoy: Optional[float] = None
    net_profit_qoq: Optional[float] = None
    navs_per_share: Optional[float] = None
    roe: Optional[float] = None
    industry: str = ""


@dataclass(frozen=True)
class SectorMember:
    """A single constituent stock of a sector / industry / concept board.

    Sector membership is a separate data shape from OHLCV — no interval /
    price fields. ``code`` is the canonical Doyoutrade symbol (e.g.
    ``600519.SH``); ``name`` is the stock short name when the source
    supplies it (empty otherwise). ``sector_name`` is the board the member
    belongs to; ``provider`` is the data source that served the row
    (``"akshare"`` / ``"qmt"``).
    """

    sector_name: str
    code: str
    name: str
    provider: str
    sector_type: str = ""


@dataclass(frozen=True)
class SectorHeatRow:
    """A single 题材 / 板块热度 (sector-heat) row for one industry / concept board.

    A separate data shape from :class:`SectorMember` (which lists a board's
    constituents) — this captures the board's *whole-board* market snapshot the
    same board-name endpoints (``stock_board_industry_name_em`` /
    ``stock_board_concept_name_em``) already return but that the membership
    provider throws away: the board-level 涨跌幅 (change), 总市值 (market cap),
    换手率 (turnover rate), 上涨/下跌家数 (advance/decline counts) and the
    领涨股票 (leader) + its 涨跌幅. Ranking boards by ``change_pct`` descending is
    a first-order read of where the day's 主线 (dominant theme) heat sits.

    ``board_name`` is the board short name; ``board_code`` is the upstream board
    code (empty when the upstream omits it). ``change_pct`` / ``turnover_rate`` /
    ``leader_change_pct`` are in %; ``total_mv`` is in 元 (100亿 = 1e10). Every
    numeric is ``None`` when the upstream omits / can't-parse it — never silently
    coerced to 0 (per §错误可见性). ``sector_type`` is ``"industry"`` or
    ``"concept"``; ``provider`` is the data source (``"akshare"``).
    """

    board_name: str
    sector_type: str
    provider: str
    board_code: str = ""
    change_pct: Optional[float] = None
    total_mv: Optional[float] = None
    turnover_rate: Optional[float] = None
    up_count: Optional[int] = None
    down_count: Optional[int] = None
    leader_stock: str = ""
    leader_change_pct: Optional[float] = None


@dataclass(frozen=True)
class Fundamentals:
    """Point-in-time valuation / size snapshot for one symbol.

    A separate data shape from OHLCV — no interval / price-series. Values
    are ``None`` when the source does not supply them (e.g. qmt derives
    ``float_mv`` from float shares × price but has no PE/PB). ``float_mv`` /
    ``total_mv`` are in **currency units** (元 for A-shares), so 100亿 is
    ``1e10``. ``provider`` is the data source that served the row
    (``"akshare"`` / ``"qmt"``).
    """

    code: str
    float_mv: float | None = None
    total_mv: float | None = None
    pe: float | None = None
    pb: float | None = None
    price: float | None = None
    asof: str = ""
    provider: str = ""


@dataclass(frozen=True)
class EventItem:
    """A calendar / status event attached to a symbol.

    ``event_type`` is one of ``"suspension"`` (停牌, currently halted) or
    ``"earnings_disclosure"`` (财报预约披露, an upcoming report date).
    ``event_date`` is ``YYYY-MM-DD`` (the suspend date, or the expected /
    actual disclosure date); empty when the source gives a status without a
    date. ``detail`` is a free-text note (停牌原因 etc.). ``provider`` is the
    data source (``"akshare"``).
    """

    code: str
    event_type: str
    event_date: str = ""
    detail: str = ""
    provider: str = ""


@dataclass
class Quote:
    symbol: str
    price: float
    timestamp: str


@dataclass
class QuoteSnapshot:
    """Realtime per-symbol quote snapshot surfaced to REST / WebSocket / frontend.

    This is the wire-shape contract consumed by the ``/market/quotes`` REST
    endpoint, the ``/ws/market/quotes`` WebSocket stream, and the frontend
    watchlist page. It is deliberately a flat, JSON-friendly bag of optional
    floats so a partially-known or unavailable quote is never silently dropped
    — instead it carries a ``status`` describing *why* fields are ``None``.

    ``price`` is the latest traded price (qmt ``last_price``); ``prev_close``
    is yesterday's close (qmt ``last_close`` / ``pre_close``). ``change`` and
    ``change_pct`` are derived (``price - prev_close`` and
    ``change / prev_close * 100``) and stay ``None`` when either input is
    missing or ``prev_close`` is non-positive. ``timestamp`` is the upstream
    tick time as a string.

    ``status`` is one of:
      * ``"ok"`` — a real quote was served.
      * ``"qmt_disconnected"`` — qmt-proxy is not connected; values are ``None``
        and the frontend should render ``—``.
      * ``"no_data"`` — qmt is connected but returned no tick for this symbol
        (unknown symbol); a placeholder so the symbol is not silently dropped.
      * ``"suspended"`` — the symbol is halted (停牌). Two sources, both yield
        ``price`` / ``change`` / ``change_pct`` ``= None`` (we refuse to derive a
        fake move) while keeping ``prev_close`` + limit prices; the frontend
        renders 停牌: (1) qmt returned a tick whose ``last_price`` is the
        halt sentinel (``<= 0``) — detected in ``quote_snapshot_from_tick``;
        (2) the quote-stream service overlaid it from the suspension event
        source (akshare) for a flat ``last_price == prev_close`` tick that would
        otherwise read as a benign 0% move.
    """

    symbol: str
    price: float | None = None
    prev_close: float | None = None
    change: float | None = None
    change_pct: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    amount: float | None = None
    timestamp: str | None = None
    status: str = "ok"
    # Order-book level-1 seal volumes (封单量) and computed A-share limit prices,
    # forwarded from the realtime QuoteData so intraday monitoring (涨停大减 /
    # 涨停打开 etc.) can judge board strength. ``bid_vol1`` is the buy-queue at
    # limit-up (涨停封单量); ``ask_vol1`` the sell-queue at limit-down (跌停封单量).
    # All optional/``None`` for producers that do not carry order-book data, so
    # the wire shape stays backward-additive.
    bid_vol1: int | None = None
    ask_vol1: int | None = None
    limit_up_price: float | None = None
    limit_down_price: float | None = None

    def to_dict(self) -> Dict[str, Any]:
        """Return the full wire shape as a plain dict (``None`` preserved as null).

        Every key is always present (Phase B / frontend rely on a stable
        shape); missing values are ``None`` rather than absent.
        """
        return {
            "symbol": self.symbol,
            "price": self.price,
            "prev_close": self.prev_close,
            "change": self.change,
            "change_pct": self.change_pct,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "volume": self.volume,
            "amount": self.amount,
            "timestamp": self.timestamp,
            "status": self.status,
            "bid_vol1": self.bid_vol1,
            "ask_vol1": self.ask_vol1,
            "limit_up_price": self.limit_up_price,
            "limit_down_price": self.limit_down_price,
        }


@dataclass
class AccountSnapshot:
    cash: Decimal
    equity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "cash", decimal_from_number(self.cash))
        object.__setattr__(self, "equity", decimal_from_number(self.equity))


@dataclass
class PositionSnapshot:
    symbol: str
    quantity: float
    cost_price: Decimal
    available: float | None = None
    market_price: float | None = None
    market_value: float | None = None
    name: str | None = None
    frozen: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "cost_price", decimal_from_number(self.cost_price))


@dataclass
class AssetSnapshot:
    """Richer broker asset breakdown (QMT ``AssetInfo``).

    Distinct from :class:`AccountSnapshot` (which carries only cash/equity that
    the worker cycle needs): this captures the frozen / available split and the
    realized profit fields that ``fetch_account`` deliberately drops. Consumed
    by the daily-review statement, never by the worker cycle path. Monetary
    fields are coerced to :class:`Decimal`; ``profit_loss_ratio`` is a plain
    ratio (already a fraction/percentage from the broker) and stays ``float``.
    """

    total_asset: Decimal
    market_value: Decimal
    cash: Decimal
    frozen_cash: Decimal
    available_cash: Decimal
    profit_loss: Decimal
    profit_loss_ratio: float

    def __post_init__(self) -> None:
        for attr in (
            "total_asset",
            "market_value",
            "cash",
            "frozen_cash",
            "available_cash",
            "profit_loss",
        ):
            object.__setattr__(self, attr, decimal_from_number(getattr(self, attr)))


@dataclass
class TradeSnapshot:
    """One executed broker trade line (QMT ``TradeInfo``) = a 成交/交割单 row.

    This is the broker's authoritative executed-trade record (includes manual
    trades made outside DoYouTrade), unlike ``trade_fills`` which only holds
    fills DoYouTrade itself executed. ``trade_time`` is kept as an ISO string so
    it survives JSON serialization into the review ``pre_data`` verbatim.
    """

    trade_id: str
    order_id: str
    symbol: str
    side: str
    quantity: int
    price: Decimal
    amount: Decimal
    trade_time: str
    commission: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", decimal_from_number(self.price))
        object.__setattr__(self, "amount", decimal_from_number(self.amount))
        object.__setattr__(self, "commission", decimal_from_number(self.commission))


@dataclass(frozen=True)
class TaskBudgetPositionUsage:
    """Per-symbol logical position owned by one task for budget accounting."""

    symbol: str
    quantity: int
    market_value: Decimal
    price: Decimal
    price_source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_value", decimal_from_number(self.market_value))
        object.__setattr__(self, "price", decimal_from_number(self.price))


@dataclass(frozen=True)
class TaskBudgetSnapshot:
    """Task-level budget view derived from the task's own persisted fills."""

    max_task_position_amount: Decimal | None = None
    max_task_position_ratio: float | None = None
    budget_cap: Decimal | None = None
    current_usage: Decimal = Decimal(0)
    remaining_budget: Decimal = Decimal(0)
    positions: tuple[TaskBudgetPositionUsage, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.max_task_position_amount is not None:
            object.__setattr__(
                self,
                "max_task_position_amount",
                decimal_from_number(self.max_task_position_amount),
            )
        if self.budget_cap is not None:
            object.__setattr__(self, "budget_cap", decimal_from_number(self.budget_cap))
        object.__setattr__(self, "current_usage", decimal_from_number(self.current_usage))
        object.__setattr__(self, "remaining_budget", decimal_from_number(self.remaining_budget))


@dataclass
class OrderIntent:
    intent_id: str
    symbol: str
    #: 复核结果：买或卖；**``amount`` 的含义仅按此字段解释**（买=名义金额，卖=股数）。
    action: Literal["buy", "sell"]
    #: 当 ``action`` 为 ``buy``：名义金额；为 ``sell``：股数。见 :meth:`quote_notional`。
    amount: Optional[float]
    order_type: str
    tif: str
    strategy_tag: str
    price_reference: float
    rationale: str
    #: Per-signal factor identifier copied from Signal.tag (set by
    #: PositionManager during compute_intents). The execution layer
    #: routes this onto TradeFillRecord.entry_tag (for buys) or
    #: TradeFillRecord.exit_tag (for sells). Distinct from
    #: ``strategy_tag`` (the strategy class name).
    signal_tag: str = ""
    #: Optional exit categorization copied from ``Signal.exit_reason`` (one of
    #: :class:`doyoutrade.strategy_sdk.signal.ExitReason`). Set only on SELL
    #: intents; ``None`` otherwise. The worker stamps it into the fill payload
    #: and persists it onto ``TradeFillRecord.exit_reason`` so closed-trade
    #: attribution (``by_exit_reason``) can group exits by kind.
    exit_reason: str | None = None

    def quote_notional_decimal(self) -> Decimal:
        """Same economics as :meth:`quote_notional`, but exact ``Decimal`` (no ``float`` product)."""
        if self.amount is None:
            return Decimal(0)
        if self.action == "buy":
            return decimal_from_number(self.amount)
        return decimal_from_number(self.amount) * decimal_from_number(self.price_reference)

    def quote_notional(self) -> float:
        """计价货币侧名义：由 ``action`` 决定；买为 ``amount``，卖为 ``amount * price_reference``.

        Prefer :meth:`quote_notional_decimal` for thresholds and persistence; this ``float`` may
        not be exactly representable in IEEE-754 for some decimal products.
        """
        return float(self.quote_notional_decimal())


def intent_to_json(intent: OrderIntent) -> str:
    """Serialize an :class:`OrderIntent` for persistence (approval resume).

    Flat JSON of the dataclass fields — all values are JSON-native (str / float
    / None). ``default=str`` is a defensive belt for any future non-native field.
    """
    return json.dumps(asdict(intent), ensure_ascii=False, default=str)


def intent_from_json(payload: str) -> OrderIntent:
    """Rebuild an :class:`OrderIntent` from :func:`intent_to_json` output.

    Tolerates schema drift by dropping unknown keys (a field removed since the
    row was written) but raises loudly (§错误可见性) when the payload is not a
    JSON object or a required field is missing — never silently fabricates a
    half-formed intent that would mis-dispatch.
    """
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError(
            f"intent payload must be a JSON object, got {type(data).__name__}: {data!r}"
        )
    field_names = {f.name for f in fields(OrderIntent)}
    kwargs = {key: value for key, value in data.items() if key in field_names}
    return OrderIntent(**kwargs)


def signal_context_from_intent_json(payload: str | None) -> dict[str, str]:
    """Pull DISPLAY-ONLY signal + order context from a persisted intent payload.

    Returns the "why" and "what" behind a held order — ``rationale`` /
    ``signal_tag`` / ``strategy_tag`` plus the order facts ``price_reference``
    (限价) / ``order_type`` / ``tif`` (有效期) / ``exit_reason`` — for the merged
    信号+审批 card rendered identically on the Feishu card and the web/Chat
    ``ApprovalQueueCard``. All values are strings (empty when absent) so the
    renderers stay dumb. Best-effort: a missing or malformed payload yields empty
    strings + a WARNING (never raises) so the approval card still renders — the
    AUTHORITATIVE dispatch path uses :func:`intent_from_json`, which DOES raise.
    Not for any decision logic (§金额十进制: never parse these back to numbers).
    """
    empty = {
        "rationale": "",
        "signal_tag": "",
        "strategy_tag": "",
        "price_reference": "",
        "order_type": "",
        "tif": "",
        "exit_reason": "",
    }
    if not payload:
        return dict(empty)
    try:
        data = json.loads(payload)
    except (ValueError, TypeError) as exc:
        logger.warning("intent payload unparseable for signal context: %s", exc)
        return dict(empty)
    if not isinstance(data, dict):
        logger.warning(
            "intent payload for signal context is not an object: %s", type(data).__name__
        )
        return dict(empty)

    def _s(key: str) -> str:
        value = data.get(key)
        return "" if value is None else str(value)

    return {
        "rationale": _s("rationale"),
        "signal_tag": _s("signal_tag"),
        "strategy_tag": _s("strategy_tag"),
        "price_reference": _s("price_reference"),
        "order_type": _s("order_type"),
        "tif": _s("tif"),
        "exit_reason": _s("exit_reason"),
    }


@dataclass
class ValidationResult:
    ok: bool
    error: str = ""


@dataclass
class RiskDecision:
    intent_id: str
    action: str
    reason: str = ""
    scaled_quantity: Optional[float] = None
    scaled_amount: Optional[float] = None


@dataclass
class FillRecord:
    intent_id: str
    symbol: str
    side: str
    quantity: float
    price: float
    #: Transaction fee (元) for this fill. Defaults to 0.0 so fee-unaware
    #: paths and the no-fee backtest are unchanged; populated by the ledger
    #: when a fee model is configured (see doyoutrade/execution/fees.py).
    fee: float = 0.0


@dataclass
class CycleReport:
    submitted_count: int
    vetoed_count: int
    pending_approval_count: int
    completed_phases: List[str]
    cycle_failed: bool = False
    failure_message: str = ""
    failure_error: Optional[dict[str, Any]] = None
    # Fills produced by this cycle (same payloads written to
    # ``cycle_runs.details.fills``). Carried on the report so the backtest
    # loop can collect them even in fast mode, where cycle_runs are not
    # persisted. Empty when the cycle produced no fills.
    fills: List[dict[str, Any]] = field(default_factory=list)
