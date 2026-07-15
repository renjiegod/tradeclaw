"""Akshare-based sector / industry / concept membership provider.

Wraps akshare's 东方财富 board endpoints into the
:class:`doyoutrade.data.protocols.SectorProvider` contract:

* ``stock_board_industry_name_em`` / ``stock_board_concept_name_em`` —
  list the available board names. ``list_sectors`` reads only the 板块名称
  column; ``get_sector_heat`` reuses the *same* endpoints but keeps the
  whole-board heat columns those two endpoints also return (涨跌幅 / 总市值 /
  换手率 / 上涨·下跌家数 / 领涨股票 + 领涨股票-涨跌幅) so callers can rank the
  day's 题材 / 板块热度.
* ``stock_board_industry_cons_em`` / ``stock_board_concept_cons_em`` —
  list a board's constituent stocks (bare 6-digit ``代码``), which this
  provider normalizes to canonical Doyoutrade symbols (e.g. ``600519.SH``)
  so the resulting universe is directly screenable.

Failure-mode discipline (per CLAUDE.md §错误可见性), mirroring
``AkshareNewsProvider``:

* A *persistent* upstream failure (all retries exhausted) re-raises so the
  ``data_sector`` tool surfaces a distinct ``sector_fetch_failed`` error.
* A genuinely *empty* board returns ``[]`` — the tool maps that to
  ``sector_empty``, a different failure mode than a fetch error.
* The ``data.akshare.<method>`` OTel span + ``data_provider.<method>``
  debug event always fire; retries log at WARNING with the attempt number.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, List

import akshare as ak

from doyoutrade.core.models import SectorHeatRow, SectorMember
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrument_catalog.normalize import (
    canonical_symbol_from_doyoutrade_or_akshare,
)
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities

logger = logging.getLogger(__name__)

# akshare board-name columns (东方财富).
_COL_BOARD_NAME = "板块名称"
# akshare constituent columns.
_COL_CODE = "代码"
_COL_NAME = "名称"

# akshare board-name endpoint *heat* columns — the membership methods only read
# ``板块名称`` and drop the rest; the heat method keeps these. Documented schema
# for stock_board_industry_name_em / stock_board_concept_name_em (东方财富):
#   排名, 板块名称, 板块代码, 最新价, 涨跌额, 涨跌幅, 总市值, 换手率,
#   上涨家数, 下跌家数, 领涨股票, 领涨股票-涨跌幅
# Each is matched by exact column name and tolerated as missing → None.
_COL_BOARD_CODE = "板块代码"
_COL_CHANGE_PCT = "涨跌幅"
_COL_TOTAL_MV = "总市值"
_COL_TURNOVER_RATE = "换手率"
_COL_UP_COUNT = "上涨家数"
_COL_DOWN_COUNT = "下跌家数"
_COL_LEADER_STOCK = "领涨股票"
_COL_LEADER_CHANGE_PCT = "领涨股票-涨跌幅"

_SECTOR_TYPE_INDUSTRY = "industry"
_SECTOR_TYPE_CONCEPT = "concept"

_MAX_ATTEMPTS = 3


def _clean_str(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _to_float(value) -> float | None:
    """Parse a numeric column to float, returning ``None`` (never 0) on failure.

    Mirrors ``limit_pool_akshare._to_float`` — a missing / unparseable /
    NaN cell must NOT be silently coerced to 0 (per §错误可见性), so a
    board with no 涨跌幅 sorts / renders as unknown rather than as flat.
    """
    text = _clean_str(value)
    if not text:
        return None
    try:
        f = float(text)
    except (TypeError, ValueError):
        logger.info("sector_heat numeric skipped reason=unparseable_float raw=%r", value)
        return None
    if f != f:  # NaN guard.
        return None
    return f


def _to_int(value) -> int | None:
    """Parse an integer-valued column, returning ``None`` (not 0) on failure.

    Returning ``None`` rather than a 0 fallback keeps a missing / unparseable
    上涨/下跌家数 out of the row instead of manufacturing a phantom 0-count
    (per §错误可见性: no ``int(脏值)`` truncation).
    """
    f = _to_float(value)
    if f is None:
        return None
    return int(f)


class AkshareSectorProvider:
    """Sector membership source backed by akshare 东方财富 board endpoints."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        # Sector membership has no interval / adjust axis; an empty interval
        # set keeps the capabilities shape uniform with OHLCV providers
        # without claiming bar support.
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        is_realtime_capable=False,
        max_history_years=None,
    )

    async def list_sectors(self, *, sector_type: str | None = None) -> List[str]:
        with data_span("akshare", "list_sectors"):
            names = await asyncio.to_thread(self._sync_list_sectors, sector_type)
        _emit_event("data_provider.list_sectors", {"sector_type": sector_type, "count": len(names)})
        return names

    async def get_sector_members(
        self, sector_name: str, *, sector_type: str | None = None
    ) -> List[SectorMember]:
        with data_span("akshare", "get_sector_members"):
            members = await asyncio.to_thread(
                self._sync_get_members, sector_name, sector_type
            )
        _emit_event(
            "data_provider.get_sector_members",
            {"sector_name": sector_name, "sector_type": sector_type, "count": len(members)},
        )
        return members

    async def get_sector_heat(self, sector_type: str) -> List[SectorHeatRow]:
        with data_span("akshare", "get_sector_heat"):
            rows = await asyncio.to_thread(self._sync_get_sector_heat, sector_type)
        _emit_event(
            "data_provider.get_sector_heat",
            {"sector_type": sector_type, "count": len(rows)},
        )
        return rows

    # ------------------------------------------------------------------

    def _sync_list_sectors(self, sector_type: str | None) -> List[str]:
        names: List[str] = []
        if sector_type in (None, _SECTOR_TYPE_INDUSTRY):
            names.extend(self._board_names(ak.stock_board_industry_name_em, "industry"))
        if sector_type in (None, _SECTOR_TYPE_CONCEPT):
            names.extend(self._board_names(ak.stock_board_concept_name_em, "concept"))
        # De-dup while preserving order (industry boards first).
        return list(dict.fromkeys(names))

    def _board_names(self, fn, label: str) -> List[str]:
        df = self._retry(fn, what=f"{label}_name_em")
        if df is None or df.empty or _COL_BOARD_NAME not in df.columns:
            logger.info("akshare %s board list empty / missing column", label)
            return []
        return [_clean_str(v) for v in df[_COL_BOARD_NAME].tolist() if _clean_str(v)]

    def _sync_get_sector_heat(self, sector_type: str) -> List[SectorHeatRow]:
        """Fetch one board family's whole-board heat rows.

        Reuses the same board-name endpoints ``_board_names`` reads, but keeps
        the heat columns rather than only ``板块名称``. ``sector_type`` is
        validated at the tool layer; reaching here with an unknown value is a
        schema violation, not a tolerated fallback — we raise rather than
        silently returning nothing (per §错误可见性).

        A *persistent* upstream failure re-raises via ``_retry`` (→ tool
        ``sector_heat_fetch_failed``); a genuinely empty / column-missing board
        list returns ``[]`` (→ tool ``sector_heat_empty``). A row with no
        identifiable 板块名称 is dropped **loudly** (``logger.info``); every
        numeric that can't be parsed becomes ``None`` (never ``int(脏值)``).
        """
        if sector_type == _SECTOR_TYPE_INDUSTRY:
            fn = ak.stock_board_industry_name_em
        elif sector_type == _SECTOR_TYPE_CONCEPT:
            fn = ak.stock_board_concept_name_em
        else:
            raise ValueError(
                f"get_sector_heat got unknown sector_type {sector_type!r}; "
                f"expected {_SECTOR_TYPE_INDUSTRY!r} or {_SECTOR_TYPE_CONCEPT!r}"
            )

        df = self._retry(lambda: fn(), what=f"{sector_type}_name_em")
        if df is None or df.empty or _COL_BOARD_NAME not in df.columns:
            logger.info(
                "akshare %s board heat empty / missing 板块名称 column", sector_type
            )
            return []

        columns = set(df.columns)
        rows: List[SectorHeatRow] = []
        for _, row in df.iterrows():
            board_name = _clean_str(row.get(_COL_BOARD_NAME))
            if not board_name:
                # A board row with no name can't be identified; skip it loudly
                # rather than emitting a phantom board.
                logger.info(
                    "sector_heat row skipped sector_type=%s reason=missing_board_name raw=%r",
                    sector_type, dict(row),
                )
                continue
            rows.append(
                SectorHeatRow(
                    board_name=board_name,
                    sector_type=sector_type,
                    provider=PROVIDER_NAME_AKSHARE,
                    board_code=_clean_str(row.get(_COL_BOARD_CODE))
                    if _COL_BOARD_CODE in columns else "",
                    change_pct=_to_float(row.get(_COL_CHANGE_PCT)),
                    total_mv=_to_float(row.get(_COL_TOTAL_MV)),
                    turnover_rate=_to_float(row.get(_COL_TURNOVER_RATE)),
                    up_count=_to_int(row.get(_COL_UP_COUNT)),
                    down_count=_to_int(row.get(_COL_DOWN_COUNT)),
                    leader_stock=_clean_str(row.get(_COL_LEADER_STOCK))
                    if _COL_LEADER_STOCK in columns else "",
                    leader_change_pct=_to_float(row.get(_COL_LEADER_CHANGE_PCT)),
                )
            )
        return rows

    def _sync_get_members(
        self, sector_name: str, sector_type: str | None
    ) -> List[SectorMember]:
        # Resolve which board family to query. When the caller does not pin
        # a type, try industry first then concept; only when EVERY queried
        # family raised do we surface the upstream error (akshare raises on
        # an unknown board) — a family that resolves but is empty is a
        # legitimate empty result, not a fetch failure.
        attempts: list[tuple[str, Callable[..., Any]]] = []
        if sector_type in (None, _SECTOR_TYPE_INDUSTRY):
            attempts.append((_SECTOR_TYPE_INDUSTRY, ak.stock_board_industry_cons_em))
        if sector_type in (None, _SECTOR_TYPE_CONCEPT):
            attempts.append((_SECTOR_TYPE_CONCEPT, ak.stock_board_concept_cons_em))

        last_exc: Exception | None = None
        any_fetch_ok = False
        for resolved_type, fn in attempts:
            try:
                df = self._retry(
                    lambda f=fn: f(symbol=sector_name), what=f"{resolved_type}_cons_em"
                )
            except Exception as exc:  # noqa: BLE001 — re-raised after all attempts
                last_exc = exc
                logger.warning(
                    "akshare %s cons lookup failed for sector=%r: %s: %s",
                    resolved_type, sector_name, type(exc).__name__, exc,
                )
                continue
            any_fetch_ok = True
            if df is None or df.empty or _COL_CODE not in df.columns:
                continue
            members: List[SectorMember] = []
            for _, row in df.iterrows():
                raw_code = _clean_str(row.get(_COL_CODE))
                if not raw_code:
                    continue
                members.append(
                    SectorMember(
                        sector_name=sector_name,
                        code=canonical_symbol_from_doyoutrade_or_akshare(raw_code),
                        name=_clean_str(row.get(_COL_NAME)),
                        provider=PROVIDER_NAME_AKSHARE,
                        sector_type=resolved_type,
                    )
                )
            if members:
                return members

        if not any_fetch_ok and last_exc is not None:
            # Every queried family raised — persistent failure, not empty.
            raise last_exc
        # Board(s) resolved but had no constituents → genuinely empty.
        return []

    def _retry(self, fn, *, what: str):
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 — re-raised below after retries
                logger.warning(
                    "akshare %s failed (attempt %d/%d): %s: %s",
                    what, attempt + 1, _MAX_ATTEMPTS, type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "akshare %s gave up: %s: %s", what, type(exc).__name__, exc
                    )
                    raise
                time.sleep(0.8 * (attempt + 1))
        return None


def _emit_event(event_name: str, payload: dict) -> None:
    payload = {"provider": PROVIDER_NAME_AKSHARE, **payload}
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        pass


__all__ = ["AkshareSectorProvider"]
