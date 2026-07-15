"""Tests for the multi-engine web-search news provider.

Network is fully mocked (``httpx.MockTransport`` for real request/parse paths,
fake engines for provider aggregation). Pins:

* the provider filters to the inclusive ``[start, end]`` window client-side,
  de-dups by URL across engines, normalizes publish times, orders most-recent
  first, and caps to ``limit``,
* a genuinely empty window returns ``[]`` (→ ``news_empty`` at the tool),
* a single engine failing does not collapse the batch (degradation),
* when every available engine fails, ``WebSearchAllEnginesFailedError`` is
  raised (→ ``news_fetch_failed``), distinct from no-engine-configured which
  raises ``WebSearchNotConfiguredError``,
* the concrete Tavily / Bocha engines parse their upstream JSON into
  ``SearchResult`` and never raise out of ``search`` (HTTP errors → success
  False),
* ``NewsArticle`` mapping tags ``provider`` with the engine name.
"""

from __future__ import annotations

import types
import unittest
from unittest.mock import patch

import httpx

from doyoutrade.core.models import NewsArticle
from doyoutrade.data.news_websearch import (
    BochaSearchEngine,
    NewsWebSearchProvider,
    SearchResponse,
    SearchResult,
    TavilySearchEngine,
    WebSearchAllEnginesFailedError,
    WebSearchNotConfiguredError,
    _normalize_publish_time,
)
from doyoutrade.data.protocols import NewsProvider


class _FakeEngine:
    """Duck-typed BaseSearchEngine stand-in for provider-level tests."""

    def __init__(self, name: str, response: SearchResponse, *, available: bool = True) -> None:
        self.name = name
        self._response = response
        self._available = available
        self.calls: list[tuple] = []

    @property
    def is_available(self) -> bool:
        return self._available

    async def search(self, query: str, max_results: int, days: int) -> SearchResponse:
        self.calls.append((query, max_results, days))
        return self._response


def _ok(name: str, results: list[SearchResult]) -> SearchResponse:
    return SearchResponse(query="q", results=results, provider=name, success=True)


def _fail(name: str, msg: str) -> SearchResponse:
    return SearchResponse(query="q", results=[], provider=name, success=False, error_message=msg)


def _result(url: str, date: str, title: str = "t") -> SearchResult:
    return SearchResult(title=title, snippet="body", url=url, source="src", published_date=date)


# ---------------------------------------------------------------------------
# Provider aggregation (fake engines)
# ---------------------------------------------------------------------------


class NewsWebSearchProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_satisfies_news_provider_protocol(self) -> None:
        provider = NewsWebSearchProvider([_FakeEngine("tavily", _ok("tavily", []))])
        self.assertIsInstance(provider, NewsProvider)

    async def test_window_filter_dedup_order_and_limit(self) -> None:
        e1 = _FakeEngine(
            "tavily",
            _ok(
                "tavily",
                [
                    _result("http://a", "2026-05-10 09:00:00", "recent"),
                    _result("http://b", "2026-04-25 10:15:22", "mid"),
                    _result("http://c", "2025-01-01 08:00:00", "old"),  # outside window
                ],
            ),
        )
        e2 = _FakeEngine(
            "bocha",
            _ok(
                "bocha",
                [
                    _result("http://a", "2026-05-10 09:00:00", "dup"),  # dup url -> dropped
                    _result("http://d", "2026-05-01 00:00:00", "extra"),
                ],
            ),
        )
        provider = NewsWebSearchProvider([e1, e2])
        articles = await provider.fetch_news(
            "600519.SH", "2026-04-01", "2026-05-29", limit=10
        )

        # c (2025) dropped by window; a de-duped across engines -> 3 unique.
        self.assertEqual([a.url for a in articles], ["http://a", "http://d", "http://b"])
        self.assertTrue(all(isinstance(a, NewsArticle) for a in articles))
        first = articles[0]
        self.assertEqual(first.publish_time, "2026-05-10 09:00:00")
        self.assertEqual(first.symbol, "600519.SH")
        self.assertEqual(first.provider, "websearch:tavily")
        self.assertEqual(first.content, "body")

    async def test_limit_caps_result(self) -> None:
        e = _FakeEngine(
            "tavily",
            _ok(
                "tavily",
                [
                    _result("http://a", "2026-05-10", "a"),
                    _result("http://b", "2026-05-09", "b"),
                ],
            ),
        )
        provider = NewsWebSearchProvider([e])
        articles = await provider.fetch_news("600519.SH", "2026-04-01", "2026-05-29", limit=1)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].url, "http://a")

    async def test_empty_window_returns_empty_not_error(self) -> None:
        e = _FakeEngine("tavily", _ok("tavily", [_result("http://a", "2020-01-01", "old")]))
        provider = NewsWebSearchProvider([e])
        articles = await provider.fetch_news("600519.SH", "2026-04-01", "2026-05-29")
        self.assertEqual(articles, [])

    async def test_undated_row_skipped(self) -> None:
        e = _FakeEngine(
            "tavily",
            _ok(
                "tavily",
                [
                    _result("http://a", "", "no_date"),
                    _result("http://b", "2026-05-10", "dated"),
                ],
            ),
        )
        provider = NewsWebSearchProvider([e])
        articles = await provider.fetch_news("600519.SH", "2026-04-01", "2026-05-29")
        self.assertEqual([a.url for a in articles], ["http://b"])

    async def test_single_engine_failure_does_not_collapse(self) -> None:
        e1 = _FakeEngine("tavily", _fail("tavily", "boom"))
        e2 = _FakeEngine("bocha", _ok("bocha", [_result("http://a", "2026-05-10", "ok")]))
        provider = NewsWebSearchProvider([e1, e2])
        articles = await provider.fetch_news("600519.SH", "2026-04-01", "2026-05-29")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].provider, "websearch:bocha")

    async def test_all_engines_failed_raises(self) -> None:
        e1 = _FakeEngine("tavily", _fail("tavily", "boom"))
        e2 = _FakeEngine("bocha", _fail("bocha", "down"))
        provider = NewsWebSearchProvider([e1, e2])
        with self.assertRaises(WebSearchAllEnginesFailedError):
            await provider.fetch_news("600519.SH", "2026-04-01", "2026-05-29")

    async def test_no_engine_configured_raises_not_configured(self) -> None:
        e = _FakeEngine("tavily", _ok("tavily", []), available=False)
        provider = NewsWebSearchProvider([e])
        with self.assertRaises(WebSearchNotConfiguredError):
            await provider.fetch_news("600519.SH", "2026-04-01", "2026-05-29")


# ---------------------------------------------------------------------------
# Concrete engines (httpx.MockTransport — real request/parse path)
# ---------------------------------------------------------------------------


def _mock_client(handler):
    def factory(timeout):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    return factory


class TavilyEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_results(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "茅台一季报",
                            "content": "营收增长",
                            "url": "http://finance.example.com/1",
                            "published_date": "2026-05-10",
                        }
                    ]
                },
            )

        engine = TavilySearchEngine(["k1"], timeout=5.0)
        with patch("doyoutrade.data.news_websearch._make_async_client", _mock_client(handler)):
            resp = await engine.search("茅台 股票", 5, 7)
        self.assertTrue(resp.success)
        self.assertEqual(len(resp.results), 1)
        r = resp.results[0]
        self.assertEqual(r.title, "茅台一季报")
        self.assertEqual(r.snippet, "营收增长")
        self.assertEqual(r.url, "http://finance.example.com/1")
        self.assertEqual(r.source, "finance.example.com")
        self.assertEqual(r.published_date, "2026-05-10")

    async def test_http_error_returns_failure_not_raise(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="unauthorized")

        engine = TavilySearchEngine(["k1"], timeout=5.0)
        with patch("doyoutrade.data.news_websearch._make_async_client", _mock_client(handler)):
            resp = await engine.search("q", 5, 7)
        self.assertFalse(resp.success)
        self.assertIn("401", resp.error_message or "")

    async def test_transport_exception_returns_failure_not_raise(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        engine = TavilySearchEngine(["k1"], timeout=5.0)
        with patch("doyoutrade.data.news_websearch._make_async_client", _mock_client(handler)):
            resp = await engine.search("q", 5, 7)
        self.assertFalse(resp.success)
        self.assertIn("ConnectError", resp.error_message or "")

    async def test_no_key_returns_failure(self) -> None:
        engine = TavilySearchEngine([], timeout=5.0)
        self.assertFalse(engine.is_available)
        resp = await engine.search("q", 5, 7)
        self.assertFalse(resp.success)


class BochaEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_results(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "webPages": {
                            "value": [
                                {
                                    "name": "茅台分红",
                                    "summary": "拟每股派息",
                                    "url": "http://news.example.cn/2",
                                    "siteName": "证券时报",
                                    "datePublished": "2026-05-10T09:00:00+08:00",
                                }
                            ]
                        }
                    },
                },
            )

        engine = BochaSearchEngine(["k1"], timeout=5.0)
        with patch("doyoutrade.data.news_websearch._make_async_client", _mock_client(handler)):
            resp = await engine.search("茅台 股票", 5, 7)
        self.assertTrue(resp.success)
        r = resp.results[0]
        self.assertEqual(r.title, "茅台分红")
        self.assertEqual(r.snippet, "拟每股派息")
        self.assertEqual(r.source, "证券时报")

    async def test_api_error_code_returns_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 403, "msg": "余额不足"})

        engine = BochaSearchEngine(["k1"], timeout=5.0)
        with patch("doyoutrade.data.news_websearch._make_async_client", _mock_client(handler)):
            resp = await engine.search("q", 5, 7)
        self.assertFalse(resp.success)
        self.assertIn("余额不足", resp.error_message or "")


# ---------------------------------------------------------------------------
# from_config
# ---------------------------------------------------------------------------


def _fake_config(tavily=(), bocha=(), timeout=10.0, max_results=10):
    ws = types.SimpleNamespace(
        tavily_api_keys=tuple(tavily),
        bocha_api_keys=tuple(bocha),
        timeout_seconds=timeout,
        max_results_per_engine=max_results,
    )
    news = types.SimpleNamespace(websearch=ws)
    data = types.SimpleNamespace(news=news)
    return types.SimpleNamespace(data=data)


class FromConfigTests(unittest.TestCase):
    def test_builds_engines_from_config(self) -> None:
        with patch("doyoutrade.config.get_config", return_value=_fake_config(tavily=("k",))):
            provider = NewsWebSearchProvider.from_config()
        engines = provider._engines
        names = {e.name for e in engines}
        self.assertEqual(names, {"tavily", "bocha"})
        by_name = {e.name: e for e in engines}
        self.assertTrue(by_name["tavily"].is_available)
        self.assertFalse(by_name["bocha"].is_available)

    def test_engine_filter_pins_single_engine(self) -> None:
        with patch(
            "doyoutrade.config.get_config",
            return_value=_fake_config(tavily=("k",), bocha=("b",)),
        ):
            provider = NewsWebSearchProvider.from_config(engine_filter="bocha")
        self.assertEqual([e.name for e in provider._engines], ["bocha"])


# ---------------------------------------------------------------------------
# publish-time normalization
# ---------------------------------------------------------------------------


class NormalizePublishTimeTests(unittest.TestCase):
    def test_variants(self) -> None:
        self.assertEqual(_normalize_publish_time("2026-05-10"), "2026-05-10")
        self.assertEqual(
            _normalize_publish_time("2026-05-10 09:00:00"), "2026-05-10 09:00:00"
        )
        self.assertEqual(
            _normalize_publish_time("2026-05-10T09:00:00+08:00"), "2026-05-10 09:00:00"
        )
        self.assertIsNone(_normalize_publish_time(None))
        self.assertIsNone(_normalize_publish_time(""))
        self.assertIsNone(_normalize_publish_time("not a date"))


if __name__ == "__main__":
    unittest.main()
