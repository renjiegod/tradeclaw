"""Tests for the ``doyoutrade-cli data fund-flow`` command / ``data_fund_flow`` tool.

Pins the contract:

* the akshare fund-flow provider fetches ``stock_individual_fund_flow_rank`` /
  ``stock_sector_fund_flow_rank``, matches columns by **substring** (so the
  period prefix on individual columns doesn't break parsing), tolerates missing
  columns on the sector endpoint (→ None, not raise), canonicalizes individual
  codes to CODE.EXCHANGE, and never coerces an unparseable numeric to 0,
* an empty result returns ``[]`` (→ ``fund_flow_empty``) while a persistent
  upstream failure re-raises (→ ``fund_flow_fetch_failed``) — distinct failure
  modes,
* the tool validates scope / period (per-scope allowed set — sector has no 3日)
  / sector_type, ranks by main net inflow descending, writes the CSV + manifest
  under the artifacts root, and surfaces stable error_codes for invalid_period /
  invalid_sector_type / unknown_data_source / unknown_arguments.
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
from doyoutrade.api.operations.data_fund_flow import DataFundFlowTool
from doyoutrade.core.models import FundFlowRow
from doyoutrade.data.fund_flow_akshare import AkshareFundFlowProvider
from doyoutrade.data.protocols import FundFlowProvider


# ---------------------------------------------------------------------------
# Fake akshare frames.
# ---------------------------------------------------------------------------


def _fake_individual_frame(period: str = "今日") -> pd.DataFrame:
    """Individual endpoint — columns are PERIOD-PREFIXED (matched by substring)."""
    p = period
    return pd.DataFrame(
        [
            {
                "序号": 1, "代码": "600519", "名称": "贵州茅台", "最新价": 1800.0,
                f"{p}涨跌幅": 9.98,
                f"{p}主力净流入-净额": 5.0e8, f"{p}主力净流入-净占比": 12.3,
                f"{p}超大单净流入-净额": 3.0e8, f"{p}超大单净流入-净占比": 7.0,
                f"{p}大单净流入-净额": 2.0e8, f"{p}大单净流入-净占比": 5.0,
                f"{p}中单净流入-净额": -1.0e8, f"{p}中单净流入-净占比": -2.0,
                f"{p}小单净流入-净额": -1.0e8, f"{p}小单净流入-净占比": -2.0,
            },
            {
                "序号": 2, "代码": "000001", "名称": "平安银行", "最新价": 12.0,
                f"{p}涨跌幅": 3.0,
                f"{p}主力净流入-净额": 8.0e8, f"{p}主力净流入-净占比": 15.0,
                f"{p}超大单净流入-净额": 5.0e8, f"{p}超大单净流入-净占比": 9.0,
                f"{p}大单净流入-净额": 3.0e8, f"{p}大单净流入-净占比": 6.0,
                f"{p}中单净流入-净额": -2.0e8, f"{p}中单净流入-净占比": -3.0,
                f"{p}小单净流入-净额": -6.0e8, f"{p}小单净流入-净占比": -11.0,
            },
        ]
    )


def _fake_sector_frame() -> pd.DataFrame:
    """Sector endpoint — exact columns unconfirmed online; match by substring.

    Deliberately omits the 超大单 / 大单 / 中单 / 小单 columns to exercise the
    missing-column-tolerance path (those fields must come back None, not raise).
    """
    return pd.DataFrame(
        [
            {
                "序号": 1, "名称": "半导体", "今日涨跌幅": 2.5,
                "今日主力净流入-净额": 4.0e9, "今日主力净流入-净占比": 3.1,
                "领涨股": "中芯国际",
            },
            {
                "序号": 2, "名称": "白酒", "今日涨跌幅": 1.0,
                "今日主力净流入-净额": 1.0e9, "今日主力净流入-净占比": 0.8,
                "领涨股": "贵州茅台",
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


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AkshareFundFlowProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_satisfies_fund_flow_provider_protocol(self) -> None:
        self.assertIsInstance(AkshareFundFlowProvider(), FundFlowProvider)

    async def test_individual_substring_column_match_and_canonicalize(self) -> None:
        with patch(
            "doyoutrade.data.fund_flow_akshare.ak.stock_individual_fund_flow_rank",
            return_value=_fake_individual_frame("今日"),
        ) as mock_fn:
            rows = await AkshareFundFlowProvider().fetch_fund_flow(
                "individual", "今日"
            )
        mock_fn.assert_called_once_with(indicator="今日")
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertIsInstance(first, FundFlowRow)
        self.assertEqual(first.scope, "individual")
        self.assertEqual(first.code, "600519")
        self.assertEqual(first.symbol, "600519.SH")
        self.assertEqual(first.name, "贵州茅台")
        self.assertAlmostEqual(first.change_pct, 9.98)
        self.assertAlmostEqual(first.main_net_amount, 5.0e8)
        self.assertAlmostEqual(first.main_net_pct, 12.3)
        self.assertAlmostEqual(first.super_large_net_amount, 3.0e8)
        self.assertAlmostEqual(first.small_net_amount, -1.0e8)
        self.assertEqual(rows[1].symbol, "000001.SZ")

    async def test_individual_period_prefix_5day_still_matches(self) -> None:
        # Columns prefixed with 5日 must still parse via substring match.
        with patch(
            "doyoutrade.data.fund_flow_akshare.ak.stock_individual_fund_flow_rank",
            return_value=_fake_individual_frame("5日"),
        ) as mock_fn:
            rows = await AkshareFundFlowProvider().fetch_fund_flow(
                "individual", "5日"
            )
        mock_fn.assert_called_once_with(indicator="5日")
        self.assertAlmostEqual(rows[0].main_net_amount, 5.0e8)

    async def test_sector_tolerates_missing_columns(self) -> None:
        with patch(
            "doyoutrade.data.fund_flow_akshare.ak.stock_sector_fund_flow_rank",
            return_value=_fake_sector_frame(),
        ) as mock_fn:
            rows = await AkshareFundFlowProvider().fetch_fund_flow(
                "sector", "今日", sector_type="概念资金流"
            )
        mock_fn.assert_called_once_with(indicator="今日", sector_type="概念资金流")
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first.scope, "sector")
        self.assertEqual(first.name, "半导体")
        self.assertEqual(first.code, "")
        self.assertEqual(first.symbol, "")
        self.assertAlmostEqual(first.main_net_amount, 4.0e9)
        self.assertEqual(first.lead_stock, "中芯国际")
        # Missing 超大单 / 大单 / 中单 / 小单 columns → None, not raise / 0.
        self.assertIsNone(first.super_large_net_amount)
        self.assertIsNone(first.large_net_amount)
        self.assertIsNone(first.medium_net_amount)
        self.assertIsNone(first.small_net_amount)

    async def test_empty_frame_returns_empty_list(self) -> None:
        with patch(
            "doyoutrade.data.fund_flow_akshare.ak.stock_individual_fund_flow_rank",
            return_value=pd.DataFrame(),
        ):
            rows = await AkshareFundFlowProvider().fetch_fund_flow(
                "individual", "今日"
            )
        self.assertEqual(rows, [])

    async def test_persistent_failure_reraises(self) -> None:
        with patch(
            "doyoutrade.data.fund_flow_akshare.ak.stock_individual_fund_flow_rank",
            side_effect=ConnectionError("RemoteDisconnected"),
        ):
            with patch("doyoutrade.data.fund_flow_akshare.time.sleep", return_value=None):
                with self.assertRaises(ConnectionError):
                    await AkshareFundFlowProvider().fetch_fund_flow(
                        "individual", "今日"
                    )

    async def test_unparseable_numeric_is_none_not_zero(self) -> None:
        df = _fake_individual_frame("今日").copy()
        df["今日主力净流入-净额"] = df["今日主力净流入-净额"].astype(object)
        df.loc[0, "今日主力净流入-净额"] = "N/A"
        with patch(
            "doyoutrade.data.fund_flow_akshare.ak.stock_individual_fund_flow_rank",
            return_value=df,
        ):
            rows = await AkshareFundFlowProvider().fetch_fund_flow(
                "individual", "今日"
            )
        self.assertIsNone(rows[0].main_net_amount)

    async def test_row_missing_name_skipped(self) -> None:
        df = _fake_individual_frame("今日").copy()
        df.loc[0, "名称"] = None
        with patch(
            "doyoutrade.data.fund_flow_akshare.ak.stock_individual_fund_flow_rank",
            return_value=df,
        ):
            rows = await AkshareFundFlowProvider().fetch_fund_flow(
                "individual", "今日"
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "平安银行")

    async def test_unknown_scope_raises(self) -> None:
        with self.assertRaises(ValueError):
            await AkshareFundFlowProvider().fetch_fund_flow("bogus", "今日")


# ---------------------------------------------------------------------------
# Tool — fake provider injected via _build_fund_flow_provider
# ---------------------------------------------------------------------------


def _row(
    scope: str,
    name: str,
    *,
    code: str = "",
    symbol: str = "",
    main: float | None = 1.0e8,
    lead: str = "",
) -> FundFlowRow:
    return FundFlowRow(
        scope=scope, name=name, provider="akshare", code=code, symbol=symbol,
        main_net_amount=main, lead_stock=lead,
    )


class _FakeProvider:
    def __init__(self, outcome: list[FundFlowRow] | Exception) -> None:
        self._outcome = outcome
        self.calls: list[tuple[str, str, str | None]] = []

    async def fetch_fund_flow(
        self, scope: str, period: str, *, sector_type: str | None = None
    ) -> list[FundFlowRow]:
        self.calls.append((scope, period, sector_type))
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _patch_provider(outcome: list[FundFlowRow] | Exception, holder: dict | None = None):
    fake = _FakeProvider(outcome)
    if holder is not None:
        holder["provider"] = fake
    return patch(
        "doyoutrade.api.operations.data_fund_flow._build_fund_flow_provider",
        return_value=(fake, "akshare"),
    )


class DataFundFlowToolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_ok_individual_ranks_and_writes_csv(self) -> None:
        # Provide rows out of order + one None-amount to verify ranking / None-last.
        rows = [
            _row("individual", "a", code="600519", symbol="600519.SH", main=5.0e8),
            _row("individual", "b", code="000001", symbol="000001.SZ", main=8.0e8),
            _row("individual", "c", code="300750", symbol="300750.SZ", main=None),
        ]
        with _patch_provider(rows):
            result = await DataFundFlowTool().execute(scope="individual", period="今日", top=2)

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["scope"], "individual")
        self.assertEqual(data["period"], "今日")
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["top"], 2)
        self.assertNotIn("sector_type", data)
        # Ranked descending by main net inflow; None sorts last.
        self.assertEqual(len(data["latest"]), 2)
        self.assertEqual(data["latest"][0]["name"], "b")  # 8e8
        self.assertEqual(data["latest"][1]["name"], "a")  # 5e8
        p = Path(data["fund_flow_path"])
        self.assertTrue(p.exists())
        df = pd.read_csv(p)
        # Full ranking persisted (all 3 rows), None-amount row last.
        self.assertEqual(len(df), 3)
        self.assertEqual(list(df["name"]), ["b", "a", "c"])
        self.assertEqual(list(df.columns)[:4], ["scope", "symbol", "code", "name"])
        self.assertTrue(Path(data["manifest_path"]).exists())

    async def test_default_top_is_30(self) -> None:
        with _patch_provider([_row("individual", "a")]):
            result = await DataFundFlowTool().execute()
        data = _payload(result)
        self.assertEqual(data["top"], 30)
        self.assertEqual(data["scope"], "individual")
        self.assertEqual(data["period"], "今日")

    async def test_ok_sector_maps_sector_type_and_carries_it(self) -> None:
        holder: dict = {}
        with _patch_provider([_row("sector", "半导体", main=4.0e9, lead="中芯国际")], holder):
            result = await DataFundFlowTool().execute(
                scope="sector", period="今日", sector_type="概念"
            )
        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["scope"], "sector")
        self.assertEqual(data["sector_type"], "概念")
        self.assertEqual(data["latest"][0]["lead_stock"], "中芯国际")
        # Upstream token was mapped from 概念 → 概念资金流.
        self.assertEqual(holder["provider"].calls, [("sector", "今日", "概念资金流")])

    async def test_sector_default_sector_type_is_concept(self) -> None:
        holder: dict = {}
        with _patch_provider([_row("sector", "半导体", main=1.0)], holder):
            result = await DataFundFlowTool().execute(scope="sector", period="今日")
        data = _payload(result)
        self.assertEqual(data["sector_type"], "概念")
        self.assertEqual(holder["provider"].calls, [("sector", "今日", "概念资金流")])

    async def test_empty_is_fund_flow_empty(self) -> None:
        with _patch_provider([]):
            result = await DataFundFlowTool().execute(scope="individual", period="今日")
        self.assertTrue(result.is_error)
        self.assertIn("fund_flow_empty", result.text)

    async def test_provider_raises_is_fetch_failed(self) -> None:
        with _patch_provider(ConnectionError("RemoteDisconnected")):
            result = await DataFundFlowTool().execute(scope="individual", period="今日")
        self.assertTrue(result.is_error)
        self.assertIn("fund_flow_fetch_failed", result.text)

    async def test_sector_rejects_3day_period(self) -> None:
        # sector scope has no 3日.
        result = await DataFundFlowTool().execute(scope="sector", period="3日")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_period", result.text)

    async def test_individual_accepts_3day_period(self) -> None:
        with _patch_provider([_row("individual", "a")]):
            result = await DataFundFlowTool().execute(scope="individual", period="3日")
        self.assertFalse(result.is_error, msg=result.text)
        self.assertEqual(_payload(result)["period"], "3日")

    async def test_invalid_period_value(self) -> None:
        result = await DataFundFlowTool().execute(scope="individual", period="7日")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_period", result.text)

    async def test_invalid_sector_type(self) -> None:
        result = await DataFundFlowTool().execute(scope="sector", sector_type="板块")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_sector_type", result.text)

    async def test_unknown_data_source(self) -> None:
        result = await DataFundFlowTool().execute(data_source="tushare")
        self.assertTrue(result.is_error)
        self.assertIn("unknown_data_source", result.text)

    async def test_unknown_argument_rejected(self) -> None:
        result = await DataFundFlowTool().execute(scope="individual", bogus="x")
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)


if __name__ == "__main__":
    unittest.main()
