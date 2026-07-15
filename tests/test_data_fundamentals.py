"""Unit tests for the ``data_fundamentals`` axis.

Coverage:

* ``DataFundamentalsTool`` happy path (CSV written) + ``missing`` reporting
  + unknown-kwarg / conflicting-input rejections.
* ``AkshareFundamentalsProvider`` parses the spot snapshot, normalizes bare
  codes to canonical symbols, filters to the requested universe, and maps
  NaN cells to None.
* ``_FallbackFundamentalsProvider`` falls through on a raising provider.
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

from doyoutrade.api.operations.data_fundamentals import DataFundamentalsTool
from doyoutrade.core.models import Fundamentals
from doyoutrade.data.protocols import ProviderCapabilities, PROVIDER_NAME_AKSHARE


def _caps():
    return ProviderCapabilities(name=PROVIDER_NAME_AKSHARE, supported_intervals=frozenset())


class _FakeFund:
    capabilities = _caps()

    def __init__(self, m, *, raise_=False):
        self._m = m
        self._raise = raise_

    async def get_fundamentals_batch(self, symbols, *, asof=None):
        if self._raise:
            raise RuntimeError("boom")
        return {s: self._m[s] for s in symbols if s in self._m}

    async def get_fundamentals(self, symbol, *, asof=None):
        if self._raise:
            raise RuntimeError("boom")
        return self._m.get(symbol)


def _payload(result) -> dict[str, Any]:
    return json.loads(re.search(r"```json\n(.*)\n```", result.text, re.DOTALL).group(1))


class DataFundamentalsToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    async def test_happy_path_writes_csv(self) -> None:
        fmap = {
            "600519.SH": Fundamentals(code="600519.SH", float_mv=2e12, pe=30.0, provider="akshare"),
        }
        out = Path(self.tmp.name) / "f.csv"
        tool = DataFundamentalsTool(fundamentals_provider_factory=lambda ds: _FakeFund(fmap))
        result = await tool.execute(code="600519.SH", output_path=str(out))
        self.assertFalse(result.is_error, msg=result.text)
        p = _payload(result)
        self.assertEqual(p["symbols_matched"], 1)
        self.assertTrue(out.exists())
        self.assertIn("float_mv", out.read_text())

    async def test_missing_symbol_reported(self) -> None:
        fmap = {"600519.SH": Fundamentals(code="600519.SH", float_mv=2e12, provider="akshare")}
        tool = DataFundamentalsTool(fundamentals_provider_factory=lambda ds: _FakeFund(fmap))
        result = await tool.execute(symbols="600519.SH,000001.SZ")
        p = _payload(result)
        self.assertEqual(p["status"], "partial")
        self.assertIn("000001.SZ", p["missing"])

    async def test_rejects_unknown_kwarg(self) -> None:
        tool = DataFundamentalsTool(fundamentals_provider_factory=lambda ds: _FakeFund({}))
        result = await tool.execute(bogus=1)
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)

    async def test_rejects_conflicting_inputs(self) -> None:
        tool = DataFundamentalsTool(fundamentals_provider_factory=lambda ds: _FakeFund({}))
        result = await tool.execute(code="600519.SH", symbols="000001.SZ")
        self.assertTrue(result.is_error)
        self.assertIn("[error:conflicting_symbol_args]", result.text)

    async def test_fetch_failure_surfaces_error_code(self) -> None:
        tool = DataFundamentalsTool(fundamentals_provider_factory=lambda ds: _FakeFund({}, raise_=True))
        result = await tool.execute(code="600519.SH")
        self.assertTrue(result.is_error)
        self.assertIn("[error:fundamentals_fetch_failed]", result.text)


class AkshareFundamentalsProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_parsed_and_filtered(self) -> None:
        from doyoutrade.data.fundamentals_akshare import AkshareFundamentalsProvider

        df = pd.DataFrame({
            "代码": ["600519", "000001", "300750"],
            "最新价": [1700.0, 12.0, 200.0],
            "流通市值": [2.1e12, 2.3e11, 9.0e11],
            "总市值": [2.1e12, 2.4e11, 9.5e11],
            "市盈率-动态": [30.0, float("nan"), 25.0],
            "市净率": [8.0, 0.6, 5.0],
        })
        with patch("akshare.stock_zh_a_spot_em", return_value=df):
            out = await AkshareFundamentalsProvider().get_fundamentals_batch(["600519.SH", "000001.SZ"])
        self.assertEqual(set(out), {"600519.SH", "000001.SZ"})
        self.assertAlmostEqual(out["600519.SH"].float_mv, 2.1e12)
        self.assertEqual(out["600519.SH"].pe, 30.0)
        # NaN PE → None
        self.assertIsNone(out["000001.SZ"].pe)

    async def test_persistent_failure_reraises(self) -> None:
        from doyoutrade.data.fundamentals_akshare import AkshareFundamentalsProvider

        with patch("akshare.stock_zh_a_spot_em", side_effect=RuntimeError("net")), \
             patch("time.sleep", return_value=None):
            with self.assertRaises(RuntimeError):
                await AkshareFundamentalsProvider().get_fundamentals_batch(["600519.SH"])


class FallbackFundamentalsProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_through_on_failure(self) -> None:
        from doyoutrade.data.factory import _FallbackFundamentalsProvider

        good = {"600519.SH": Fundamentals(code="600519.SH", float_mv=2e12, provider="qmt")}
        fb = _FallbackFundamentalsProvider([_FakeFund({}, raise_=True), _FakeFund(good)])
        out = await fb.get_fundamentals_batch(["600519.SH"])
        self.assertEqual(out["600519.SH"].float_mv, 2e12)


if __name__ == "__main__":
    unittest.main()
