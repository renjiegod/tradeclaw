"""Akshare-based A-share 资金流排名 (fund-flow ranking) provider.

Wraps akshare's two fund-flow ranking endpoints into the
:class:`doyoutrade.data.protocols.FundFlowProvider` contract:

* ``stock_individual_fund_flow_rank`` — per-stock ranking (个股资金流排名).
* ``stock_sector_fund_flow_rank`` — per-board ranking (板块资金流排名).

Neither endpoint takes a date — the window is the rolling ``indicator``
(``今日`` / ``3日`` / ``5日`` / ``10日``). Two shape quirks the provider handles:

* **Individual columns are period-prefixed** — e.g. ``今日主力净流入-净额`` for
  ``今日`` but ``5日主力净流入-净额`` for ``5日``. The provider matches columns by
  **substring** (``主力净流入-净额`` etc.), never by the exact prefixed name, so
  swapping the period doesn't break parsing.
* **Sector columns were not confirmed online** — the provider matches the same
  substrings and tolerates missing columns: a substring with no matching column
  yields ``None`` on the row rather than raising.

Failure-mode discipline (per CLAUDE.md §错误可见性):

* A *persistent* upstream failure (all retries exhausted; the ``今日`` endpoint
  intermittently ``RemoteDisconnected``) re-raises the last exception so the
  ``data_fund_flow`` tool can surface a distinct ``fund_flow_fetch_failed``
  error_code with the exception type.
* A genuinely *empty* result returns ``[]`` — the tool maps that to a distinct
  ``fund_flow_empty``.
* A row with no identifiable name is dropped **loudly** (``logger.info``); every
  numeric that can't be parsed becomes ``None`` (never an ``int(脏值)``
  truncation).

Both paths are observable: the ``data.akshare.fetch_fund_flow`` OTel span +
``data_provider.fetch_fund_flow`` debug event always fire (carrying scope /
period / returned count), and retries log at WARNING with the attempt number.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, List, Optional

import akshare as ak

from doyoutrade.core.models import FundFlowRow
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities

logger = logging.getLogger(__name__)

_SCOPE_INDIVIDUAL = "individual"
_SCOPE_SECTOR = "sector"

# Substrings matched against upstream columns (period prefix stripped). Order
# only matters for readability; each maps a FundFlowRow field to the *first*
# upstream column whose name contains the substring.
_SUB_NAME = "名称"
_SUB_CODE = "代码"
_SUB_LATEST_PRICE = "最新价"
_SUB_CHANGE_PCT = "涨跌幅"
_SUB_MAIN_NET_AMOUNT = "主力净流入-净额"
_SUB_MAIN_NET_PCT = "主力净流入-净占比"
_SUB_SUPER_LARGE_NET = "超大单净流入-净额"
_SUB_LARGE_NET = "大单净流入-净额"
_SUB_MEDIUM_NET = "中单净流入-净额"
_SUB_SMALL_NET = "小单净流入-净额"
_SUB_LEAD_STOCK = "领涨股"

_MAX_ATTEMPTS = 3


class AkshareFundFlowProvider:
    """A-share 资金流排名 (individual + sector) source backed by akshare."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        # Fund-flow rankings have no interval / adjust axis; an empty interval
        # set keeps the capabilities shape uniform with OHLCV providers.
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        is_realtime_capable=False,
        max_history_years=None,
    )

    async def fetch_fund_flow(
        self,
        scope: str,
        period: str,
        *,
        sector_type: str | None = None,
    ) -> List[FundFlowRow]:
        with data_span("akshare", "fetch_fund_flow"):
            rows = await asyncio.to_thread(
                self._sync_fetch_fund_flow, scope, period, sector_type
            )
        _emit_fetch_fund_flow_event(scope, period, sector_type, len(rows))
        return rows

    def _sync_fetch_fund_flow(
        self,
        scope: str,
        period: str,
        sector_type: str | None,
    ) -> List[FundFlowRow]:
        if scope == _SCOPE_INDIVIDUAL:
            call: Callable = lambda: ak.stock_individual_fund_flow_rank(indicator=period)
            desc = f"stock_individual_fund_flow_rank(indicator={period})"
        elif scope == _SCOPE_SECTOR:
            call = lambda: ak.stock_sector_fund_flow_rank(
                indicator=period, sector_type=sector_type
            )
            desc = (
                f"stock_sector_fund_flow_rank(indicator={period}, "
                f"sector_type={sector_type})"
            )
        else:
            # Scope is validated in the tool layer; reaching here is a schema
            # violation, not a tolerated fallback.
            raise ValueError(
                f"fetch_fund_flow got unknown scope {scope!r}; "
                "expected 'individual' or 'sector'"
            )

        df = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                df = call()
                break
            except Exception as exc:  # noqa: BLE001 — re-raised below after retries
                logger.warning(
                    "akshare %s failed (attempt %d/%d): %s: %s",
                    desc, attempt + 1, _MAX_ATTEMPTS, type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "akshare %s gave up: %s: %s",
                        desc, type(exc).__name__, exc,
                    )
                    raise
                time.sleep(0.8 * (attempt + 1))

        if df is None or df.empty:
            logger.info("akshare %s returned no rows", desc)
            return []

        # Resolve each substring to a concrete upstream column *once* per call
        # (period prefixes shift, so this must not be hard-coded).
        columns = list(df.columns)
        col_of = {sub: _first_col_containing(columns, sub) for sub in (
            _SUB_NAME, _SUB_CODE, _SUB_LATEST_PRICE, _SUB_CHANGE_PCT,
            _SUB_MAIN_NET_AMOUNT, _SUB_MAIN_NET_PCT, _SUB_SUPER_LARGE_NET,
            _SUB_LARGE_NET, _SUB_MEDIUM_NET, _SUB_SMALL_NET, _SUB_LEAD_STOCK,
        )}

        rows: List[FundFlowRow] = []
        for _, row in df.iterrows():
            name = _cell_str(row, col_of[_SUB_NAME])
            if not name:
                logger.info(
                    "fund_flow row skipped scope=%s period=%s reason=missing_name raw=%r",
                    scope, period, dict(row),
                )
                continue
            code = _cell_str(row, col_of[_SUB_CODE])
            symbol = _canonical(code) if (scope == _SCOPE_INDIVIDUAL and code) else ""
            rows.append(
                FundFlowRow(
                    scope=scope,
                    name=name,
                    provider=PROVIDER_NAME_AKSHARE,
                    code=code,
                    symbol=symbol,
                    latest_price=_cell_float(row, col_of[_SUB_LATEST_PRICE]),
                    change_pct=_cell_float(row, col_of[_SUB_CHANGE_PCT]),
                    main_net_amount=_cell_float(row, col_of[_SUB_MAIN_NET_AMOUNT]),
                    main_net_pct=_cell_float(row, col_of[_SUB_MAIN_NET_PCT]),
                    super_large_net_amount=_cell_float(row, col_of[_SUB_SUPER_LARGE_NET]),
                    large_net_amount=_cell_float(row, col_of[_SUB_LARGE_NET]),
                    medium_net_amount=_cell_float(row, col_of[_SUB_MEDIUM_NET]),
                    small_net_amount=_cell_float(row, col_of[_SUB_SMALL_NET]),
                    lead_stock=_cell_str(row, col_of[_SUB_LEAD_STOCK]),
                )
            )
        return rows


