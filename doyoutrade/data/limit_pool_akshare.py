"""Akshare-based A-share limit-up / down / broken-board breadth provider.

Wraps akshare's three 东方财富 打板 pool endpoints into the
:class:`doyoutrade.data.protocols.MarketBreadthProvider` contract:

* ``stock_zt_pool_em`` — 涨停池 (limit-up pool), carries 连板数 (streak).
* ``stock_zt_pool_dtgc_em`` — 跌停池 (limit-down pool).
* ``stock_zt_pool_zbgc_em`` — 炸板池 (broken-board pool).

All three require an explicit ``date=YYYYMMDD`` — akshare defaults to a
stale date otherwise — and must be a real trading day. On a non-trading
day / before the after-hours snapshot updates they may return an empty
DataFrame *or* raise.

Failure-mode discipline (per CLAUDE.md §错误可见性):

* A *persistent* per-pool upstream failure (all retries exhausted) is
  **not** silently swallowed — it is recorded on
  ``MarketBreadth.pool_errors`` (pool name → ``ExcType: message``) and
  logged at WARNING, so the ``data_market_breadth`` tool can report a
  ``partial`` status naming the failed pool instead of under-counting in
  silence. If *every* pool fails, the tool maps the empty aggregate +
  populated ``pool_errors`` to ``market_breadth_fetch_failed``.
* A genuinely *empty* day (all three pools returned nothing, no errors)
  comes back with three empty lists → the tool maps that to a distinct
  ``market_breadth_empty``.
* Row-level parse failures are dropped **loudly** (``logger.info`` with
  the raw code) rather than silently, and numeric fields that can't be
  parsed become ``None`` (never an ``int(脏值)`` truncation).

Both paths are observable: the ``data.akshare.fetch_market_breadth`` OTel
span + ``data_provider.fetch_market_breadth`` debug event always fire
(carrying the trade date and the three pool counts), and retries log at
WARNING with the attempt number.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import List, Optional

import akshare as ak

from doyoutrade.core.models import LimitPoolStock, MarketBreadth
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# akshare column names per pool (东方财富).
# ---------------------------------------------------------------------------
# 涨停池 stock_zt_pool_em:
#   序号,代码,名称,涨跌幅,最新价,成交额,流通市值,总市值,换手率,封板资金,
#   首次封板时间,最后封板时间,炸板次数,涨停统计,连板数,所属行业
# 跌停池 stock_zt_pool_dtgc_em:
#   序号,代码,名称,涨跌幅,最新价,成交额,流通市值,总市值,动态市盈率,换手率,
#   封单资金,最后封板时间,板上成交额,连续跌停,开板次数,所属行业
# 炸板池 stock_zt_pool_zbgc_em:
#   序号,代码,名称,涨跌幅,最新价,涨停价,成交额,流通市值,总市值,换手率,涨速,
#   首次封板时间,炸板次数,涨停统计,振幅,所属行业
_COL_CODE = "代码"
_COL_NAME = "名称"
_COL_CHANGE_PCT = "涨跌幅"
_COL_LATEST_PRICE = "最新价"
_COL_TURNOVER = "成交额"
_COL_CIRC_MV = "流通市值"
_COL_TOTAL_MV = "总市值"
_COL_TURNOVER_RATE = "换手率"
_COL_INDUSTRY = "所属行业"
_COL_STREAK = "连板数"
_COL_BROKEN_COUNT = "炸板次数"
_COL_FIRST_SEAL = "首次封板时间"
_COL_LAST_SEAL = "最后封板时间"

_POOL_LIMIT_UP = "limit_up"
_POOL_LIMIT_DOWN = "limit_down"
_POOL_BROKEN_BOARD = "broken_board"

_MAX_ATTEMPTS = 3


class AkshareMarketBreadthProvider:
    """A-share limit-up / down / broken-board pool source backed by akshare."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        # Breadth pools have no interval / adjust axis; an empty interval
        # set keeps the capabilities shape uniform with OHLCV providers
        # without claiming bar support.
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        is_realtime_capable=False,
        max_history_years=None,
    )

    async def fetch_market_breadth(self, trade_date: str) -> MarketBreadth:
        with data_span("akshare", "fetch_market_breadth"):
            breadth = await asyncio.to_thread(self._sync_fetch_market_breadth, trade_date)
        _emit_fetch_market_breadth_event(breadth)
        return breadth

    def _sync_fetch_market_breadth(self, trade_date: str) -> MarketBreadth:
        pool_errors: dict[str, str] = {}

        limit_up = self._fetch_pool(
            ak.stock_zt_pool_em, trade_date, _POOL_LIMIT_UP, pool_errors
        )
        limit_down = self._fetch_pool(
            ak.stock_zt_pool_dtgc_em, trade_date, _POOL_LIMIT_DOWN, pool_errors
        )
        broken_board = self._fetch_pool(
            ak.stock_zt_pool_zbgc_em, trade_date, _POOL_BROKEN_BOARD, pool_errors
        )

        ladder, max_streak = _build_ladder(limit_up)
        broken_rate = _broken_board_rate(len(limit_up), len(broken_board))

        return MarketBreadth(
            trade_date=trade_date,
            provider=PROVIDER_NAME_AKSHARE,
            limit_up=limit_up,
            limit_down=limit_down,
            broken_board=broken_board,
            ladder=ladder,
            max_streak=max_streak,
            broken_board_rate=broken_rate,
            pool_errors=pool_errors,
        )

    def _fetch_pool(
        self,
        ak_fn,
        trade_date: str,
        pool: str,
        pool_errors: dict[str, str],
    ) -> List[LimitPoolStock]:
        """Fetch and parse one pool, recording persistent failures visibly.

        Returns the parsed rows (possibly empty on an empty upstream frame).
        On a *persistent* upstream failure we record ``pool_errors[pool]``
        (never silently swallow) and return ``[]`` so the other pools still
        aggregate — the tool reads ``pool_errors`` to decide partial vs empty
        vs fetch-failed.
        """
        df = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                df = ak_fn(date=trade_date)
                break
            except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
                logger.warning(
                    "akshare %s pool=%s date=%s failed (attempt %d/%d): %s: %s",
                    getattr(ak_fn, "__name__", "<pool_fn>"),
                    pool, trade_date, attempt + 1, _MAX_ATTEMPTS,
                    type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "akshare %s pool=%s date=%s gave up: %s: %s",
                        getattr(ak_fn, "__name__", "<pool_fn>"),
                        pool, trade_date, type(exc).__name__, exc,
                    )
                    pool_errors[pool] = f"{type(exc).__name__}: {exc}"
                    return []
                time.sleep(0.8 * (attempt + 1))

        if df is None or df.empty:
            logger.info(
                "akshare pool=%s date=%s returned no rows", pool, trade_date
            )
            return []

        rows: List[LimitPoolStock] = []
        for _, row in df.iterrows():
            code = _clean_str(row.get(_COL_CODE))
            if not code:
                # A row with no code can't be identified; skip it loudly
                # rather than emitting a phantom stock.
                logger.info(
                    "limit_pool row skipped pool=%s date=%s reason=missing_code raw=%r",
                    pool, trade_date, dict(row),
                )
                continue
            rows.append(
                LimitPoolStock(
                    pool=pool,
                    code=code,
                    symbol=_canonical(code),
                    name=_clean_str(row.get(_COL_NAME)),
                    provider=PROVIDER_NAME_AKSHARE,
                    change_pct=_to_float(row.get(_COL_CHANGE_PCT)),
                    latest_price=_to_float(row.get(_COL_LATEST_PRICE)),
                    turnover=_to_float(row.get(_COL_TURNOVER)),
                    circulating_mv=_to_float(row.get(_COL_CIRC_MV)),
                    total_mv=_to_float(row.get(_COL_TOTAL_MV)),
                    turnover_rate=_to_float(row.get(_COL_TURNOVER_RATE)),
                    industry=_clean_str(row.get(_COL_INDUSTRY)),
                    # 连板数 only exists in the limit-up pool.
                    streak=_to_int(row.get(_COL_STREAK)) if pool == _POOL_LIMIT_UP else None,
                    broken_board_count=_to_int(row.get(_COL_BROKEN_COUNT)),
                    first_seal_time=_clean_str(row.get(_COL_FIRST_SEAL)),
                    last_seal_time=_clean_str(row.get(_COL_LAST_SEAL)),
                )
            )
        return rows


