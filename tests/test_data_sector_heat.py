"""Tests for the ``doyoutrade-cli data sector-heat`` command / ``data_sector_heat`` tool.

Pins the contract:

* the akshare sector provider's ``get_sector_heat`` reuses the board-name
  endpoints (``stock_board_industry_name_em`` / ``stock_board_concept_name_em``)
  but keeps the whole-board heat columns the membership methods drop (涨跌幅 /
  总市值 / 换手率 / 上涨·下跌家数 / 领涨股 + 领涨股涨跌幅), parses by column name,
  tolerates missing columns (→ None, not raise / 0), and never coerces an
  unparseable numeric to 0,
* an empty board list returns ``[]`` (→ ``sector_heat_empty``) while a
  persistent upstream failure re-raises (→ ``sector_heat_fetch_failed``) —
  distinct failure modes,
* the tool validates sector_type / top / data_source, ranks by 涨跌幅
  descending, writes the CSV + manifest under the artifacts root, previews the
  top N, and surfaces stable error_codes for invalid_sector_type /
  unknown_data_source / unknown_arguments.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from tests._tool_result_helpers import payload as _payload
from doyoutrade.api.operations.data_sector_heat import DataSectorHeatTool
from doyoutrade.core.models import SectorHeatRow
from doyoutrade.data.protocols import SectorProvider
from doyoutrade.data.sector_akshare import AkshareSectorProvider


# ---------------------------------------------------------------------------
# Fake akshare board-name frames (documented 东方财富 schema).
# ---------------------------------------------------------------------------


def _fake_board_name_frame() -> pd.DataFrame:
    """Board-name endpoint — full documented heat columns."""
    return pd.DataFrame(
        [
            {
                "排名": 1, "板块名称": "半导体", "板块代码": "BK1036",
                "最新价": 1234.5, "涨跌额": 30.0, "涨跌幅": 2.5,
                "总市值": 4.0e12, "换手率": 3.1,
                "上涨家数": 80, "下跌家数": 5,
                "领涨股票": "中芯国际", "领涨股票-涨跌幅": 9.98,
            },
            {
                "排名": 2, "板块名称": "白酒", "板块代码": "BK0477",
                "最新价": 5678.0, "涨跌额": 50.0, "涨跌幅": 1.0,
                "总市值": 3.0e12, "换手率": 1.2,
                "上涨家数": 12, "下跌家数": 8,
                "领涨股票": "贵州茅台", "领涨股票-涨跌幅": 4.0,
            },
        ]
    )


def _fake_board_name_frame_missing_cols() -> pd.DataFrame:
    """Board-name endpoint minus the family/leader/count columns.

    Deliberately omits 板块代码 / 换手率 / 上涨家数 / 下跌家数 / 领涨股票 /
    领涨股票-涨跌幅 to exercise the missing-column-tolerance path (those fields
    must come back None / "", not raise / 0).
    """
    return pd.DataFrame(
        [
            {"排名": 1, "板块名称": "半导体", "涨跌幅": 2.5, "总市值": 4.0e12},
            {"排名": 2, "板块名称": "白酒", "涨跌幅": 1.0, "总市值": 3.0e12},
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


class AkshareSectorHeatProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_satisfies_sector_provider_protocol(self) -> None:
        self.assertIsInstance(AkshareSectorProvider(), SectorProvider)

    async def test_concept_uses_concept_endpoint_and_parses_by_column(self) -> None:
        with patch(
            "doyoutrade.data.sector_akshare.ak.stock_board_concept_name_em",
            return_value=_fake_board_name_frame(),
        ) as mock_concept, patch(
            "doyoutrade.data.sector_akshare.ak.stock_board_industry_name_em",
        ) as mock_industry:
            rows = await AkshareSectorProvider().get_sector_heat("concept")

        mock_concept.assert_called_once_with()
        mock_industry.assert_not_called()
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertIsInstance(first, SectorHeatRow)
        self.assertEqual(first.board_name, "半导体")
        self.assertEqual(first.sector_type, "concept")
        self.assertEqual(first.provider, "akshare")
        self.assertEqual(first.board_code, "BK1036")
        self.assertAlmostEqual(first.change_pct, 2.5)
        self.assertAlmostEqual(first.total_mv, 4.0e12)
        self.assertAlmostEqual(first.turnover_rate, 3.1)
        self.assertEqual(first.up_count, 80)
        self.assertEqual(first.down_count, 5)
        self.assertEqual(first.leader_stock, "中芯国际")
        self.assertAlmostEqual(first.leader_change_pct, 9.98)

    async def test_industry_uses_industry_endpoint(self) -> None:
        with patch(
            "doyoutrade.data.sector_akshare.ak.stock_board_industry_name_em",
            return_value=_fake_board_name_frame(),
        ) as mock_industry, patch(
            "doyoutrade.data.sector_akshare.ak.stock_board_concept_name_em",
        ) as mock_concept:
            rows = await AkshareSectorProvider().get_sector_heat("industry")

        mock_industry.assert_called_once_with()
        mock_concept.assert_not_called()
        self.assertEqual(rows[0].sector_type, "industry")

    async def test_tolerates_missing_columns(self) -> None:
        with patch(
            "doyoutrade.data.sector_akshare.ak.stock_board_concept_name_em",
            return_value=_fake_board_name_frame_missing_cols(),
        ):
            rows = await AkshareSectorProvider().get_sector_heat("concept")
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first.board_name, "半导体")
        self.assertAlmostEqual(first.change_pct, 2.5)
        self.assertAlmostEqual(first.total_mv, 4.0e12)
        # Missing columns → None / "" (never raise / 0).
        self.assertEqual(first.board_code, "")
        self.assertIsNone(first.turnover_rate)
        self.assertIsNone(first.up_count)
        self.assertIsNone(first.down_count)
        self.assertEqual(first.leader_stock, "")
        self.assertIsNone(first.leader_change_pct)

    async def test_empty_frame_returns_empty_list(self) -> None:
        with patch(
            "doyoutrade.data.sector_akshare.ak.stock_board_concept_name_em",
            return_value=pd.DataFrame(),
        ):
            rows = await AkshareSectorProvider().get_sector_heat("concept")
        self.assertEqual(rows, [])

    async def test_missing_board_name_column_returns_empty(self) -> None:
        # A frame without 板块名称 can't identify any board → empty, not raise.
        df = pd.DataFrame([{"涨跌幅": 2.5, "总市值": 4.0e12}])
        with patch(
            "doyoutrade.data.sector_akshare.ak.stock_board_concept_name_em",
            return_value=df,
        ):
            rows = await AkshareSectorProvider().get_sector_heat("concept")
        self.assertEqual(rows, [])

    async def test_persistent_failure_reraises(self) -> None:
        with patch(
            "doyoutrade.data.sector_akshare.ak.stock_board_concept_name_em",
            side_effect=ConnectionError("RemoteDisconnected"),
        ):
            with patch("doyoutrade.data.sector_akshare.time.sleep", return_value=None):
                with self.assertRaises(ConnectionError):
                    await AkshareSectorProvider().get_sector_heat("concept")

    async def test_unparseable_numeric_is_none_not_zero(self) -> None:
        df = _fake_board_name_frame().copy()
        df["涨跌幅"] = df["涨跌幅"].astype(object)
        df.loc[0, "涨跌幅"] = "N/A"
        with patch(
            "doyoutrade.data.sector_akshare.ak.stock_board_concept_name_em",
            return_value=df,
        ):
            rows = await AkshareSectorProvider().get_sector_heat("concept")
        self.assertIsNone(rows[0].change_pct)

    async def test_row_missing_board_name_skipped(self) -> None:
        df = _fake_board_name_frame().copy()
        df.loc[0, "板块名称"] = None
        with patch(
            "doyoutrade.data.sector_akshare.ak.stock_board_concept_name_em",
            return_value=df,
        ):
            rows = await AkshareSectorProvider().get_sector_heat("concept")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].board_name, "白酒")

    async def test_unknown_sector_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            await AkshareSectorProvider().get_sector_heat("bogus")


# ---------------------------------------------------------------------------
# Tool — fake provider injected via _build_sector_heat_provider
# ---------------------------------------------------------------------------


def _row(
    board_name: str,
    *,
    sector_type: str = "concept",
    change_pct: float | None = 1.0,
    leader: str = "",
    leader_change: float | None = None,
    up: int | None = None,
    down: int | None = None,
) -> SectorHeatRow:
    return SectorHeatRow(
        board_name=board_name,
        sector_type=sector_type,
        provider="akshare",
        change_pct=change_pct,
        leader_stock=leader,
        leader_change_pct=leader_change,
        up_count=up,
        down_count=down,
    )


class _FakeProvider:
    def __init__(self, outcome: list[SectorHeatRow] | Exception) -> None:
        self._outcome = outcome
        self.calls: list[str] = []

    async def get_sector_heat(self, sector_type: str) -> list[SectorHeatRow]:
        self.calls.append(sector_type)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _patch_provider(outcome: list[SectorHeatRow] | Exception, holder: dict | None = None):
    fake = _FakeProvider(outcome)
    if holder is not None:
        holder["provider"] = fake
    return patch(
        "doyoutrade.api.operations.data_sector_heat._build_sector_heat_provider",
        return_value=(fake, "akshare"),
    )


class DataSectorHeatToolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_ok_ranks_by_change_desc_and_writes_csv(self) -> None:
        # Out of order + one None-change to verify ranking / None-last.
        rows = [
            _row("a", change_pct=2.5, leader="中芯国际", leader_change=9.98, up=80, down=5),
            _row("b", change_pct=5.0, leader="贵州茅台", up=12, down=8),
            _row("c", change_pct=None),
        ]
        holder: dict = {}
        with _patch_provider(rows, holder):
            result = await DataSectorHeatTool().execute(sector_type="concept", top=2)

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["sector_type"], "concept")
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["top"], 2)
        # Ranked descending by 涨跌幅; None sorts last.
        self.assertEqual(len(data["latest"]), 2)
        self.assertEqual(data["latest"][0]["board_name"], "b")  # 5.0
        self.assertEqual(data["latest"][1]["board_name"], "a")  # 2.5
        self.assertEqual(data["latest"][1]["leader_stock"], "中芯国际")
        self.assertAlmostEqual(data["latest"][1]["leader_change_pct"], 9.98)
        self.assertEqual(holder["provider"].calls, ["concept"])
        p = Path(data["sector_heat_path"])
        self.assertTrue(p.exists())
        df = pd.read_csv(p)
        # Full ranking persisted (all 3), None-change row last.
        self.assertEqual(len(df), 3)
        self.assertEqual(list(df["board_name"]), ["b", "a", "c"])
        self.assertEqual(list(df.columns)[:3], ["board_name", "board_code", "sector_type"])
        self.assertTrue(Path(data["manifest_path"]).exists())

    async def test_default_top_is_30_and_default_sector_type_concept(self) -> None:
        holder: dict = {}
        with _patch_provider([_row("a")], holder):
            result = await DataSectorHeatTool().execute()
        data = _payload(result)
        self.assertEqual(data["top"], 30)
        self.assertEqual(data["sector_type"], "concept")
        self.assertEqual(holder["provider"].calls, ["concept"])

    async def test_industry_sector_type_passed_through(self) -> None:
        holder: dict = {}
        with _patch_provider([_row("钢铁", sector_type="industry", change_pct=1.0)], holder):
            result = await DataSectorHeatTool().execute(sector_type="industry")
        data = _payload(result)
        self.assertEqual(data["sector_type"], "industry")
        self.assertEqual(holder["provider"].calls, ["industry"])

    async def test_empty_is_sector_heat_empty(self) -> None:
        with _patch_provider([]):
            result = await DataSectorHeatTool().execute(sector_type="concept")
        self.assertTrue(result.is_error)
        self.assertIn("sector_heat_empty", result.text)

    async def test_provider_raises_is_fetch_failed(self) -> None:
        with _patch_provider(ConnectionError("RemoteDisconnected")):
            result = await DataSectorHeatTool().execute(sector_type="concept")
        self.assertTrue(result.is_error)
        self.assertIn("sector_heat_fetch_failed", result.text)

    async def test_invalid_sector_type(self) -> None:
        result = await DataSectorHeatTool().execute(sector_type="板块")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_sector_type", result.text)

    async def test_unknown_data_source(self) -> None:
        result = await DataSectorHeatTool().execute(data_source="tushare")
        self.assertTrue(result.is_error)
        self.assertIn("unknown_data_source", result.text)

    async def test_unknown_argument_rejected(self) -> None:
        result = await DataSectorHeatTool().execute(sector_type="concept", bogus="x")
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)


if __name__ == "__main__":
    unittest.main()
