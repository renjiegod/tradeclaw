"""Tests for the ``doyoutrade-cli data earnings`` command / ``data_earnings`` tool.

Pins the contract:

* the akshare earnings provider pulls each report period full-market once,
  filters to the requested symbols, and maps bare codes back to canonical
  ``CODE.EXCHANGE`` symbols; a persistent per-period failure is recorded but
  does NOT abort the batch,
* report-period resolution turns a date window into the quarter-end tokens
  (``YYYYMMDD``) that fall inside it,
* the tool fans out batch-style (one fetch per kind, shared across symbols),
  writes per-(symbol, kind) CSVs + a manifest, and reports per-symbol status
  without collapsing the run,
* the kwargs contract rejects unknown args and the symbol-input / window /
  kind / report-period validation surfaces stable error_codes.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

from tests._tool_result_helpers import payload as _payload
from doyoutrade.api.operations.data_earnings import (
    DataEarningsTool,
    _resolve_report_periods,
)
from doyoutrade.core.models import EarningsExpress, EarningsForecast
from doyoutrade.data.earnings_akshare import AkshareEarningsProvider
from doyoutrade.data.protocols import EarningsProvider


def _fake_forecast_df() -> pd.DataFrame:
    """Mirror akshare ``stock_yjyg_em`` columns (full-market, 2 symbols)."""
    return pd.DataFrame(
        [
            {
                "股票代码": "600519",
                "股票简称": "贵州茅台",
                "预测指标": "净利润",
                "业绩变动": "预计净利润同比增长",
                "预测数值": 8.5e10,
                "业绩变动幅度": 15.2,
                "业绩变动原因": "主业增长",
                "预告类型": "预增",
                "上年同期值": 7.4e10,
                "公告日期": "2025-01-28",
            },
            {
                "股票代码": "000001",
                "股票简称": "平安银行",
                "预测指标": "净利润",
                "业绩变动": "预计净利润略减",
                "预测数值": 4.0e10,
                "业绩变动幅度": -2.1,
                "业绩变动原因": "减值",
                "预告类型": "略减",
                "上年同期值": 4.1e10,
                "公告日期": "2025-01-20",
            },
        ]
    )


def _fake_express_df() -> pd.DataFrame:
    """Mirror akshare ``stock_yjkb_em`` columns (full-market, 1 symbol)."""
    return pd.DataFrame(
        [
            {
                "股票代码": "600519",
                "股票简称": "贵州茅台",
                "每股收益": 66.0,
                "营业收入-营业收入": 1.5e11,
                "营业收入-去年同期": 1.3e11,
                "营业收入-同比增长": 15.0,
                "营业收入-季度环比增长": 5.0,
                "净利润-净利润": 8.5e10,
                "净利润-去年同期": 7.4e10,
                "净利润-同比增长": 15.2,
                "净利润-季度环比增长": 4.0,
                "每股净资产": 200.0,
                "净资产收益率": 30.0,
                "所处行业": "白酒Ⅱ",
                "公告日期": "2025-02-28",
            }
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
# Report-period resolution
# ---------------------------------------------------------------------------


class ReportPeriodResolutionTests(unittest.TestCase):
    def test_full_year_covers_four_quarters(self) -> None:
        periods = _resolve_report_periods(date(2024, 1, 1), date(2024, 12, 31))
        self.assertEqual(periods, ["20240331", "20240630", "20240930", "20241231"])

    def test_partial_window_picks_inner_quarter_ends(self) -> None:
        periods = _resolve_report_periods(date(2024, 5, 1), date(2024, 9, 15))
        self.assertEqual(periods, ["20240630"])

    def test_window_before_any_quarter_end_is_empty(self) -> None:
        periods = _resolve_report_periods(date(2024, 1, 1), date(2024, 3, 30))
        self.assertEqual(periods, [])

    def test_crosses_year_boundary(self) -> None:
        periods = _resolve_report_periods(date(2023, 10, 1), date(2024, 3, 31))
        self.assertEqual(periods, ["20231231", "20240331"])


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AkshareEarningsProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_satisfies_earnings_provider_protocol(self) -> None:
        self.assertIsInstance(AkshareEarningsProvider(), EarningsProvider)

    async def test_forecast_filters_to_requested_symbols(self) -> None:
        with patch(
            "doyoutrade.data.earnings_akshare.ak.stock_yjyg_em",
            return_value=_fake_forecast_df(),
        ):
            result = await AkshareEarningsProvider().fetch_earnings_forecasts(
                ["600519.SH"], ["20241231"]
            )
        # Only the requested symbol survives; the full-market row for
        # 000001 is filtered out.
        self.assertEqual(set(result.keys()), {"600519.SH"})
        rows = result["600519.SH"]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIsInstance(r, EarningsForecast)
        self.assertEqual(r.symbol, "600519.SH")
        self.assertEqual(r.name, "贵州茅台")
        self.assertEqual(r.report_period, "20241231")
        self.assertEqual(r.preannounce_type, "预增")
        self.assertEqual(r.forecast_indicator, "净利润")
        self.assertEqual(r.change_pct, 15.2)
        self.assertEqual(r.prev_year_value, 7.4e10)
        self.assertEqual(r.forecast_value, 8.5e10)
        self.assertEqual(r.announce_date, "2025-01-28")
        self.assertEqual(r.provider, "akshare")

    async def test_express_filters_and_maps_fields(self) -> None:
        with patch(
            "doyoutrade.data.earnings_akshare.ak.stock_yjkb_em",
            return_value=_fake_express_df(),
        ):
            result = await AkshareEarningsProvider().fetch_earnings_express(
                ["600519.SH", "000001.SZ"], ["20241231"]
            )
        # Only 600519 had a row; 000001 absent (not an error).
        self.assertEqual(set(result.keys()), {"600519.SH"})
        r = result["600519.SH"][0]
        self.assertIsInstance(r, EarningsExpress)
        self.assertEqual(r.eps, 66.0)
        self.assertEqual(r.net_profit, 8.5e10)
        self.assertEqual(r.net_profit_prev_yoy, 15.2)
        self.assertEqual(r.roe, 30.0)
        self.assertEqual(r.industry, "白酒Ⅱ")
        self.assertEqual(r.announce_date, "2025-02-28")

    async def test_empty_when_symbol_not_in_market(self) -> None:
        with patch(
            "doyoutrade.data.earnings_akshare.ak.stock_yjyg_em",
            return_value=_fake_forecast_df(),
        ):
            result = await AkshareEarningsProvider().fetch_earnings_forecasts(
                ["999999.SH"], ["20241231"]
            )
        self.assertEqual(result, {})

    async def test_per_period_failure_does_not_abort_batch(self) -> None:
        # First period fails, second succeeds — the failed one is skipped,
        # not raised, so the batch still returns the successful period.
        call_count = {"n": 0}

        def _fake(date):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("network down")
            return _fake_forecast_df()

        with patch("doyoutrade.data.earnings_akshare.ak.stock_yjyg_em", side_effect=_fake):
            with patch("doyoutrade.data.earnings_akshare.time.sleep", return_value=None):
                result = await AkshareEarningsProvider().fetch_earnings_forecasts(
                    ["600519.SH"], ["20240930", "20241231"]
                )
        # The failed period contributed nothing; the successful one did.
        self.assertIn("600519.SH", result)
        self.assertTrue(any(r.report_period == "20241231" for r in result["600519.SH"]))


# ---------------------------------------------------------------------------
# Tool — fake provider injected via _build_earnings_provider
# ---------------------------------------------------------------------------


def _forecast(symbol: str, period: str) -> EarningsForecast:
    return EarningsForecast(
        symbol=symbol,
        name="test",
        report_period=period,
        preannounce_type="预增",
        announce_date="2025-01-28",
        provider="akshare",
        forecast_indicator="净利润",
        forecast_value=8.5e10,
        change_pct=15.2,
        prev_year_value=7.4e10,
    )


def _express(symbol: str, period: str) -> EarningsExpress:
    return EarningsExpress(
        symbol=symbol,
        name="test",
        report_period=period,
        announce_date="2025-02-28",
        provider="akshare",
        eps=66.0,
        net_profit=8.5e10,
        net_profit_prev_yoy=15.2,
        roe=30.0,
        industry="白酒Ⅱ",
    )


class _FakeEarningsProvider:
    def __init__(self, forecast_map, express_map) -> None:
        self._forecast_map = forecast_map
        self._express_map = express_map

    async def fetch_earnings_forecasts(self, symbols, report_periods):
        return {k: list(v) for k, v in self._forecast_map.items() if k in symbols}

    async def fetch_earnings_express(self, symbols, report_periods):
        return {k: list(v) for k, v in self._express_map.items() if k in symbols}


def _patch_provider(forecast_map, express_map):
    return patch(
        "doyoutrade.api.operations.data_earnings._build_earnings_provider",
        return_value=(_FakeEarningsProvider(forecast_map, express_map), "akshare"),
    )


class DataEarningsToolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_both_kinds_writes_two_csvs_and_manifest(self) -> None:
        forecast_map = {"600519.SH": [_forecast("600519.SH", "20241231")]}
        express_map = {"600519.SH": [_express("600519.SH", "20241231")]}
        with _patch_provider(forecast_map, express_map):
            result = await DataEarningsTool().execute(
                code="600519.SH", period="1y", kind="both"
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["kind"], "both")
        self.assertEqual(data["symbols_succeeded"], 1)
        symbol = data["symbols"][0]
        self.assertEqual(symbol["code"], "600519.SH")
        self.assertEqual(symbol["status"], "ok")
        self.assertEqual(symbol["forecast"]["count"], 1)
        self.assertEqual(symbol["express"]["count"], 1)
        # Two CSVs written with documented column orders.
        fpath = Path(symbol["forecast"]["path"])
        epath = Path(symbol["express"]["path"])
        self.assertTrue(fpath.exists())
        self.assertTrue(epath.exists())
        fdf = pd.read_csv(fpath)
        edf = pd.read_csv(epath)
        self.assertEqual(str(fdf.iloc[0]["report_period"]), "20241231")
        self.assertEqual(fdf.iloc[0]["preannounce_type"], "预增")
        self.assertEqual(edf.iloc[0]["eps"], 66.0)
        self.assertTrue(Path(data["manifest_path"]).exists())

    async def test_kind_forecast_only(self) -> None:
        forecast_map = {"600519.SH": [_forecast("600519.SH", "20241231")]}
        with _patch_provider(forecast_map, {}):
            result = await DataEarningsTool().execute(
                code="600519.SH", kind="forecast"
            )
        data = _payload(result)
        self.assertEqual(data["kind"], "forecast")
        symbol = data["symbols"][0]
        self.assertIn("forecast", symbol)
        self.assertNotIn("express", symbol)

    async def test_earnings_empty_distinct_failure(self) -> None:
        with _patch_provider({}, {}):
            result = await DataEarningsTool().execute(code="999999.SH")
        data = _payload(result)
        self.assertEqual(data["status"], "failed")
        symbol = data["symbols"][0]
        self.assertEqual(symbol["status"], "failed")
        self.assertEqual(symbol["error_code"], "earnings_empty")

    async def test_partial_run_does_not_collapse(self) -> None:
        forecast_map = {"600519.SH": [_forecast("600519.SH", "20241231")]}
        with _patch_provider(forecast_map, {}):
            result = await DataEarningsTool().execute(
                symbols="600519.SH,000001.SZ", kind="forecast"
            )
        data = _payload(result)
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["symbols_succeeded"], 1)
        self.assertEqual(data["symbols_failed"], 1)

    async def test_unknown_argument_rejected(self) -> None:
        result = await DataEarningsTool().execute(code="600519.SH", bogus="x")
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)

    async def test_missing_symbol_input(self) -> None:
        result = await DataEarningsTool().execute(period="1y")
        self.assertTrue(result.is_error)
        self.assertIn("missing_symbol_input", result.text)

    async def test_conflicting_symbol_args(self) -> None:
        result = await DataEarningsTool().execute(code="600519.SH", symbols="000001.SZ")
        self.assertTrue(result.is_error)
        self.assertIn("conflicting_symbol_args", result.text)

    async def test_invalid_kind(self) -> None:
        result = await DataEarningsTool().execute(code="600519.SH", kind="bogus")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_kind", result.text)

    async def test_unknown_data_source(self) -> None:
        result = await DataEarningsTool().execute(code="600519.SH", data_source="tushare")
        self.assertTrue(result.is_error)
        self.assertIn("unknown_data_source", result.text)

    async def test_no_report_periods_when_window_too_narrow(self) -> None:
        # A window that contains no quarter-end (e.g. all of Jan 1–Mar 30).
        result = await DataEarningsTool().execute(
            code="600519.SH", start_date="2024-01-01", end_date="2024-03-30"
        )
        self.assertTrue(result.is_error)
        self.assertIn("no_report_periods", result.text)


if __name__ == "__main__":
    unittest.main()
