"""Unit tests for the ``data_sector`` axis.

Coverage:

* ``DataSectorTool`` list mode + members mode (universe CSV written).
* Distinct failure modes: ``sector_empty`` (resolved but no members) vs
  ``sector_fetch_failed`` (provider raised); per-board failures don't
  collapse the run.
* kwargs contract (unknown key) + invalid_sector_type rejections.
* ``AkshareSectorProvider`` normalizes bare 6-digit codes to canonical
  symbols; persistent upstream failure re-raises, empty board returns [].
* ``_FallbackSectorProvider`` tries the next provider when one raises.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

from doyoutrade.api.operations.data_sector import DataSectorTool
from doyoutrade.core.models import SectorMember
from doyoutrade.data.protocols import ProviderCapabilities, PROVIDER_NAME_AKSHARE


def _caps() -> ProviderCapabilities:
    return ProviderCapabilities(name=PROVIDER_NAME_AKSHARE, supported_intervals=frozenset())


class _FakeSectorProvider:
    capabilities = _caps()

    def __init__(self, *, sectors=None, members_by_name=None, raise_on=None):
        self._sectors = sectors or []
        self._members = members_by_name or {}
        self._raise_on = raise_on or set()

    async def list_sectors(self, *, sector_type=None):
        return list(self._sectors)

    async def get_sector_members(self, sector_name, *, sector_type=None):
        if sector_name in self._raise_on:
            raise RuntimeError(f"simulated failure for {sector_name}")
        return list(self._members.get(sector_name, []))


def _member(sector: str, code: str, name: str) -> SectorMember:
    return SectorMember(sector_name=sector, code=code, name=name,
                        provider="akshare", sector_type="industry")


def _extract_payload(result) -> dict[str, Any]:
    match = re.search(r"```json\n(.*)\n```", result.text, re.DOTALL)
    assert match is not None, f"no fenced JSON: {result.text!r}"
    return json.loads(match.group(1))


class DataSectorToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    async def test_list_mode_returns_board_names(self) -> None:
        provider = _FakeSectorProvider(sectors=["白酒", "半导体", "银行"])
        tool = DataSectorTool(sector_provider_factory=lambda ds: provider)
        result = await tool.execute(data_source="auto")
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["mode"], "list")
        self.assertEqual(payload["sector_count"], 3)
        self.assertIn("白酒", payload["sectors"])

    async def test_members_mode_writes_universe(self) -> None:
        provider = _FakeSectorProvider(members_by_name={
            "白酒": [_member("白酒", "600519.SH", "贵州茅台"),
                     _member("白酒", "000858.SZ", "五粮液")],
        })
        out = Path(self.tmp.name) / "u.csv"
        tool = DataSectorTool(sector_provider_factory=lambda ds: provider)
        result = await tool.execute(sector_names="白酒", output_path=str(out))
        self.assertFalse(result.is_error, msg=result.text)
        payload = _extract_payload(result)
        self.assertEqual(payload["mode"], "members")
        self.assertEqual(payload["universe_size"], 2)
        self.assertEqual(out.read_text().split(), ["600519.SH", "000858.SZ"])

    async def test_members_mode_dedups_across_boards(self) -> None:
        provider = _FakeSectorProvider(members_by_name={
            "A": [_member("A", "600519.SH", "x"), _member("A", "000001.SZ", "y")],
            "B": [_member("B", "600519.SH", "x"), _member("B", "300750.SZ", "z")],
        })
        out = Path(self.tmp.name) / "u.csv"
        tool = DataSectorTool(sector_provider_factory=lambda ds: provider)
        result = await tool.execute(sector_names="A,B", output_path=str(out))
        payload = _extract_payload(result)
        self.assertEqual(payload["universe_size"], 3)  # 600519 de-duped

    async def test_sector_empty_is_distinct_failure(self) -> None:
        provider = _FakeSectorProvider(members_by_name={"白酒": []})
        tool = DataSectorTool(sector_provider_factory=lambda ds: provider)
        result = await tool.execute(sector_names="白酒")
        payload = _extract_payload(result)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["sectors"][0]["error_code"], "sector_empty")

    async def test_sector_fetch_failed_does_not_abort_run(self) -> None:
        provider = _FakeSectorProvider(
            members_by_name={"OK": [_member("OK", "600519.SH", "x")]},
            raise_on={"BAD"},
        )
        tool = DataSectorTool(sector_provider_factory=lambda ds: provider)
        result = await tool.execute(sector_names="BAD,OK")
        payload = _extract_payload(result)
        self.assertEqual(payload["status"], "partial")
        codes = {r["sector_name"]: r for r in payload["sectors"]}
        self.assertEqual(codes["BAD"]["error_code"], "sector_fetch_failed")
        self.assertEqual(codes["OK"]["status"], "ok")

    async def test_rejects_unknown_kwarg(self) -> None:
        tool = DataSectorTool(sector_provider_factory=lambda ds: _FakeSectorProvider())
        result = await tool.execute(bogus=1)
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)

    async def test_rejects_invalid_sector_type(self) -> None:
        tool = DataSectorTool(sector_provider_factory=lambda ds: _FakeSectorProvider())
        result = await tool.execute(sector_name="白酒", sector_type="bogus")
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_sector_type]", result.text)


class AkshareSectorProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_members_normalize_bare_codes(self) -> None:
        from doyoutrade.data.sector_akshare import AkshareSectorProvider

        df = pd.DataFrame({"代码": ["600519", "000858"], "名称": ["贵州茅台", "五粮液"]})
        with patch("akshare.stock_board_industry_cons_em", return_value=df):
            members = await AkshareSectorProvider().get_sector_members("白酒", sector_type="industry")
        codes = [m.code for m in members]
        self.assertEqual(codes, ["600519.SH", "000858.SZ"])
        self.assertEqual(members[0].name, "贵州茅台")

    async def test_members_empty_returns_empty_not_raise(self) -> None:
        from doyoutrade.data.sector_akshare import AkshareSectorProvider

        empty = pd.DataFrame({"代码": [], "名称": []})
        with patch("akshare.stock_board_industry_cons_em", return_value=empty):
            members = await AkshareSectorProvider().get_sector_members("空板块", sector_type="industry")
        self.assertEqual(members, [])

    async def test_members_persistent_failure_reraises(self) -> None:
        from doyoutrade.data.sector_akshare import AkshareSectorProvider

        with patch("akshare.stock_board_industry_cons_em", side_effect=RuntimeError("boom")), \
             patch("time.sleep", return_value=None):
            with self.assertRaises(RuntimeError):
                await AkshareSectorProvider().get_sector_members("白酒", sector_type="industry")


class FallbackSectorProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_through_to_next_on_failure(self) -> None:
        from doyoutrade.data.factory import _FallbackSectorProvider

        bad = _FakeSectorProvider(raise_on={"白酒"})
        good = _FakeSectorProvider(members_by_name={"白酒": [_member("白酒", "600519.SH", "x")]})
        fb = _FallbackSectorProvider([bad, good])
        members = await fb.get_sector_members("白酒")
        self.assertEqual([m.code for m in members], ["600519.SH"])

    async def test_raises_when_all_fail(self) -> None:
        from doyoutrade.data.factory import _FallbackSectorProvider

        bad1 = _FakeSectorProvider(raise_on={"白酒"})
        bad2 = _FakeSectorProvider(raise_on={"白酒"})
        fb = _FallbackSectorProvider([bad1, bad2])
        with self.assertRaises(RuntimeError):
            await fb.get_sector_members("白酒")


if __name__ == "__main__":
    unittest.main()
