"""Akshare-based brokerage research-report provider for A-share symbols.

Wraps akshare's ``stock_research_report_em`` (东方财富 个股研报) into the
:class:`doyoutrade.data.protocols.ResearchReportProvider` contract. Like
the news provider, the upstream endpoint has **no date-range parameter**
— it returns every report akshare holds for the symbol — so this provider
filters to the caller's ``[start, end]`` window client-side on the
report ``日期`` column and never leaks rows outside it.

Failure-mode discipline (per CLAUDE.md §错误可见性):

* A *persistent* upstream failure (all retries exhausted) re-raises the
  last exception so the ``data_research_reports`` tool can surface a
  distinct ``research_reports_fetch_failed`` error_code with the
  exception type.
* A genuinely *empty* result (API returned nothing, or everything fell
  outside the window) returns ``[]`` — the tool maps that to
  ``research_reports_empty``, a different failure mode than a fetch error.

The forecast columns (``<year>-盈利预测-收益`` / ``<year>-盈利预测-市盈率``)
are dynamic — the year prefixes shift forward over time — so they are
parsed out of whatever columns the upstream happens to return this run,
rather than hard-coded. A column absent upstream is simply absent from
the forecast dicts.

Both paths are observable: the ``data.akshare.fetch_research_reports``
OTel span + ``data_provider.fetch_research_reports`` debug event always
fire (carrying the symbol, window, fetched/returned counts), and retries
log at WARNING with the attempt number.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import List, Optional

import akshare as ak

from doyoutrade.core.models import ResearchReport
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities

logger = logging.getLogger(__name__)

# akshare ``stock_research_report_em`` column names (东方财富 个股研报).
_COL_TITLE = "报告名称"
_COL_RATING = "东财评级"
_COL_INSTITUTION = "机构"
_COL_RECENT_COUNT = "近一月个股研报数"
_COL_INDUSTRY = "行业"
_COL_DATE = "日期"
_COL_PDF = "报告PDF链接"

# Forecast columns are dynamic: ``<year>-盈利预测-收益`` (EPS) and
# ``<year>-盈利预测-市盈率`` (PE). Parse whatever upstream returns.
_RE_EPS_COL = re.compile(r"^(\d{4})-盈利预测-收益$")
_RE_PE_COL = re.compile(r"^(\d{4})-盈利预测-市盈率$")

_MAX_ATTEMPTS = 3


class AkshareResearchReportProvider:
    """Symbol-scoped brokerage research-report source backed by akshare."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        # Research reports have no interval / adjust axis; an empty interval
        # set keeps the capabilities shape uniform with OHLCV providers
        # without claiming bar support.
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        is_realtime_capable=False,
        max_history_years=None,
    )

    async def fetch_research_reports(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        limit: int | None = None,
    ) -> List[ResearchReport]:
        with data_span("akshare", "fetch_research_reports"):
            reports = await asyncio.to_thread(
                self._sync_fetch_research_reports, symbol, start, end, limit
            )
        _emit_fetch_research_reports_event(symbol, start, end, len(reports), limit)
        return reports

    def _sync_fetch_research_reports(
        self,
        symbol: str,
        start: str,
        end: str,
        limit: int | None,
    ) -> List[ResearchReport]:
        # ``stock_research_report_em`` expects a bare 6-digit code.
        ak_symbol = symbol.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")

        df = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                df = ak.stock_research_report_em(symbol=ak_symbol)
                break
            except Exception as exc:  # noqa: BLE001 — re-raised below after retries
                logger.warning(
                    "akshare stock_research_report_em failed for %s (attempt %d/%d): %s: %s",
                    symbol, attempt + 1, _MAX_ATTEMPTS, type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "akshare stock_research_report_em gave up for %s [%s..%s]: %s: %s",
                        symbol, start, end, type(exc).__name__, exc,
                    )
                    raise
                time.sleep(0.8 * (attempt + 1))

        if df is None or df.empty:
            logger.info(
                "akshare stock_research_report_em returned no rows for %s [%s..%s]",
                symbol, start, end,
            )
            return []

        eps_year_cols = {
            m.group(1): col for col in df.columns if (m := _RE_EPS_COL.match(col))
        }
        pe_year_cols = {
            m.group(1): col for col in df.columns if (m := _RE_PE_COL.match(col))
        }

        reports: List[ResearchReport] = []
        for _, row in df.iterrows():
            report_date = _normalize_date(row.get(_COL_DATE))
            if report_date is None:
                logger.info(
                    "research_report row skipped for %s reason=unparseable_date raw=%r",
                    symbol, row.get(_COL_DATE),
                )
                continue
            eps_forecasts = {
                year: _to_float(row.get(col))
                for year, col in eps_year_cols.items()
            }
            pe_forecasts = {
                year: _to_float(row.get(col))
                for year, col in pe_year_cols.items()
            }
            reports.append(
                ResearchReport(
                    symbol=symbol,
                    title=_clean_str(row.get(_COL_TITLE)),
                    rating=_clean_str(row.get(_COL_RATING)),
                    institution=_clean_str(row.get(_COL_INSTITUTION)),
                    report_date=report_date,
                    pdf_url=_clean_str(row.get(_COL_PDF)),
                    provider=PROVIDER_NAME_AKSHARE,
                    industry=_clean_str(row.get(_COL_INDUSTRY)),
                    recent_report_count=_to_int(row.get(_COL_RECENT_COUNT)),
                    eps_forecasts=eps_forecasts,
                    pe_forecasts=pe_forecasts,
                )
            )

        windowed = [
            r for r in reports if start <= r.report_date <= end
        ]
        windowed.sort(key=lambda r: r.report_date, reverse=True)
        if limit is not None and limit >= 0:
            windowed = windowed[:limit]
        return windowed


def _emit_fetch_research_reports_event(
    symbol: str,
    start: str,
    end: str,
    report_count: int,
    limit: int | None,
) -> None:
    _fire_event(
        "data_provider.fetch_research_reports",
        {
            "provider": PROVIDER_NAME_AKSHARE,
            "method": "fetch_research_reports",
            "symbol": symbol,
            "start": start,
            "end": end,
            "report_count": report_count,
            "limit": limit,
        },
    )


def _fire_event(event_name: str, payload: dict) -> None:
    """Fire emit_debug_event as a fire-and-forget task from a sync/async context."""
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


def _to_float(value) -> Optional[float]:
    text = _clean_str(value)
    if not text:
        return None
    try:
        f = float(text)
    except (TypeError, ValueError):
        logger.info(
            "research_report forecast skipped reason=unparseable_float raw=%r",
            value,
        )
        return None
    if f != f:  # NaN guard — must NOT silently mask a schema violation.
        return None
    return f


def _to_int(value) -> int:
    f = _to_float(value)
    if f is None:
        return 0
    return int(f)


def _normalize_date(value) -> Optional[str]:
    """Normalize akshare report date to ``YYYY-MM-DD``.

    Returns ``None`` when the raw value can't be parsed so the caller can
    skip the row instead of silently dropping it into the wrong window.
    """
    text = _clean_str(value)
    if not text:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text
    try:
        import pandas as pd

        ts = pd.to_datetime(text, errors="raise")
        return ts.strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 — unparseable; caller skips the row
        return None


__all__ = ["AkshareResearchReportProvider"]
