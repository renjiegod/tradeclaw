"""Akshare-based event provider (suspension / 停牌雷).

Backed by ``stock_tfp_em`` — 东方财富 停复牌信息 for a given date. One call
returns the whole-market suspension list for that date, so checking whether
a universe member is halted as of ``asof`` is a membership test on the
``代码`` column (the same canonical column M2/M3 rely on). Suspension logic
needs only that membership, so it is robust to drift in the descriptive
columns (停牌时间 / 停牌原因 are surfaced as ``detail`` best-effort).

Earnings-disclosure (财报预约披露) is intentionally **not** implemented here:
``stock_report_disclosure``'s period-token format and date columns can't be
verified offline, and gating a screen on unverified data risks silently
mis-excluding symbols. It is a documented follow-up.

Failure-mode discipline (per CLAUDE.md §错误可见性): a persistent upstream
failure re-raises (``data_events`` maps it to ``events_fetch_failed``); a
missing ``代码`` column raises ``_EventSchemaError`` (loud, never silently
empty). An empty snapshot is a legitimate "no suspensions" → empty map.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import List

import akshare as ak

from doyoutrade.core.models import EventItem
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrument_catalog.normalize import (
    canonical_symbol_from_doyoutrade_or_akshare,
)
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities

logger = logging.getLogger(__name__)

EVENT_SUSPENSION = "suspension"

# Candidate column names (akshare 东方财富 停复牌). Code is the load-bearing
# one; the rest are best-effort detail.
_CODE_COLS = ("代码", "证券代码", "股票代码")
_SUSPEND_DATE_COLS = ("停牌时间", "停牌日期")
_REASON_COLS = ("停牌原因", "停牌事项说明")

_MAX_ATTEMPTS = 3


class _EventSchemaError(Exception):
    """Upstream frame lacked an expected load-bearing column."""


def _clean(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def _first_col(df, candidates) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


class AkshareEventProvider:
    """Event source backed by akshare ``stock_tfp_em`` (suspension only)."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        is_realtime_capable=False,
        max_history_years=None,
    )

    async def get_events_batch(
        self, symbols: list[str], *, asof: str | None = None
    ) -> dict[str, list[EventItem]]:
        asof_date = self._parse_asof(asof)
        with data_span("akshare", "get_events"):
            suspended = await asyncio.to_thread(self._sync_suspensions, asof_date)
        wanted = {canonical_symbol_from_doyoutrade_or_akshare(s) for s in symbols}
        result: dict[str, list[EventItem]] = {
            code: items for code, items in suspended.items() if code in wanted
        }
        _emit_event(
            "data_provider.get_events",
            {"asof": asof_date.isoformat(), "requested": len(symbols), "with_events": len(result)},
        )
        return result

    async def get_events(self, symbol: str, *, asof: str | None = None) -> List[EventItem]:
        batch = await self.get_events_batch([symbol], asof=asof)
        return batch.get(canonical_symbol_from_doyoutrade_or_akshare(symbol), [])

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_asof(asof: str | None) -> date:
        if not asof:
            return date.today()
        return date.fromisoformat(asof)

    def _sync_suspensions(self, asof_date: date) -> dict[str, list[EventItem]]:
        compact = asof_date.strftime("%Y%m%d")
        df = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                df = ak.stock_tfp_em(date=compact)
                break
            except Exception as exc:  # noqa: BLE001 — re-raised after retries
                logger.warning(
                    "akshare stock_tfp_em failed for %s (attempt %d/%d): %s: %s",
                    compact, attempt + 1, _MAX_ATTEMPTS, type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error("akshare stock_tfp_em gave up for %s", compact)
                    raise
                time.sleep(0.8 * (attempt + 1))

        if df is None or df.empty:
            logger.info("akshare stock_tfp_em returned no suspensions for %s", compact)
            return {}

        code_col = _first_col(df, _CODE_COLS)
        if code_col is None:
            # Load-bearing column missing — fail loud rather than return an
            # empty (and therefore wrongly "nothing suspended") result.
            raise _EventSchemaError(
                f"stock_tfp_em frame has no code column; got {list(df.columns)}"
            )
        date_col = _first_col(df, _SUSPEND_DATE_COLS)
        reason_col = _first_col(df, _REASON_COLS)

        out: dict[str, list[EventItem]] = {}
        for _, row in df.iterrows():
            raw = _clean(row.get(code_col))
            if not raw:
                continue
            code = canonical_symbol_from_doyoutrade_or_akshare(raw)
            out.setdefault(code, []).append(
                EventItem(
                    code=code,
                    event_type=EVENT_SUSPENSION,
                    event_date=_clean(row.get(date_col)) if date_col else asof_date.isoformat(),
                    detail=_clean(row.get(reason_col)) if reason_col else "",
                    provider=PROVIDER_NAME_AKSHARE,
                )
            )
        return out


def _emit_event(event_name: str, payload: dict) -> None:
    payload = {"provider": PROVIDER_NAME_AKSHARE, **payload}
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        pass


__all__ = ["AkshareEventProvider", "EVENT_SUSPENSION"]
