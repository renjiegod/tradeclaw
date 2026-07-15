"""Web-search-backed news provider (multi-engine) for any symbol.

Complements :class:`doyoutrade.data.news_akshare.AkshareNewsProvider`, whose
upstream (东方财富 个股新闻) only serves recent A-share items keyed by 6-digit
code. This provider issues a web search per symbol against one or more search
engines (Tavily + Bocha today; more via the :class:`BaseSearchEngine`
extension point) and maps the hits onto the same
:class:`doyoutrade.data.protocols.NewsProvider` contract.

Design ported from the reference ``daily_stock_analysis/src/search_service.py``:

* :class:`SearchResult` / :class:`SearchResponse` are the engine-neutral shapes.
* :class:`BaseSearchEngine` owns multi-key round-robin, per-key error counting,
  and the shared "pick key → call → record success/error" flow. Concrete
  engines only implement the async :meth:`BaseSearchEngine._do_search`.
* HTTP is **async** (``httpx.AsyncClient``, already a core dependency) so the
  event loop is never blocked — the reference project's synchronous
  ``requests`` / ``tavily`` SDK path is deliberately not reused.

Failure-mode discipline (per CLAUDE.md §错误可见性):

* A single engine failing (network error, bad key, HTTP 4xx/5xx) is recorded
  as ``success=False`` on its :class:`SearchResponse`, logged at WARNING with
  the exception type + message, and emitted as a ``news_websearch.engine_failed``
  debug event — it never crashes the batch and never silently disappears.
* When **every** available engine fails, :meth:`fetch_news` re-raises
  :class:`WebSearchAllEnginesFailedError` (error_code
  ``websearch_all_engines_failed``) so the ``data_news`` tool surfaces
  ``news_fetch_failed`` — distinct from an empty window.
* When no engine is configured (no API keys), :meth:`fetch_news` raises
  :class:`WebSearchNotConfiguredError` (error_code ``websearch_not_configured``)
  rather than silently returning ``[]`` (which would masquerade as
  ``news_empty``).
* A genuinely empty result (engines succeeded but nothing fell inside the
  ``[start, end]`` window) returns ``[]`` → the tool maps it to ``news_empty``.

Both the ``data.websearch.fetch_news`` OTel span and the
``data_provider.fetch_news`` debug event always fire (carrying symbol, window,
engine count, fetched/returned counts).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from itertools import cycle
from typing import Any, List, Optional
from urllib.parse import urlparse

import httpx

from doyoutrade.core.models import NewsArticle
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_WEBSEARCH, ProviderCapabilities

logger = logging.getLogger(__name__)

# Per-engine key error threshold before a key is skipped in rotation.
_KEY_ERROR_THRESHOLD = 3
# Upper bound on the recency window (days) handed to engines, so a caller
# requesting a decade of history does not ask an engine for "3650 days".
_MAX_SEARCH_DAYS = 365


class WebSearchError(RuntimeError):
    """Base for web-search provider failures carrying a stable ``error_code``."""

    error_code = "websearch_error"


class WebSearchNotConfiguredError(WebSearchError):
    """No search engine has API keys configured."""

    error_code = "websearch_not_configured"


class WebSearchAllEnginesFailedError(WebSearchError):
    """Every available engine failed for this query."""

    error_code = "websearch_all_engines_failed"


# ---------------------------------------------------------------------------
# Engine-neutral result shapes (ported from search_service.py)
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    """One search hit, before mapping onto :class:`NewsArticle`."""

    title: str
    snippet: str
    url: str
    source: str
    published_date: Optional[str] = None


@dataclass
class SearchResponse:
    """The outcome of one engine call.

    ``success=False`` with a populated ``error_message`` is the structured
    failure signal — engines never raise out of :meth:`BaseSearchEngine.search`.
    """

    query: str
    results: List[SearchResult]
    provider: str
    success: bool = True
    error_message: Optional[str] = None
    search_time: float = 0.0


def _make_async_client(timeout: float) -> httpx.AsyncClient:
    """Factory for the engine HTTP client (patched in tests via MockTransport)."""
    return httpx.AsyncClient(timeout=timeout)


def _extract_domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.replace("www.", "")
        return netloc or "unknown_source"
    except Exception:  # noqa: BLE001 — degrade to a label, never crash mapping
        return "unknown_source"


def _freshness_bocha(days: int) -> str:
    if days <= 1:
        return "oneDay"
    if days <= 7:
        return "oneWeek"
    if days <= 30:
        return "oneMonth"
    return "oneYear"


# ---------------------------------------------------------------------------
# Engine base + concrete engines
# ---------------------------------------------------------------------------


class BaseSearchEngine(ABC):
    """Multi-key round-robin search engine base.

    Subclasses implement the async :meth:`_do_search`; the base owns key
    selection, per-key error counting, and turning a raised exception into a
    structured ``success=False`` response (logged + never swallowed silently).
    """

    def __init__(self, api_keys: List[str], name: str, *, timeout: float = 10.0) -> None:
        self._api_keys = [k for k in api_keys if k]
        self._name = name
        self._timeout = timeout
        self._key_cycle = cycle(self._api_keys) if self._api_keys else None
        self._key_errors: dict[str, int] = {k: 0 for k in self._api_keys}

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_available(self) -> bool:
        return bool(self._api_keys)

    def _get_next_key(self) -> Optional[str]:
        if not self._key_cycle:
            return None
        for _ in range(len(self._api_keys)):
            key = next(self._key_cycle)
            if self._key_errors.get(key, 0) < _KEY_ERROR_THRESHOLD:
                return key
        # Every key has tripped the error threshold; reset and reuse the first
        # rather than starving the engine — but log so it is visible.
        logger.warning(
            "news_websearch engine=%s all keys over error threshold; resetting counts",
            self._name,
        )
        self._key_errors = {k: 0 for k in self._api_keys}
        return self._api_keys[0] if self._api_keys else None

    def _record_success(self, key: str) -> None:
        if self._key_errors.get(key, 0) > 0:
            self._key_errors[key] -= 1

    def _record_error(self, key: str) -> None:
        self._key_errors[key] = self._key_errors.get(key, 0) + 1

    async def search(self, query: str, max_results: int, days: int) -> SearchResponse:
        """Run one search; always returns a :class:`SearchResponse` (never raises)."""
        key = self._get_next_key()
        if not key:
            return SearchResponse(
                query=query,
                results=[],
                provider=self._name,
                success=False,
                error_message=f"{self._name} has no API key configured",
            )
        started = time.perf_counter()
        try:
            response = await self._do_search(query, key, max_results, days)
            response.search_time = time.perf_counter() - started
            if response.success:
                self._record_success(key)
            else:
                self._record_error(key)
                logger.warning(
                    "news_websearch engine=%s query=%r returned error: %s",
                    self._name, query, response.error_message,
                )
            return response
        except Exception as exc:  # noqa: BLE001 — surfaced structurally below
            self._record_error(key)
            logger.warning(
                "news_websearch engine=%s query=%r failed: %s: %s",
                self._name, query, type(exc).__name__, exc,
            )
            return SearchResponse(
                query=query,
                results=[],
                provider=self._name,
                success=False,
                error_message=f"{type(exc).__name__}: {exc}",
                search_time=time.perf_counter() - started,
            )

    @abstractmethod
    async def _do_search(
        self, query: str, api_key: str, max_results: int, days: int
    ) -> SearchResponse:
        """Execute the engine-specific request. Raise on transport error."""
        raise NotImplementedError


class TavilySearchEngine(BaseSearchEngine):
    """Tavily REST search (https://docs.tavily.com/). AI-optimised, news topic."""

    API_ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_keys: List[str], *, timeout: float = 10.0) -> None:
        super().__init__(api_keys, "tavily", timeout=timeout)

    async def _do_search(
        self, query: str, api_key: str, max_results: int, days: int
    ) -> SearchResponse:
        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": "advanced",
            "topic": "news",
            "max_results": max_results,
            "days": days,
            "include_answer": False,
            "include_raw_content": False,
        }
        async with _make_async_client(self._timeout) as client:
            resp = await client.post(self.API_ENDPOINT, json=payload)
        if resp.status_code != 200:
            return SearchResponse(
                query=query, results=[], provider=self.name, success=False,
                error_message=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        results = [
            SearchResult(
                title=str(item.get("title") or ""),
                snippet=str(item.get("content") or "")[:500],
                url=str(item.get("url") or ""),
                source=_extract_domain(str(item.get("url") or "")),
                published_date=item.get("published_date") or item.get("publishedDate"),
            )
            for item in (data.get("results") or [])
        ]
        return SearchResponse(query=query, results=results, provider=self.name, success=True)


class BochaSearchEngine(BaseSearchEngine):
    """博查 web search (https://bocha.cn/). AI-optimised Chinese search."""

    API_ENDPOINT = "https://api.bocha.cn/v1/web-search"

    def __init__(self, api_keys: List[str], *, timeout: float = 10.0) -> None:
        super().__init__(api_keys, "bocha", timeout=timeout)

    async def _do_search(
        self, query: str, api_key: str, max_results: int, days: int
    ) -> SearchResponse:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "query": query,
            "freshness": _freshness_bocha(days),
            "summary": True,
            "count": min(max_results, 50),
        }
        async with _make_async_client(self._timeout) as client:
            resp = await client.post(self.API_ENDPOINT, headers=headers, json=payload)
        if resp.status_code != 200:
            return SearchResponse(
                query=query, results=[], provider=self.name, success=False,
                error_message=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        if data.get("code") != 200:
            return SearchResponse(
                query=query, results=[], provider=self.name, success=False,
                error_message=data.get("msg") or f"API error code {data.get('code')}",
            )
        value_list = (
            (data.get("data") or {}).get("webPages", {}).get("value", []) or []
        )
        results = [
            SearchResult(
                title=str(item.get("name") or ""),
                snippet=str(item.get("summary") or item.get("snippet") or "")[:500],
                url=str(item.get("url") or ""),
                source=str(item.get("siteName") or _extract_domain(str(item.get("url") or ""))),
                published_date=item.get("datePublished"),
            )
            for item in value_list[:max_results]
        ]
        return SearchResponse(query=query, results=results, provider=self.name, success=True)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class NewsWebSearchProvider:
    """Symbol-scoped news source backed by one or more web-search engines."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_WEBSEARCH,
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        # Every engine needs an API key; the factory / tool skips it when
        # nothing is configured (fetch_news raises websearch_not_configured).
        requires_auth=True,
        is_realtime_capable=False,
        max_history_years=None,
    )

    def __init__(
        self,
        engines: List[BaseSearchEngine],
        *,
        max_results_per_engine: int = 10,
    ) -> None:
        self._engines = engines
        self._max_results_per_engine = max_results_per_engine

    @classmethod
    def from_config(cls, *, engine_filter: Optional[str] = None) -> "NewsWebSearchProvider":
        """Build from the effective DoYouTrade config (``data.news.websearch``).

        ``engine_filter`` restricts to a single engine id (``"tavily"`` /
        ``"bocha"``); ``None`` builds every engine that has keys. An engine with
        no keys is still constructed but reports ``is_available == False`` and is
        skipped at fetch time — so misconfiguration is visible, not silent.
        """
        from doyoutrade import config

        ws = config.get_config().data.news.websearch
        timeout = ws.timeout_seconds
        all_engines: dict[str, BaseSearchEngine] = {
            "tavily": TavilySearchEngine(list(ws.tavily_api_keys), timeout=timeout),
            "bocha": BochaSearchEngine(list(ws.bocha_api_keys), timeout=timeout),
        }
        if engine_filter is not None:
            if engine_filter not in all_engines:
                raise WebSearchError(f"unknown websearch engine {engine_filter!r}")
            engines = [all_engines[engine_filter]]
        else:
            # Priority order: Tavily first (better recency/structured news),
            # then Bocha (Chinese coverage). Unavailable engines stay in the
            # list but are skipped in fetch_news.
            engines = [all_engines["tavily"], all_engines["bocha"]]
        return cls(engines, max_results_per_engine=ws.max_results_per_engine)

    async def fetch_news(
        self,
        symbol: str,
        start: str,
        end: str,
        *,
        limit: int | None = None,
    ) -> List[NewsArticle]:
        with data_span("websearch", "fetch_news"):
            articles = await self._fetch(symbol, start, end, limit)
        _emit_fetch_news_event(symbol, start, end, len(articles), limit, self._engines)
        return articles

    async def _fetch(
        self,
        symbol: str,
        start: str,
        end: str,
        limit: int | None,
    ) -> List[NewsArticle]:
        available = [e for e in self._engines if e.is_available]
        if not available:
            _emit_event(
                "news_websearch.not_configured",
                {
                    "symbol": symbol,
                    "reason": "no_engine_configured",
                    "hint": "set data.news.websearch.tavily_api_keys / bocha_api_keys "
                    "(or DOYOUTRADE_TAVILY_API_KEYS / DOYOUTRADE_BOCHA_API_KEYS)",
                },
            )
            logger.warning(
                "news_websearch symbol=%s no engine configured (all key lists empty)",
                symbol,
            )
            raise WebSearchNotConfiguredError(
                "no web-search engine configured; set data.news.websearch API keys"
            )

        query = _build_query(symbol)
        days = _window_days(start)
        max_results = self._max_results_per_engine

        collected: List[tuple[str, SearchResult]] = []  # (engine_name, result)
        any_success = False
        engine_errors: list[dict[str, Any]] = []
        for engine in available:
            response = await engine.search(query, max_results, days)
            if response.success:
                any_success = True
                collected.extend((engine.name, r) for r in response.results)
            else:
                engine_errors.append(
                    {"engine": engine.name, "message": response.error_message}
                )
                _emit_event(
                    "news_websearch.engine_failed",
                    {
                        "symbol": symbol,
                        "engine": engine.name,
                        "message": response.error_message,
                        "hint": "check the engine API key / balance / network",
                    },
                )

        if not any_success:
            _emit_event(
                "news_websearch.all_engines_failed",
                {
                    "symbol": symbol,
                    "engine_count": len(available),
                    "errors": engine_errors,
                    "hint": "every engine failed; check keys / balance / connectivity",
                },
            )
            logger.error(
                "news_websearch symbol=%s all %d engine(s) failed: %s",
                symbol, len(available), engine_errors,
            )
            raise WebSearchAllEnginesFailedError(
                f"all {len(available)} web-search engine(s) failed for {symbol}: "
                f"{engine_errors}"
            )

        articles = _map_and_window(symbol, collected, start, end)
        articles.sort(key=lambda a: a.publish_time, reverse=True)
        if limit is not None and limit >= 0:
            articles = articles[:limit]
        return articles


# ---------------------------------------------------------------------------
# Query / window / mapping helpers
# ---------------------------------------------------------------------------


def _build_query(symbol: str) -> str:
    """Construct a stock-news query from a canonical symbol.

    Mirrors the reference ``search_stock_news`` shape (name + code + news
    keywords). A display name is not available at this layer, so the bare
    6-digit code is used alongside the news keywords — sufficient for the
    engines' relevance ranking. Extension point: a name resolver could enrich
    this later.
    """
    code = symbol.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
    return f"{code} 股票 最新消息 公告 财报"


def _window_days(start: str) -> int:
    """Recency window (days) handed to engines, derived from ``start``.

    The authoritative filter is the client-side ``[start, end]`` pass; this
    only tells the engine how far back to look. Bounded by ``_MAX_SEARCH_DAYS``.
    """
    try:
        start_d = date.fromisoformat(start[:10])
    except ValueError:
        return 7
    delta = (date.today() - start_d).days + 1
    if delta < 1:
        return 1
    return min(delta, _MAX_SEARCH_DAYS)


def _map_and_window(
    symbol: str,
    collected: List[tuple[str, SearchResult]],
    start: str,
    end: str,
) -> List[NewsArticle]:
    """Map hits to :class:`NewsArticle`, dedup by URL, filter to ``[start, end]``.

    A hit whose publish date cannot be parsed cannot be placed inside/outside
    the window correctly, so it is skipped loudly (logger.info) rather than
    silently mis-filed — matching the akshare provider's discipline.
    """
    seen_urls: set[str] = set()
    articles: List[NewsArticle] = []
    for engine_name, result in collected:
        url = (result.url or "").strip()
        # De-dup on URL (the stable identity across engines). Undated / URL-less
        # rows cannot be windowed or de-duped reliably; skip them loudly.
        if not url:
            logger.info(
                "news_websearch skip symbol=%s engine=%s reason=missing_url title=%r",
                symbol, engine_name, result.title,
            )
            continue
        if url in seen_urls:
            continue
        publish_time = _normalize_publish_time(result.published_date)
        if publish_time is None:
            logger.info(
                "news_websearch skip symbol=%s engine=%s reason=unparseable_publish_time raw=%r",
                symbol, engine_name, result.published_date,
            )
            continue
        if not (start <= publish_time[:10] <= end):
            continue
        seen_urls.add(url)
        articles.append(
            NewsArticle(
                symbol=symbol,
                title=result.title or "",
                content=result.snippet or "",
                publish_time=publish_time,
                source=result.source or "",
                url=url,
                provider=f"{PROVIDER_NAME_WEBSEARCH}:{engine_name}",
                keyword=symbol,
            )
        )
    return articles


def _normalize_publish_time(value: Any) -> Optional[str]:
    """Normalize a heterogeneous publish date to ``YYYY-MM-DD[ HH:MM:SS]``.

    Returns ``None`` when unparseable so the caller can skip the row instead of
    mis-windowing it.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    # Already ``YYYY-MM-DD`` / ``YYYY-MM-DD HH:MM:SS`` / ``...THH:MM:SS``.
    if re.match(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?", text):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})([ T](\d{2}:\d{2}(:\d{2})?))?", text)
        if m:
            date_part = m.group(1)
            time_part = m.group(3)
            return f"{date_part} {time_part}" if time_part else date_part
    # ISO-8601 with timezone (e.g. ``2026-05-10T09:00:00+08:00`` / ``...Z``).
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _emit_fetch_news_event(
    symbol: str,
    start: str,
    end: str,
    article_count: int,
    limit: int | None,
    engines: List[BaseSearchEngine],
) -> None:
    _emit_event(
        "data_provider.fetch_news",
        {
            "provider": PROVIDER_NAME_WEBSEARCH,
            "method": "fetch_news",
            "symbol": symbol,
            "start": start,
            "end": end,
            "article_count": article_count,
            "limit": limit,
            "engines": [e.name for e in engines if e.is_available],
        },
    )


def _emit_event(event_name: str, payload: dict) -> None:
    """Fire emit_debug_event as fire-and-forget when an event loop is running."""
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        # No running event loop; skip (non-async callers / import time).
        pass


__all__ = [
    "NewsWebSearchProvider",
    "BaseSearchEngine",
    "TavilySearchEngine",
    "BochaSearchEngine",
    "SearchResult",
    "SearchResponse",
    "WebSearchError",
    "WebSearchNotConfiguredError",
    "WebSearchAllEnginesFailedError",
]
