"""Tests for the ``doyoutrade-cli data news`` command / ``data_news`` tool.

Pins the contract:

* the akshare news provider normalizes publish times, filters to the
  requested ``[start, end]`` window client-side, caps to ``limit``, and
  returns most-recent-first ``NewsArticle`` rows,
* a persistent upstream failure re-raises (→ ``news_fetch_failed``) while a
  genuinely empty window returns ``[]`` (→ ``news_empty``) — distinct
  failure modes,
* the tool fans out over one/many symbols, writes a per-symbol CSV +
  manifest under the artifacts root, and reports per-symbol status without
  collapsing the run,
* the kwargs contract rejects unknown args and the symbol-input / window /
  limit validation surfaces stable error_codes.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

from tests._tool_result_helpers import payload as _payload
from doyoutrade.api.operations.data_news import DataNewsTool
from doyoutrade.core.models import NewsArticle
from doyoutrade.data.news_akshare import AkshareNewsProvider
from doyoutrade.data.protocols import NewsProvider


def _fake_news_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "关键词": "600519",
                "新闻标题": "茅台一季报",
                "新闻内容": "营收增长",
                "发布时间": "2026-04-25 10:15:22",
                "文章来源": "界面新闻",
                "新闻链接": "http://example.com/a",
            },
            {
                "关键词": "600519",
                "新闻标题": "茅台分红",
                "新闻内容": "拟每股派息",
                "发布时间": "2026-05-10 09:00:00",
                "文章来源": "证券时报",
                "新闻链接": "http://example.com/b",
            },
            {
                "关键词": "600519",
                "新闻标题": "陈年旧闻",
                "新闻内容": "过期",
                "发布时间": "2025-01-01 08:00:00",
                "文章来源": "旧闻社",
                "新闻链接": "http://example.com/c",
            },
        ]
    )


class _HomeArtifactsMixin:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp.name

    def tearDown(self) -> None:
        if self._orig_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._orig_home
        self._tmp.cleanup()

    @property
    def artifacts_dir(self) -> Path:
        return Path(self._tmp.name) / ".doyoutrade" / "assistant" / "artifacts"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AkshareNewsProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_satisfies_news_provider_protocol(self) -> None:
        self.assertIsInstance(AkshareNewsProvider(), NewsProvider)

    async def test_filters_window_caps_limit_and_orders_recent_first(self) -> None:
        with patch(
            "doyoutrade.data.news_akshare.ak.stock_news_em",
            return_value=_fake_news_df(),
        ):
            articles = await AkshareNewsProvider().fetch_news(
                "600519.SH", "2026-04-01", "2026-05-29", limit=10
            )

        # The 2025 row is outside the window and dropped.
        self.assertEqual(len(articles), 2)
        self.assertTrue(all(isinstance(a, NewsArticle) for a in articles))
        # Most-recent first.
        self.assertEqual(articles[0].publish_time, "2026-05-10 09:00:00")
        self.assertEqual(articles[1].publish_time, "2026-04-25 10:15:22")
        first = articles[0]
        self.assertEqual(first.title, "茅台分红")
        self.assertEqual(first.source, "证券时报")
        self.assertEqual(first.url, "http://example.com/b")
        self.assertEqual(first.provider, "akshare")
        self.assertEqual(first.symbol, "600519.SH")

    async def test_limit_caps_result(self) -> None:
        with patch(
            "doyoutrade.data.news_akshare.ak.stock_news_em",
            return_value=_fake_news_df(),
        ):
            articles = await AkshareNewsProvider().fetch_news(
                "600519.SH", "2026-04-01", "2026-05-29", limit=1
            )
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].publish_time, "2026-05-10 09:00:00")

    async def test_empty_window_returns_empty_not_error(self) -> None:
        with patch(
            "doyoutrade.data.news_akshare.ak.stock_news_em",
            return_value=_fake_news_df(),
        ):
            articles = await AkshareNewsProvider().fetch_news(
                "600519.SH", "2020-01-01", "2020-12-31", limit=10
            )
        self.assertEqual(articles, [])

    async def test_persistent_upstream_failure_raises(self) -> None:
        with patch(
            "doyoutrade.data.news_akshare.ak.stock_news_em",
            side_effect=RuntimeError("network down"),
        ):
            with patch("doyoutrade.data.news_akshare.time.sleep", return_value=None):
                with self.assertRaises(RuntimeError):
                    await AkshareNewsProvider().fetch_news(
                        "600519.SH", "2026-04-01", "2026-05-29"
                    )


# ---------------------------------------------------------------------------
# Tool — fake provider injected via _build_news_provider
# ---------------------------------------------------------------------------


def _article(symbol: str, publish_time: str, title: str) -> NewsArticle:
    return NewsArticle(
        symbol=symbol,
        title=title,
        content="body",
        publish_time=publish_time,
        source="src",
        url=f"http://example.com/{title}",
        provider="akshare",
        keyword=symbol,
    )


class _FakeProvider:
    def __init__(self, mapping: dict[str, Any]) -> None:
        # mapping: code -> list[NewsArticle] | Exception
        self._mapping = mapping

    async def fetch_news(self, symbol, start, end, *, limit=None):
        outcome = self._mapping.get(symbol, [])
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome)


def _patch_provider(mapping: dict[str, Any]):
    return patch(
        "doyoutrade.api.operations.data_news._build_news_provider",
        return_value=(_FakeProvider(mapping), "akshare"),
    )


class DataNewsToolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_single_symbol_writes_csv_and_manifest(self) -> None:
        mapping = {
            "600519.SH": [
                _article("600519.SH", "2026-05-10 09:00:00", "b"),
                _article("600519.SH", "2026-04-25 10:15:22", "a"),
            ]
        }
        with _patch_provider(mapping):
            result = await DataNewsTool().execute(
                code="600519.SH", start_date="2026-04-01", end_date="2026-05-29"
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["symbols_total"], 1)
        self.assertEqual(data["symbols_succeeded"], 1)
        symbol = data["symbols"][0]
        self.assertEqual(symbol["code"], "600519.SH")
        self.assertEqual(symbol["status"], "ok")
        self.assertEqual(symbol["data_source"], "akshare")
        self.assertEqual(symbol["article_count"], 2)
        self.assertEqual(len(symbol["latest"]), 2)
        # CSV exists with the documented column order.
        news_path = Path(symbol["news_path"])
        self.assertTrue(news_path.exists())
        df = pd.read_csv(news_path)
        self.assertEqual(
            list(df.columns),
            ["publish_time", "title", "source", "url", "keyword", "content"],
        )
        self.assertEqual(len(df), 2)
        # Manifest written.
        self.assertTrue(Path(data["manifest_path"]).exists())

    async def test_news_empty_is_distinct_failure(self) -> None:
        with _patch_provider({"600519.SH": []}):
            result = await DataNewsTool().execute(code="600519.SH")

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "failed")
        symbol = data["symbols"][0]
        self.assertEqual(symbol["status"], "failed")
        self.assertEqual(symbol["error_code"], "news_empty")

    async def test_news_fetch_failed_carries_error_type(self) -> None:
        with _patch_provider({"600519.SH": RuntimeError("boom")}):
            result = await DataNewsTool().execute(code="600519.SH")

        data = _payload(result)
        symbol = data["symbols"][0]
        self.assertEqual(symbol["status"], "failed")
        self.assertEqual(symbol["error_code"], "news_fetch_failed")
        self.assertEqual(symbol["error_type"], "RuntimeError")

    async def test_partial_run_does_not_collapse(self) -> None:
        mapping = {
            "600519.SH": [_article("600519.SH", "2026-05-10 09:00:00", "ok")],
            "000001.SZ": [],
        }
        with _patch_provider(mapping):
            result = await DataNewsTool().execute(
                symbols="600519.SH,000001.SZ"
            )
        data = _payload(result)
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["symbols_succeeded"], 1)
        self.assertEqual(data["symbols_failed"], 1)

    async def test_unknown_argument_rejected(self) -> None:
        result = await DataNewsTool().execute(code="600519.SH", bogus="x")
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)

    async def test_missing_symbol_input(self) -> None:
        result = await DataNewsTool().execute(period="1mo")
        self.assertTrue(result.is_error)
        self.assertIn("missing_symbol_input", result.text)

    async def test_conflicting_symbol_args(self) -> None:
        result = await DataNewsTool().execute(code="600519.SH", symbols="000001.SZ")
        self.assertTrue(result.is_error)
        self.assertIn("conflicting_symbol_args", result.text)

    async def test_invalid_limit(self) -> None:
        result = await DataNewsTool().execute(code="600519.SH", limit=-1)
        self.assertTrue(result.is_error)
        self.assertIn("invalid_limit", result.text)

    async def test_unknown_data_source(self) -> None:
        result = await DataNewsTool().execute(code="600519.SH", data_source="tushare")
        self.assertTrue(result.is_error)
        self.assertIn("unknown_data_source", result.text)


if __name__ == "__main__":
    unittest.main()
