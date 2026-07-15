"""Akshare-based A-share 龙虎榜 (dragon-tiger board) provider.

Wraps akshare's ``stock_lhb_detail_em`` (东方财富 龙虎榜详情) into the
:class:`doyoutrade.data.protocols.DragonTigerProvider` contract. The upstream
takes an inclusive ``start_date`` / ``end_date`` range (``YYYYMMDD``) and
returns every name that made the exchange's daily 龙虎榜 (large-order /
abnormal-move disclosure list) inside the window. Unlike OHLCV this is a
*market-level* list per day, not a per-symbol series.

Failure-mode discipline (per CLAUDE.md §错误可见性):

* A *persistent* upstream failure (all retries exhausted) re-raises the last
  exception so the ``data_lhb`` tool can surface a distinct
  ``lhb_fetch_failed`` error_code with the exception type.
* A genuinely *empty* window (no name made the board, or the after-hours
  snapshot hasn't updated) returns ``[]`` — the tool maps that to a distinct
  ``lhb_empty`` (a different failure mode than a fetch error).
* Row-level parse failures (a row with no code) are dropped **loudly**
  (``logger.info`` with the raw row) rather than silently, and numeric fields
  that can't be parsed become ``None`` (never an ``int(脏值)`` truncation).

Both paths are observable: the ``data.akshare.fetch_dragon_tiger`` OTel span +
``data_provider.fetch_dragon_tiger`` debug event always fire (carrying the
window and returned count), and retries log at WARNING with the attempt number.

The per-seat / 游资 detail mode (``stock_lhb_stock_detail_em``) is implemented
by :meth:`AkshareDragonTigerProvider.fetch_seat_detail`: it pulls the 买入 and
卖出 席位 for one name on one day and tags each seat with a best-effort 游资名
(from :mod:`doyoutrade.data.hot_money_seats`). Its extra failure-mode split: a
name that did NOT make the board that day makes akshare internally index a
``None`` frame and raise ``TypeError: 'NoneType' object is not subscriptable``
— that specific "no seat data" condition is caught and re-raised as the
distinct :class:`LhbNoSeatDataError` so the tool can surface
``lhb_no_seat_data`` separately from ``lhb_fetch_failed``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

import akshare as ak

from doyoutrade.core.models import LhbRow, LhbSeatRow
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.hot_money_seats import is_institution_seat, match_hot_money
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities

logger = logging.getLogger(__name__)


class LhbNoSeatDataError(Exception):
    """The requested name did not make the 龙虎榜 on the requested day.

    Distinct from a transport / upstream failure: akshare's
    ``stock_lhb_stock_detail_em`` internally subscripts a ``None`` frame when a
    symbol has no board entry for the date, raising a bare ``TypeError``. We
    catch **only** that shape and re-raise this so the ``data_lhb`` tool maps it
    to a distinct ``lhb_no_seat_data`` error_code (confirm the name actually
    上榜 that day) rather than the generic ``lhb_fetch_failed``.
    """

# akshare ``stock_lhb_detail_em`` column names (东方财富 龙虎榜详情):
#   序号,代码,名称,上榜日,解读,收盘价,涨跌幅,龙虎榜净买额,龙虎榜买入额,
#   龙虎榜卖出额,龙虎榜成交额,市场总成交额,净买额占总成交比,成交额占总成交比,
#   换手率,流通市值,上榜原因,上榜后1日,上榜后2日,上榜后5日,上榜后10日
_COL_CODE = "代码"
_COL_NAME = "名称"
_COL_ON_DATE = "上榜日"
_COL_INTERPRETATION = "解读"
_COL_CLOSE = "收盘价"
_COL_CHANGE_PCT = "涨跌幅"
_COL_NET_BUY = "龙虎榜净买额"
_COL_BUY = "龙虎榜买入额"
_COL_SELL = "龙虎榜卖出额"
_COL_TURNOVER_RATE = "换手率"
_COL_CIRC_MV = "流通市值"
_COL_REASON = "上榜原因"

# akshare ``stock_lhb_stock_detail_em`` column names (个股龙虎榜席位明细):
#   序号,交易营业部名称,买入金额,买入金额-占总成交比例,卖出金额,
#   卖出金额-占总成交比例,净额,类型
_SEAT_COL_NAME = "交易营业部名称"
_SEAT_COL_BUY = "买入金额"
_SEAT_COL_BUY_PCT = "买入金额-占总成交比例"
_SEAT_COL_SELL = "卖出金额"
_SEAT_COL_SELL_PCT = "卖出金额-占总成交比例"
_SEAT_COL_NET = "净额"
_SEAT_COL_TYPE = "类型"

# akshare's ``flag`` argument selects the buy vs sell side of the 席位榜.
_SEAT_FLAG_BUY = "买入"
_SEAT_FLAG_SELL = "卖出"

_MAX_ATTEMPTS = 3


def _strip_exchange_suffix(symbol: str) -> str:
    """Drop a ``.SH`` / ``.SZ`` / ``.BJ`` suffix, returning the bare 6-digit code.

    akshare's ``stock_lhb_stock_detail_em`` wants the bare code (``"000788"``),
    not the canonical ``CODE.EXCHANGE`` form. We only split on the FIRST dot so
    a malformed input isn't silently mangled beyond recognition.
    """
    return symbol.split(".", 1)[0].strip()


class AkshareDragonTigerProvider:
    """A-share 龙虎榜 (market-level, per-day board) source backed by akshare."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        # 龙虎榜 has no interval / adjust axis; an empty interval set keeps the
        # capabilities shape uniform with OHLCV providers without claiming
        # bar support.
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        is_realtime_capable=False,
        max_history_years=None,
    )

    async def fetch_dragon_tiger(
        self, start_date: str, end_date: str
    ) -> List[LhbRow]:
        with data_span("akshare", "fetch_dragon_tiger"):
            rows = await asyncio.to_thread(
                self._sync_fetch_dragon_tiger, start_date, end_date
            )
        _emit_fetch_dragon_tiger_event(start_date, end_date, len(rows))
        return rows

    def _sync_fetch_dragon_tiger(
        self, start_date: str, end_date: str
    ) -> List[LhbRow]:
        df = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                df = ak.stock_lhb_detail_em(
                    start_date=start_date, end_date=end_date
                )
                break
            except Exception as exc:  # noqa: BLE001 — re-raised below after retries
                logger.warning(
                    "akshare stock_lhb_detail_em failed [%s..%s] (attempt %d/%d): %s: %s",
                    start_date, end_date, attempt + 1, _MAX_ATTEMPTS,
                    type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "akshare stock_lhb_detail_em gave up [%s..%s]: %s: %s",
                        start_date, end_date, type(exc).__name__, exc,
                    )
                    raise
                time.sleep(0.8 * (attempt + 1))

        if df is None or df.empty:
            logger.info(
                "akshare stock_lhb_detail_em returned no rows [%s..%s]",
                start_date, end_date,
            )
            return []

        rows: List[LhbRow] = []
        for _, row in df.iterrows():
            code = _clean_str(row.get(_COL_CODE))
            if not code:
                # A row with no code can't be identified; skip it loudly
                # rather than emitting a phantom entry.
                logger.info(
                    "lhb row skipped [%s..%s] reason=missing_code raw=%r",
                    start_date, end_date, dict(row),
                )
                continue
            rows.append(
                LhbRow(
                    code=code,
                    symbol=_canonical(code),
                    name=_clean_str(row.get(_COL_NAME)),
                    on_date=_clean_str(row.get(_COL_ON_DATE)),
                    provider=PROVIDER_NAME_AKSHARE,
                    reason=_clean_str(row.get(_COL_REASON)),
                    interpretation=_clean_str(row.get(_COL_INTERPRETATION)),
                    change_pct=_to_float(row.get(_COL_CHANGE_PCT)),
                    close_price=_to_float(row.get(_COL_CLOSE)),
                    net_buy_amount=_to_float(row.get(_COL_NET_BUY)),
                    buy_amount=_to_float(row.get(_COL_BUY)),
                    sell_amount=_to_float(row.get(_COL_SELL)),
                    turnover_rate=_to_float(row.get(_COL_TURNOVER_RATE)),
                    circulating_mv=_to_float(row.get(_COL_CIRC_MV)),
                )
            )
        return rows

    async def fetch_seat_detail(
        self, symbol: str, date: str
    ) -> List[LhbSeatRow]:
        with data_span("akshare", "fetch_seat_detail"):
            rows = await asyncio.to_thread(
                self._sync_fetch_seat_detail, symbol, date
            )
        _emit_fetch_seat_detail_event(symbol, date, len(rows))
        return rows

    def _sync_fetch_seat_detail(
        self, symbol: str, date: str
    ) -> List[LhbSeatRow]:
        bare_code = _strip_exchange_suffix(symbol)
        rows: List[LhbSeatRow] = []
        rows.extend(
            self._fetch_one_seat_side(bare_code, symbol, date, _SEAT_FLAG_BUY)
        )
        rows.extend(
            self._fetch_one_seat_side(bare_code, symbol, date, _SEAT_FLAG_SELL)
        )
        return rows

    def _fetch_one_seat_side(
        self, bare_code: str, symbol: str, date: str, flag: str
    ) -> List[LhbSeatRow]:
        """Fetch one buy/sell side of the 席位榜 with retry + no-seat-data split.

        akshare raises a bare ``TypeError: 'NoneType' object is not
        subscriptable`` when the name has no board entry for the day (it
        indexes an internal ``None`` frame). That specific shape is NOT a
        transport failure, so it is re-raised as :class:`LhbNoSeatDataError`
        (mapped to ``lhb_no_seat_data``) instead of being retried into
        ``lhb_fetch_failed``. Every other exception keeps the market-level
        retry / re-raise discipline.
        """
        side = "买入" if flag == _SEAT_FLAG_BUY else "卖出"
        df = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                df = ak.stock_lhb_stock_detail_em(
                    symbol=bare_code, date=date, flag=flag
                )
                break
            except TypeError as exc:
                # akshare's own None-subscript when the name isn't on the board
                # that day. Distinct condition — do NOT retry, do NOT bucket as
                # a transport failure.
                if _is_no_seat_data_typeerror(exc):
                    logger.info(
                        "akshare stock_lhb_stock_detail_em: %s not on board "
                        "date=%s side=%s (no seat data): %s",
                        bare_code, date, side, exc,
                    )
                    raise LhbNoSeatDataError(
                        f"{symbol} has no 龙虎榜席位 for date={date} side={side}"
                    ) from exc
                # A different TypeError is a genuine bug/upstream change; treat
                # it like any other failure (loud + retry + re-raise).
                logger.warning(
                    "akshare stock_lhb_stock_detail_em TypeError %s date=%s "
                    "side=%s (attempt %d/%d): %s",
                    bare_code, date, side, attempt + 1, _MAX_ATTEMPTS, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "akshare stock_lhb_stock_detail_em gave up %s date=%s "
                        "side=%s: %s: %s",
                        bare_code, date, side, type(exc).__name__, exc,
                    )
                    raise
                time.sleep(0.8 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001 — re-raised below after retries
                logger.warning(
                    "akshare stock_lhb_stock_detail_em failed %s date=%s "
                    "side=%s (attempt %d/%d): %s: %s",
                    bare_code, date, side, attempt + 1, _MAX_ATTEMPTS,
                    type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "akshare stock_lhb_stock_detail_em gave up %s date=%s "
                        "side=%s: %s: %s",
                        bare_code, date, side, type(exc).__name__, exc,
                    )
                    raise
                time.sleep(0.8 * (attempt + 1))

        if df is None or df.empty:
            logger.info(
                "akshare stock_lhb_stock_detail_em returned no rows %s date=%s "
                "side=%s",
                bare_code, date, side,
            )
            return []

        canonical = _canonical(bare_code)
        rows: List[LhbSeatRow] = []
        for _, row in df.iterrows():
            seat_name = _clean_str(row.get(_SEAT_COL_NAME))
            if not seat_name:
                # A seat row with no 营业部名称 can't be identified; skip loudly.
                logger.info(
                    "lhb seat row skipped %s date=%s side=%s "
                    "reason=missing_seat_name raw=%r",
                    bare_code, date, side, dict(row),
                )
                continue
            seat_type = _clean_str(row.get(_SEAT_COL_TYPE))
            rows.append(
                LhbSeatRow(
                    side=side,
                    seat_name=seat_name,
                    symbol=canonical,
                    date=date,
                    provider=PROVIDER_NAME_AKSHARE,
                    seat_type=seat_type,
                    buy_amount=_to_float(row.get(_SEAT_COL_BUY)),
                    sell_amount=_to_float(row.get(_SEAT_COL_SELL)),
                    net_amount=_to_float(row.get(_SEAT_COL_NET)),
                    buy_pct=_to_float(row.get(_SEAT_COL_BUY_PCT)),
                    sell_pct=_to_float(row.get(_SEAT_COL_SELL_PCT)),
                    hot_money=match_hot_money(seat_name),
                    is_institution=is_institution_seat(seat_name, seat_type),
                )
            )
        return rows


def _emit_fetch_dragon_tiger_event(
    start_date: str, end_date: str, row_count: int
) -> None:
    _fire_event(
        "data_provider.fetch_dragon_tiger",
        {
            "provider": PROVIDER_NAME_AKSHARE,
            "method": "fetch_dragon_tiger",
            "start_date": start_date,
            "end_date": end_date,
            "row_count": row_count,
        },
    )


def _emit_fetch_seat_detail_event(
    symbol: str, date: str, row_count: int
) -> None:
    _fire_event(
        "data_provider.fetch_seat_detail",
        {
            "provider": PROVIDER_NAME_AKSHARE,
            "method": "fetch_seat_detail",
            "symbol": symbol,
            "date": date,
            "row_count": row_count,
        },
    )


def _is_no_seat_data_typeerror(exc: TypeError) -> bool:
    """True when a ``TypeError`` is akshare's "name not on board" None-subscript.

    akshare raises ``TypeError: 'NoneType' object is not subscriptable`` when
    ``stock_lhb_stock_detail_em`` finds no board entry for the (symbol, date).
    We match on that message shape so an *unrelated* ``TypeError`` (a genuine
    upstream / library bug) is NOT misclassified as "no seat data" — it stays a
    real failure that retries and surfaces ``lhb_fetch_failed``.
    """
    message = str(exc)
    return "NoneType" in message and "subscriptable" in message


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
    default to SH. Mirrors ``limit_pool_akshare._canonical`` so the canonical
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
        logger.info("lhb numeric skipped reason=unparseable_float raw=%r", value)
        return None
    if f != f:  # NaN guard — must NOT silently mask a schema violation.
        return None
    return f


__all__ = ["AkshareDragonTigerProvider", "LhbNoSeatDataError"]
