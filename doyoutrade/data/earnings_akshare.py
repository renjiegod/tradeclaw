"""Akshare-based earnings-data provider (业绩预告 / 业绩快报) for A-shares.

Wraps akshare's ``stock_yjyg_em`` (业绩预告) and ``stock_yjkb_em`` (业绩快报)
into the :class:`doyoutrade.data.protocols.EarningsProvider` contract.

Unlike the news / research-report providers (symbol-scoped, one network
call per symbol), earnings data is served **full-market per fiscal
quarter-end** — the upstream takes a single ``date`` report-period token
(e.g. ``"20240930"``) and returns every listed company's row in one
DataFrame. This provider is therefore **batch**: callers pass the set of
symbols they care about plus the report periods, and the provider pulls
each period once for the whole market, then filters to the requested
symbols in memory. Re-fetching the whole market once per symbol would be
wasteful (a single period can be thousands of rows).

Report-period tokens are quarter-ends (``YYYYMMDD`` with month/day in
``03-31 / 06-30 / 09-30 / 12-31``); the caller resolves them from a date
window. A period that upstream has no data for simply yields no rows for
any symbol (not an error). Numeric fields are ``None`` when upstream omits
them or reports NaN — common for newly-listed names missing prior-year
comparables; this is a genuine data gap, not masked.

Failure-mode discipline (per CLAUDE.md §错误可见性):

* A *persistent* upstream failure on a period (all retries exhausted) is
  recorded but does NOT abort the whole batch — other periods still
  resolve. The failed periods are returned to the caller so it can surface
  ``earnings_fetch_failed`` with the affected periods + exception type.
* A genuinely *empty* result (every period returned nothing, or nothing
  matched the requested symbols) returns empty maps — the tool maps that
  to ``earnings_empty``, a different failure mode than a fetch error.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

import akshare as ak

from doyoutrade.core.models import EarningsExpress, EarningsForecast
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities

logger = logging.getLogger(__name__)

# ``stock_yjyg_em`` (业绩预告) columns.
_YG_CODE = "股票代码"
_YG_NAME = "股票简称"
_YG_INDICATOR = "预测指标"
_YG_CHANGE = "业绩变动"
_YG_VALUE = "预测数值"
_YG_CHANGE_PCT = "业绩变动幅度"
_YG_REASON = "业绩变动原因"
_YG_TYPE = "预告类型"
_YG_PREV = "上年同期值"
_YG_ANNOUNCE = "公告日期"

# ``stock_yjkb_em`` (业绩快报) columns.
_KB_CODE = "股票代码"
_KB_NAME = "股票简称"
_KB_EPS = "每股收益"
_KB_REV = "营业收入-营业收入"
_KB_REV_YOY = "营业收入-同比增长"
_KB_REV_QOQ = "营业收入-季度环比增长"
_KB_NP = "净利润-净利润"
_KB_NP_YOY = "净利润-同比增长"
_KB_NP_QOQ = "净利润-季度环比增长"
_KB_NAVS = "每股净资产"
_KB_ROE = "净资产收益率"
_KB_INDUSTRY = "所处行业"
_KB_ANNOUNCE = "公告日期"

_MAX_ATTEMPTS = 3


class AkshareEarningsProvider:
    """Batch earnings-data source backed by akshare (full-market per period)."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        is_realtime_capable=False,
        max_history_years=None,
    )

    async def fetch_earnings_forecasts(
        self,
        symbols: list[str],
        report_periods: list[str],
    ) -> dict[str, list[EarningsForecast]]:
        wanted_codes = _to_bare_codes(symbols)
        result: dict[str, list[EarningsForecast]] = {}
        failures: list[tuple[str, str]] = []
        with data_span("akshare", "fetch_earnings_forecasts"):
            for period in report_periods:
                df = await self._fetch_period(
                    ak.stock_yjyg_em, "stock_yjyg_em", period, failures
                )
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    code = _clean_str(row.get(_YG_CODE))
                    canonical = _canonical(code)
                    if code not in wanted_codes:
                        continue
                    announce = _normalize_date(row.get(_YG_ANNOUNCE))
                    result.setdefault(canonical, []).append(
                        EarningsForecast(
                            symbol=canonical,
                            name=_clean_str(row.get(_YG_NAME)),
                            report_period=period,
                            preannounce_type=_clean_str(row.get(_YG_TYPE)),
                            announce_date=announce,
                            provider=PROVIDER_NAME_AKSHARE,
                            forecast_indicator=_clean_str(row.get(_YG_INDICATOR)),
                            forecast_value=_to_float(row.get(_YG_VALUE)),
                            change_pct=_to_float(row.get(_YG_CHANGE_PCT)),
                            prev_year_value=_to_float(row.get(_YG_PREV)),
                            change_description=_clean_str(row.get(_YG_CHANGE)),
                            reason=_clean_str(row.get(_YG_REASON)),
                        )
                    )
        _emit_event(
            "data_provider.fetch_earnings_forecasts",
            {
                "provider": PROVIDER_NAME_AKSHARE,
                "symbols_requested": len(wanted_codes),
                "report_periods": list(report_periods),
                "symbols_served": len(result),
                "failed_periods": failures,
            },
        )
        return result

    async def fetch_earnings_express(
        self,
        symbols: list[str],
        report_periods: list[str],
    ) -> dict[str, list[EarningsExpress]]:
        wanted_codes = _to_bare_codes(symbols)
        result: dict[str, list[EarningsExpress]] = {}
        failures: list[tuple[str, str]] = []
        with data_span("akshare", "fetch_earnings_express"):
            for period in report_periods:
                df = await self._fetch_period(
                    ak.stock_yjkb_em, "stock_yjkb_em", period, failures
                )
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    code = _clean_str(row.get(_KB_CODE))
                    canonical = _canonical(code)
                    if code not in wanted_codes:
                        continue
                    announce = _normalize_date(row.get(_KB_ANNOUNCE))
                    result.setdefault(canonical, []).append(
                        EarningsExpress(
                            symbol=canonical,
                            name=_clean_str(row.get(_KB_NAME)),
                            report_period=period,
                            announce_date=announce,
                            provider=PROVIDER_NAME_AKSHARE,
                            eps=_to_float(row.get(_KB_EPS)),
                            revenue=_to_float(row.get(_KB_REV)),
                            revenue_prev_yoy=_to_float(row.get(_KB_REV_YOY)),
                            revenue_qoq=_to_float(row.get(_KB_REV_QOQ)),
                            net_profit=_to_float(row.get(_KB_NP)),
                            net_profit_prev_yoy=_to_float(row.get(_KB_NP_YOY)),
                            net_profit_qoq=_to_float(row.get(_KB_NP_QOQ)),
                            navs_per_share=_to_float(row.get(_KB_NAVS)),
                            roe=_to_float(row.get(_KB_ROE)),
                            industry=_clean_str(row.get(_KB_INDUSTRY)),
                        )
                    )
        _emit_event(
            "data_provider.fetch_earnings_express",
            {
                "provider": PROVIDER_NAME_AKSHARE,
                "symbols_requested": len(wanted_codes),
                "report_periods": list(report_periods),
                "symbols_served": len(result),
                "failed_periods": failures,
            },
        )
        return result

    async def _fetch_period(self, fn, label: str, period: str, failures: list[tuple[str, str]]):
        """Pull one full-market period with retries. Records (period, errtype).

        A failed period returns ``None`` (other periods still resolve) so a
        single upstream hiccup never aborts the whole batch — but it is NOT
        swallowed: it lands in ``failures`` for the tool to surface.
        ``label`` is the akshare function name (passed explicitly because a
        unittest mock has no ``__name__``).
        """
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return await asyncio.to_thread(fn, date=period)
            except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
                logger.warning(
                    "akshare %s failed for period %s (attempt %d/%d): %s: %s",
                    label, period, attempt + 1, _MAX_ATTEMPTS,
                    type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "akshare %s gave up for period %s: %s: %s",
                        label, period, type(exc).__name__, exc,
                    )
                    failures.append((period, type(exc).__name__))
                    return None
                time.sleep(0.8 * (attempt + 1))
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_bare_codes(symbols: Iterable[str]) -> set[str]:
    """Map canonical symbols (600519.SH) to akshare bare 6-digit codes."""
    return {
        s.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
        for s in symbols
    }