def _build_ladder(limit_up: List[LimitPoolStock]) -> tuple[dict[str, int], int]:
    """Aggregate the limit-up pool's 连板数 into a {height: count} ladder.

    Rows whose ``streak`` could not be parsed (``None``) are excluded from
    the ladder (they were already logged loudly when parsed) — they must
    not be silently counted as height 0 or as 1-board. ``max_streak`` is 0
    when the ladder is empty.
    """
    counter: Counter[int] = Counter()
    for stock in limit_up:
        if stock.streak is None or stock.streak <= 0:
            continue
        counter[stock.streak] += 1
    ladder = {str(height): counter[height] for height in sorted(counter)}
    max_streak = max(counter) if counter else 0
    return ladder, max_streak


def _broken_board_rate(limit_up_count: int, broken_board_count: int) -> float:
    """炸板率 = 炸板 / (涨停 + 炸板); 0.0 when the denominator is 0."""
    denom = limit_up_count + broken_board_count
    if denom <= 0:
        return 0.0
    return broken_board_count / denom


def _emit_fetch_market_breadth_event(breadth: MarketBreadth) -> None:
    _fire_event(
        "data_provider.fetch_market_breadth",
        {
            "provider": PROVIDER_NAME_AKSHARE,
            "method": "fetch_market_breadth",
            "trade_date": breadth.trade_date,
            "limit_up_count": breadth.limit_up_count,
            "limit_down_count": breadth.limit_down_count,
            "broken_board_count": breadth.broken_board_count,
            "max_streak": breadth.max_streak,
            "pool_errors": sorted(breadth.pool_errors.keys()),
        },
    )


