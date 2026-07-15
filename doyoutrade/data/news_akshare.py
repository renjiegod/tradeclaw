"""Akshare-based news provider for A-share symbols (沪深北交所).

Wraps akshare's ``stock_news_em`` (东方财富 个股新闻) into the
:class:`doyoutrade.data.protocols.NewsProvider` contract. Unlike the OHLCV
provider, the upstream endpoint has **no date-range parameter** — it
returns a fixed window of recent items — so this provider filters to the
caller's ``[start, end]`` window client-side and never leaks rows outside
it.

Failure-mode discipline (per CLAUDE.md §错误可见性):

* A *persistent* upstream failure (all retries exhausted) re-raises the
  last exception so the ``data_news`` tool can surface a distinct
  ``news_fetch_failed`` error_code with the exception type.
* A genuinely *empty* result (API returned nothing, or everything fell
  outside the window) returns ``[]`` — the tool maps that to
  ``news_empty``, a different failure mode than a fetch error.

Both paths are observable: the ``data.akshare.fetch_news`` OTel span +
``data_provider.fetch_news`` debug event always fire (carrying the symbol,
window, fetched/returned counts), and retries log at WARNING with the
attempt number.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

import akshare as ak

from doyoutrade.core.models import NewsArticle
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities

logger = logging.getLogger(__name__)

# akshare ``stock_news_em`` column names (东方财富 个股新闻).
_COL_KEYWORD = "关键词"
_COL_TITLE = "新闻标题"
_COL_CONTENT = "新闻内容"
_COL_PUBLISH = "发布时间"
_COL_SOURCE = "文章来源"
_COL_URL = "新闻链接"

_MAX_ATTEMPTS = 3


class AkshareNewsProvider:
    """Symbol-scoped news source backed by akshare ``stock_news_em``."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        # News has no interval / adjust axis; an empty interval set keeps
        # the capabilities shape uniform with OHLCV providers without
        # claiming bar support.
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        # ``stock_news_em`` returns only recent items (no historical
        # backfill), so realtime-ish but window-limited.
        is_realtime_capable=False,
        max_history_years=None,
    )

    async def fetch_news(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        limit: int | None = None,
    ) -> List[NewsArticle]:
        with data_span("akshare", "fetch_news"):
            articles = await asyncio.to_thread(
                self._sync_fetch_news, symbol, start, end, limit
            )
        _emit_fetch_news_event(symbol, start, end, len(articles), limit)
        return articles

    def _sync_fetch_news(
        self,
        symbol: str,
        start: str,
        end: str,
        limit: int | None,
    ) -> List[NewsArticle]:
        # ``stock_news_em`` expects a bare 6-digit code (no exchange suffix).
        ak_symbol = symbol.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")

        df = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                df = ak.stock_news_em(symbol=ak_symbol)
                break
            except Exception as exc:  # noqa: BLE001 — re-raised below after retries
                logger.warning(
                    "akshare stock_news_em failed for %s (attempt %d/%d): %s: %s",
                    symbol, attempt + 1, _MAX_ATTEMPTS, type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    # Persistent failure is NOT silently swallowed — re-raise
                    # so the tool reports news_fetch_failed (distinct from
                    # the empty-result path below).
                    logger.error(
                        "akshare stock_news_em gave up for %s [%s..%s]: %s: %s",
                        symbol, start, end, type(exc).__name__, exc,
                    )
                    raise
                time.sleep(0.8 * (attempt + 1))

        if df is None or df.empty:
            logger.info(
                "akshare stock_news_em returned no rows for %s [%s..%s]",
                symbol, start, end,
            )
            return []

        articles: List[NewsArticle] = []
        for _, row in df.iterrows():
            publish_time = _normalize_publish_time(row.get(_COL_PUBLISH))
            if publish_time is None:
                # A row we cannot date can't be windowed correctly; skip it
                # loudly rather than silently mis-filing it inside/outside
                # the requested window.
                logger.info(
                    "news row skipped for %s reason=unparseable_publish_time raw=%r",
                    symbol, row.get(_COL_PUBLISH),
                )
                continue
            articles.append(
                NewsArticle(
                    symbol=symbol,
                    title=_clean_str(row.get(_COL_TITLE)),
                    content=_clean_str(row.get(_COL_CONTENT)),
                    publish_time=publish_time,
                    source=_clean_str(row.get(_COL_SOURCE)),
                    url=_clean_str(row.get(_COL_URL)),
                    provider=PROVIDER_NAME_AKSHARE,
                    keyword=_clean_str(row.get(_COL_KEYWORD)),
                )
            )

        # Inclusive date-window filter (compare the date portion only so a
        # ``YYYY-MM-DD`` end bound still matches same-day timestamps).
        windowed = [
            a for a in articles if start <= a.publish_time[:10] <= end
        ]
        # Most-recent first, then cap.
        windowed.sort(key=lambda a: a.publish_time, reverse=True)
        if limit is not None and limit >= 0:
            windowed = windowed[:limit]
        return windowed


def _emit_fetch_news_event(
    symbol: str,
    start: str,
    end: str,
    article_count: int,
    limit: int | None,
) -> None:
    _fire_event(
        "data_provider.fetch_news",
        {
            "provider": PROVIDER_NAME_AKSHARE,
            "method": "fetch_news",
            "symbol": symbol,
            "start": start,
            "end": end,
            "article_count": article_count,
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
        # No running event loop; skip.
        pass


def _clean_str(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    # pandas NaN stringifies to "nan"; treat as empty.
    return "" if text.lower() == "nan" else text


def _normalize_publish_time(value) -> Optional[str]:
    """Normalize akshare publish time to ``YYYY-MM-DD HH:MM:SS`` / ``YYYY-MM-DD``.

    Returns ``None`` when the raw value can't be parsed so the caller can
    skip the row instead of silently dropping it into the wrong window.
    """
    text = _clean_str(value)
    if not text:
        return None
    # Already in the expected shapes.
    import re

    if re.match(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$", text):
        return text.replace("T", " ")
    # Fallback: let pandas attempt a parse for odd upstream formats.
    try:
        import pandas as pd

        ts = pd.to_datetime(text, errors="raise")
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # noqa: BLE001 — unparseable; caller skips the row
        return None


__all__ = ["AkshareNewsProvider"]