def _canonical(bare_code: str) -> str:
    """Best-effort canonicalization of a bare 6-digit code to CODE.EXCHANGE.

    The provider already filters by requested symbols, so this only needs to
    match the canonical form the caller passed in. We reconstruct the suffix
    by A-share rules: 6/9 → SH, 0/2/3 → SZ, 4/8 → BJ. When unsure, default
    to SH (the caller's symbol set already gated membership).
    """
    if not bare_code or len(bare_code) < 1:
        return bare_code
    first = bare_code[0]
    if first in ("6", "9"):
        return f"{bare_code}.SH"
    if first in ("0", "2", "3"):
        return f"{bare_code}.SZ"
    if first in ("4", "8"):
        return f"{bare_code}.BJ"
    return f"{bare_code}.SH"


def _emit_event(event_name: str, payload: dict) -> None:
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        pass


def _clean_str(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _to_float(value):
    text = _clean_str(value)
    if not text:
        return None
    try:
        f = float(text)
    except (TypeError, ValueError):
        logger.info("earnings numeric skipped reason=unparseable_float raw=%r", value)
        return None
    if f != f:  # NaN guard — must NOT silently mask a schema violation.
        return None
    return f


def _normalize_date(value) -> str:
    """Normalize akshare announce date to ``YYYY-MM-DD`` (empty if unparseable)."""
    import re

    text = _clean_str(value)
    if not text:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    try:
        import pandas as pd

        ts = pd.to_datetime(text, errors="raise")
        return ts.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 — unparseable; caller keeps empty string
        return ""


__all__ = ["AkshareEarningsProvider"]