def _fire_event(event_name: str, payload: dict) -> None:
    """Fire emit_debug_event as a fire-and-forget task from a sync/async context."""
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        # No running event loop; skip.
        pass


def _canonical(bare_code: str) -> str:
    """Best-effort canonicalization of a bare 6-digit code to CODE.EXCHANGE.

    A-share suffix rules: 6/9 → SH, 0/2/3 → SZ, 4/8 → BJ. When unsure,
    default to SH. Mirrors ``earnings_akshare._canonical`` so the canonical
    form stays consistent across the data layer.
    """
    if not bare_code:
        return bare_code
    first = bare_code[0]
    if first in ("6", "9"):
        return f"{bare_code}.SH"
    if first in ("0", "2", "3"):
        return f"{bare_code}.SZ"
    if first in ("4", "8"):
        return f"{bare_code}.BJ"
    return f"{bare_code}.SH"


def _clean_str(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    # pandas NaN stringifies to "nan"; treat as empty.
    return "" if text.lower() == "nan" else text


def _to_float(value) -> Optional[float]:
    text = _clean_str(value)
    if not text:
        return None
    try:
        f = float(text)
    except (TypeError, ValueError):
        logger.info("limit_pool numeric skipped reason=unparseable_float raw=%r", value)
        return None
    if f != f:  # NaN guard — must NOT silently mask a schema violation.
        return None
    return f


def _to_int(value) -> Optional[int]:
    """Parse an integer-valued column, returning ``None`` (not 0) on failure.

    Returning ``None`` rather than a 0 fallback keeps a missing / unparseable
    连板数 out of the ladder instead of manufacturing a phantom height-0 rung
    (per §错误可见性: no ``int(脏值)`` truncation).
    """
    f = _to_float(value)
    if f is None:
        return None
    return int(f)


__all__ = ["AkshareMarketBreadthProvider"]