def _first_col_containing(columns: List[str], substring: str) -> Optional[str]:
    """Return the first column whose name contains ``substring`` (else None).

    The ``主力净流入-净额`` substring is contained in ``今日主力净流入-净额`` /
    ``5日主力净流入-净额`` / etc., so matching by substring survives the
    period-prefix drift. A None result means the upstream omitted the column
    entirely — the caller stores ``None`` on the row rather than raising.
    """
    for col in columns:
        if substring in str(col):
            return col
    return None


def _cell_str(row, col: Optional[str]) -> str:
    if col is None:
        return ""
    return _clean_str(row.get(col))


def _cell_float(row, col: Optional[str]) -> Optional[float]:
    if col is None:
        return None
    return _to_float(row.get(col))


def _emit_fetch_fund_flow_event(
    scope: str, period: str, sector_type: str | None, row_count: int
) -> None:
    _fire_event(
        "data_provider.fetch_fund_flow",
        {
            "provider": PROVIDER_NAME_AKSHARE,
            "method": "fetch_fund_flow",
            "scope": scope,
            "period": period,
            "sector_type": sector_type,
            "row_count": row_count,
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
        logger.info("fund_flow numeric skipped reason=unparseable_float raw=%r", value)
        return None
    if f != f:  # NaN guard — must NOT silently mask a schema violation.
        return None
    return f


__all__ = ["AkshareFundFlowProvider"]
