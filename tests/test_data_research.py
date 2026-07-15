"""Tests for the ``doyoutrade-cli data reports`` command / ``data_research_reports`` tool.

Pins the contract:

* the akshare research-report provider parses the dynamic year-keyed
  forecast columns (``<year>-盈利预测-收益`` / ``<year>-盈利预测-市盈率``),
  filters to the requested ``[start, end]`` window client-side on the
  report date, caps to ``limit``, and returns most-recent-first
  ``ResearchReport`` rows,
* a persistent upstream failure re-raises (→ ``research_reports_fetch_failed``)
  while a genuinely empty window returns ``[]`` (→ ``research_reports_empty``)
  — distinct failure modes,
* the tool fans out over one/many symbols, writes a per-symbol CSV +
  manifest under the artifacts root, and reports per-symbol status without
  collapsing the run,
* the kwargs contract rejects unknown args and the symbol-input / window /
  limit validation surfaces stable error_codes.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

from tests._tool_result_helpers import payload as _payload
from doyoutrade.api.operations.data_research import DataResearchReportsTool
from doyoutrade.core.models import ResearchReport
from doyoutrade.data.protocols import ResearchReportProvider
from doyoutrade.data.research_report_akshare import AkshareResearchReportProvider


def _fake_research_df() -> pd.DataFrame:
    """Mirror akshare ``stock_research_report_em`` columns incl. dynamic years."""
    return pd.DataFrame(
        [
            {
                "序号": 1,
                "股票代码": "600519",
                "股票简称": "贵州茅台",
                "报告名称": "年报点评：稳健前行",
                "东财评级": "买入",
                "机构": "诚通证券",
                "近一月个股研报数": 12,
                "2026-盈利预测-收益": 66.68,
                "2026-盈利预测-市盈率": 19.8,
                "2027-盈利预测-收益": 69.43,
                "2027-盈利预测-市盈率": 19.1,
                "行业": "白酒Ⅱ",
                "日期": "2026-05-25",
                "报告PDF链接": "http://pdf.example.com/a",
            },
            {
                "序号": 2,
                "股票代码": "600519",
                "股票简称": "贵州茅台",
                "报告名称": "事件点评：改革初显",
                "东财评级": "增持",
                "机构": "华鑫证券",
                "近一月个股研报数": 0,
                "2026-盈利预测-收益": 68.71,
                "2026-盈利预测-市盈率": 20.2,
                "2027-盈利预测-收益": 73.01,
                "2027-盈利预测-市盈率": 19.0,
                "行业": "白酒Ⅱ",
                "日期": "2026-04-10",
                "报告PDF链接": "http://pdf.example.com/b",
            },
            {
                "序号": 3,
                "股票代码": "600519",
                "股票简称": "贵州茅台",
                "报告名称": "陈年旧报",
                "东财评级": "中性",
                "机构": "旧报证券",
                "近一月个股研报数": 0,
                "2026-盈利预测-收益": 60.0,
                "2026-盈利预测-市盈率": 22.0,
                "2027-盈利预测-收益": 63.0,
                "2027-盈利预测-市盈率": 21.0,
                "行业": "白酒Ⅱ",
                "日期": "2025-01-15",
                "报告PDF链接": "http://pdf.example.com/c",
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


class AkshareResearchReportProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_satisfies_research_report_provider_protocol(self) -> None:
        self.assertIsInstance(AkshareResearchReportProvider(), ResearchReportProvider)

    async def test_filters_window_parses_forecasts_orders_recent_first(self) -> None:
        with patch(
            "doyoutrade.data.research_report_akshare.ak.stock_research_report_em",
            return_value=_fake_research_df(),
        ):
            reports = await AkshareResearchReportProvider().fetch_research_reports(
                "600519.SH", "2026-01-01", "2026-06-30", limit=10
            )

        # The 2025 row is outside the window and dropped.
        self.assertEqual(len(reports), 2)
        self.assertTrue(all(isinstance(r, ResearchReport) for r in reports))
        # Most-recent first.
        self.assertEqual(reports[0].report_date, "2026-05-25")
        self.assertEqual(reports[1].report_date, "2026-04-10")
        first = reports[0]
        self.assertEqual(first.title, "年报点评：稳健前行")
        self.assertEqual(first.rating, "买入")
        self.assertEqual(first.institution, "诚通证券")
        self.assertEqual(first.industry, "白酒Ⅱ")
        self.assertEqual(first.recent_report_count, 12)
        self.assertEqual(first.pdf_url, "http://pdf.example.com/a")
        self.assertEqual(first.provider, "akshare")
        self.assertEqual(first.symbol, "600519.SH")
        # Dynamic year-keyed forecasts parsed correctly.
        self.assertEqual(first.eps_forecasts, {"2026": 66.68, "2027": 69.43})
        self.assertEqual(first.pe_forecasts, {"2026": 19.8, "2027": 19.1})

    async def test_limit_caps_result(self) -> None:
        with patch(
            "doyoutrade.data.research_report_akshare.ak.stock_research_report_em",
            return_value=_fake_research_df(),
        ):
            reports = await AkshareResearchReportProvider().fetch_research_reports(
                "600519.SH", "2026-01-01", "2026-06-30", limit=1
            )
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].report_date, "2026-05-25")

    async def test_empty_window_returns_empty_not_error(self) -> None:
        with patch(
            "doyoutrade.data.research_report_akshare.ak.stock_research_report_em",
            return_value=_fake_research_df(),
        ):
            reports = await AkshareResearchReportProvider().fetch_research_reports(
                "600519.SH", "2020-01-01", "2020-12-31", limit=10
            )
        self.assertEqual(reports, [])

    async def test_persistent_upstream_failure_raises(self) -> None:
        with patch(
            "doyoutrade.data.research_report_akshare.ak.stock_research_report_em",
            side_effect=RuntimeError("network down"),
        ):
            with patch("doyoutrade.data.research_report_akshare.time.sleep", return_value=None):
                with self.assertRaises(RuntimeError):
                    await AkshareResearchReportProvider().fetch_research_reports(
                        "600519.SH", "2026-01-01", "2026-06-30"
                    )

    async def test_unparseable_date_row_is_skipped_loudly(self) -> None:
        df = _fake_research_df().copy()
        df.loc[0, "日期"] = "not-a-date"
        with patch(
            "doyoutrade.data.research_report_akshare.ak.stock_research_report_em",
            return_value=df,
        ):
            reports = await AkshareResearchReportProvider().fetch_research_reports(
                "600519.SH", "2026-01-01", "2026-06-30", limit=10
            )
        # The bad-date row is dropped; the other in-window row survives.
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].report_date, "2026-04-10")


# ---------------------------------------------------------------------------
# Tool — fake provider injected via _build_research_report_provider
# ---------------------------------------------------------------------------


def _report(symbol: str, report_date: str, title: str) -> ResearchReport:
    return ResearchReport(
        symbol=symbol,
        title=title,
        rating="买入",
        institution="src",
        report_date=report_date,
        pdf_url=f"http://pdf.example.com/{title}",
        provider="akshare",
        industry="白酒Ⅱ",
        recent_report_count=5,
        eps_forecasts={"2026": 66.0},
        pe_forecasts={"2026": 20.0},
    )


class _FakeProvider:
    def __init__(self, mapping: dict[str, Any]) -> None:
        # mapping: code -> list[ResearchReport] | Exception
        self._mapping = mapping

    async def fetch_research_reports(self, symbol, start, end, *, limit=None):
        outcome = self._mapping.get(symbol, [])
        if isinstance(outcome, Exception):
            raise outcome
        return list(outcome)


def _patch_provider(mapping: dict[str, Any]):
    return patch(
        "doyoutrade.api.operations.data_research._build_research_report_provider",
        return_value=(_FakeProvider(mapping), "akshare"),
    )


class DataResearchReportsToolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_single_symbol_writes_csv_and_manifest(self) -> None:
        mapping = {
            "600519.SH": [
                _report("600519.SH", "2026-05-25", "b"),
                _report("600519.SH", "2026-04-10", "a"),
            ]
        }
        with _patch_provider(mapping):
            result = await DataResearchReportsTool().execute(
                code="600519.SH", start_date="2026-01-01", end_date="2026-06-30"
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
        self.assertEqual(symbol["report_count"], 2)
        self.assertEqual(len(symbol["latest"]), 2)
        # CSV exists with the documented column order.
        reports_path = Path(symbol["reports_path"])
        self.assertTrue(reports_path.exists())
        df = pd.read_csv(reports_path)
        self.assertEqual(
            list(df.columns),
            [
                "report_date",
                "title",
                "rating",
                "institution",
                "industry",
                "recent_report_count",
                "eps_forecasts",
                "pe_forecasts",
                "pdf_url",
            ],
        )
        self.assertEqual(len(df), 2)
        # Forecast dicts round-trip as JSON strings.
        self.assertEqual(json.loads(df.iloc[0]["eps_forecasts"]), {"2026": 66.0})
        # Manifest written.
        self.assertTrue(Path(data["manifest_path"]).exists())

    async def test_research_reports_empty_is_distinct_failure(self) -> None:
        with _patch_provider({"600519.SH": []}):
            result = await DataResearchReportsTool().execute(code="600519.SH")

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "failed")
        symbol = data["symbols"][0]
        self.assertEqual(symbol["status"], "failed")
        self.assertEqual(symbol["error_code"], "research_reports_empty")

    async def test_research_reports_fetch_failed_carries_error_type(self) -> None:
        with _patch_provider({"600519.SH": RuntimeError("boom")}):
            result = await DataResearchReportsTool().execute(code="600519.SH")

        data = _payload(result)
        symbol = data["symbols"][0]
        self.assertEqual(symbol["status"], "failed")
        self.assertEqual(symbol["error_code"], "research_reports_fetch_failed")
        self.assertEqual(symbol["error_type"], "RuntimeError")

    async def test_partial_run_does_not_collapse(self) -> None:
        mapping = {
            "600519.SH": [_report("600519.SH", "2026-05-25", "ok")],
            "000001.SZ": [],
        }
        with _patch_provider(mapping):
            result = await DataResearchReportsTool().execute(
                symbols="600519.SH,000001.SZ"
            )
        data = _payload(result)
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["symbols_succeeded"], 1)
        self.assertEqual(data["symbols_failed"], 1)

    async def test_unknown_argument_rejected(self) -> None:
        result = await DataResearchReportsTool().execute(code="600519.SH", bogus="x")
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)

    async def test_missing_symbol_input(self) -> None:
        result = await DataResearchReportsTool().execute(period="1mo")
        self.assertTrue(result.is_error)
        self.assertIn("missing_symbol_input", result.text)

    async def test_conflicting_symbol_args(self) -> None:
        result = await DataResearchReportsTool().execute(code="600519.SH", symbols="000001.SZ")
        self.assertTrue(result.is_error)
        self.assertIn("conflicting_symbol_args", result.text)

    async def test_invalid_limit(self) -> None:
        result = await DataResearchReportsTool().execute(code="600519.SH", limit=-1)
        self.assertTrue(result.is_error)
        self.assertIn("invalid_limit", result.text)

    async def test_unknown_data_source(self) -> None:
        result = await DataResearchReportsTool().execute(code="600519.SH", data_source="tushare")
        self.assertTrue(result.is_error)
        self.assertIn("unknown_data_source", result.text)

    async def test_period_window_resolves(self) -> None:
        mapping = {"600519.SH": [_report("600519.SH", "2026-05-25", "ok")]}
        with _patch_provider(mapping):
            result = await DataResearchReportsTool().execute(
                code="600519.SH", period="1mo"
            )
        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        # period resolves to concrete requested_start / requested_end dates.
        self.assertIsNotNone(data["requested_start"])
        self.assertIsNotNone(data["requested_end"])


if __name__ == "__main__":
    unittest.main()
