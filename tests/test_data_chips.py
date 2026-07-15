"""Tests for the ``doyoutrade-cli data chips`` command / ``data_chips`` tool.

Pins the contract:

* the akshare 筹码分布 provider fetches ``stock_cyq_em`` for one symbol,
  parses the 中文 columns, defaults to the single latest day (``days=1``),
  and never coerces an unparseable numeric to 0 (NaN excluded, not 0.0),
* a genuinely empty result (ETF/index/delisted name) returns ``[]`` (→
  ``chip_distribution_empty``) while a persistent upstream failure re-raises
  (→ ``chip_distribution_fetch_failed``) — distinct failure modes,
* the tool validates ``symbol`` / ``days`` / ``data_source``, writes the CSV +
  manifest under the artifacts root, and surfaces stable error_codes for
  invalid_symbol / invalid_days / unknown_data_source / unknown_arguments.
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
from doyoutrade.api.operations.data_chips import DataChipsTool
from doyoutrade.core.models import ChipDistributionRow
from doyoutrade.data.chip_distribution_akshare import (
    AkshareChipDistributionProvider,
    _ensure_working_mini_racer,
)

# ---------------------------------------------------------------------------
# Fake akshare frame (mirrors the documented column names / ordering).
# ---------------------------------------------------------------------------


def _fake_cyq_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "日期": "2026-07-10", "获利比例": 0.42, "平均成本": 60.12,
                "90成本-低": 50.23, "90成本-高": 66.28, "90集中度": 0.15,
                "70成本-低": 56.0, "70成本-高": 63.08, "70集中度": 0.08,
            },
            {
                "日期": "2026-07-13", "获利比例": 0.10, "平均成本": 58.90,
                "90成本-低": 50.23, "90成本-高": 66.28, "90集中度": 0.18,
                "70成本-低": 54.01, "70成本-高": 57.5, "70集中度": 0.09,
            },
            {
                "日期": "2026-07-14", "获利比例": 0.61, "平均成本": 57.30,
                "90成本-低": 50.23, "90成本-高": 66.28, "90集中度": 0.20,
                "70成本-低": 52.12, "70成本-高": 59.41, "70集中度": 0.10,
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


class AkshareChipDistributionProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_to_latest_single_day(self) -> None:
        with patch(
            "doyoutrade.data.chip_distribution_akshare.ak.stock_cyq_em",
            return_value=_fake_cyq_frame(),
        ) as mock_fn:
            rows = await AkshareChipDistributionProvider().fetch_chip_distribution(
                "000636.SZ"
            )
        mock_fn.assert_called_once_with(symbol="000636")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIsInstance(row, ChipDistributionRow)
        self.assertEqual(row.symbol, "000636.SZ")
        self.assertEqual(row.date, "2026-07-14")
        self.assertAlmostEqual(row.profit_ratio, 0.61)
        self.assertAlmostEqual(row.avg_cost, 57.30)
        self.assertAlmostEqual(row.concentration_90, 0.20)
        self.assertAlmostEqual(row.concentration_70, 0.10)
        self.assertEqual(row.provider, "akshare")

    async def test_days_returns_a_trend_window_oldest_first(self) -> None:
        with patch(
            "doyoutrade.data.chip_distribution_akshare.ak.stock_cyq_em",
            return_value=_fake_cyq_frame(),
        ):
            rows = await AkshareChipDistributionProvider().fetch_chip_distribution(
                "000636.SZ", days=3
            )
        self.assertEqual(len(rows), 3)
        self.assertEqual([r.date for r in rows], ["2026-07-10", "2026-07-13", "2026-07-14"])

    async def test_symbol_suffix_stripped_before_akshare_call(self) -> None:
        with patch(
            "doyoutrade.data.chip_distribution_akshare.ak.stock_cyq_em",
            return_value=_fake_cyq_frame(),
        ) as mock_fn:
            await AkshareChipDistributionProvider().fetch_chip_distribution("600519.SH")
        mock_fn.assert_called_once_with(symbol="600519")

    async def test_empty_frame_returns_empty_list(self) -> None:
        with patch(
            "doyoutrade.data.chip_distribution_akshare.ak.stock_cyq_em",
            return_value=pd.DataFrame(),
        ):
            rows = await AkshareChipDistributionProvider().fetch_chip_distribution(
                "512880.SH"  # ETF — no 筹码分布 upstream
            )
        self.assertEqual(rows, [])

    async def test_persistent_failure_reraises(self) -> None:
        with patch(
            "doyoutrade.data.chip_distribution_akshare.ak.stock_cyq_em",
            side_effect=RuntimeError("network down"),
        ), patch(
            "doyoutrade.data.chip_distribution_akshare.time.sleep", return_value=None
        ):
            with self.assertRaises(RuntimeError):
                await AkshareChipDistributionProvider().fetch_chip_distribution("000636.SZ")

    async def test_unparseable_numeric_is_none_not_zero(self) -> None:
        df = _fake_cyq_frame().copy()
        df["获利比例"] = df["获利比例"].astype(object)
        df.loc[2, "获利比例"] = "N/A"
        with patch(
            "doyoutrade.data.chip_distribution_akshare.ak.stock_cyq_em",
            return_value=df,
        ):
            rows = await AkshareChipDistributionProvider().fetch_chip_distribution(
                "000636.SZ"
            )
        self.assertIsNone(rows[0].profit_ratio)

    async def test_nan_excluded_not_propagated_as_value(self) -> None:
        df = _fake_cyq_frame().copy()
        df.loc[2, "平均成本"] = float("nan")
        with patch(
            "doyoutrade.data.chip_distribution_akshare.ak.stock_cyq_em",
            return_value=df,
        ):
            rows = await AkshareChipDistributionProvider().fetch_chip_distribution(
                "000636.SZ"
            )
        self.assertIsNone(rows[0].avg_cost)


# ---------------------------------------------------------------------------
# py-mini-racer==0.6.0 legacy-shim workaround
# ---------------------------------------------------------------------------


class EnsureWorkingMiniRacerTests(unittest.TestCase):
    """``ak.stock_cyq_em`` needs a working ``py_mini_racer.MiniRacer`` for its
    embedded JS 筹码分布 calculation. On this project's pinned
    ``py-mini-racer==0.6.0``, the package's own default wiring points at a
    legacy shim whose C-API calls don't match the bundled dylib — these tests
    pin that :func:`_ensure_working_mini_racer` correctly detects and repairs
    that specific case, and leaves an already-working class alone.
    """

    def setUp(self) -> None:
        import py_mini_racer

        self._py_mini_racer = py_mini_racer
        self._original = py_mini_racer.MiniRacer

    def tearDown(self) -> None:
        self._py_mini_racer.MiniRacer = self._original

    def test_repairs_broken_legacy_shim(self) -> None:
        from py_mini_racer._mini_racer import MiniRacer as _WorkingMiniRacer

        # Force the broken default back on, as if freshly imported.
        self._py_mini_racer.MiniRacer = self._py_mini_racer.py_mini_racer.MiniRacer

        _ensure_working_mini_racer()

        self.assertIs(self._py_mini_racer.MiniRacer, _WorkingMiniRacer)

    def test_noop_when_already_working(self) -> None:
        from py_mini_racer._mini_racer import MiniRacer as _WorkingMiniRacer

        self._py_mini_racer.MiniRacer = _WorkingMiniRacer

        _ensure_working_mini_racer()

        # Still the exact same class object — not re-wrapped / replaced.
        self.assertIs(self._py_mini_racer.MiniRacer, _WorkingMiniRacer)


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


def _row(date: str, *, profit_ratio: float = 0.5) -> ChipDistributionRow:
    return ChipDistributionRow(
        symbol="000636.SZ",
        date=date,
        provider="akshare",
        profit_ratio=profit_ratio,
        avg_cost=57.3,
        cost_90_low=50.23,
        cost_90_high=66.28,
        concentration_90=0.20,
        cost_70_low=52.12,
        cost_70_high=59.41,
        concentration_70=0.10,
    )


class _FakeProvider:
    def __init__(self, outcome: "list[ChipDistributionRow] | Exception") -> None:
        self._outcome = outcome

    async def fetch_chip_distribution(
        self, symbol: str, *, days: int = 1
    ) -> "list[ChipDistributionRow]":
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _patch_provider(outcome: "list[ChipDistributionRow] | Exception"):
    return patch(
        "doyoutrade.api.operations.data_chips._build_chip_distribution_provider",
        return_value=(_FakeProvider(outcome), "akshare"),
    )


class DataChipsToolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_ok_writes_csv_and_envelope(self) -> None:
        with _patch_provider([_row("2026-07-14")]):
            result = await DataChipsTool().execute(symbol="000636.SZ")

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["symbol"], "000636.SZ")
        self.assertEqual(data["days"], 1)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["data_source"], "akshare")
        self.assertEqual(data["latest"][0]["date"], "2026-07-14")
        p = Path(data["chips_path"])
        self.assertTrue(p.exists())
        df = pd.read_csv(p)
        self.assertEqual(list(df.columns)[:3], ["symbol", "date", "profit_ratio"])
        self.assertEqual(len(df), 1)
        self.assertTrue(Path(data["manifest_path"]).exists())

    async def test_days_forwarded_to_provider(self) -> None:
        rows = [_row("2026-07-10"), _row("2026-07-13"), _row("2026-07-14")]
        with _patch_provider(rows):
            result = await DataChipsTool().execute(symbol="000636.SZ", days=3)
        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["days"], 3)
        self.assertEqual(data["count"], 3)

    async def test_empty_is_chip_distribution_empty(self) -> None:
        with _patch_provider([]):
            result = await DataChipsTool().execute(symbol="512880.SH")
        self.assertTrue(result.is_error)
        self.assertIn("chip_distribution_empty", result.text)

    async def test_provider_raises_is_fetch_failed(self) -> None:
        with _patch_provider(RuntimeError("upstream exploded")):
            result = await DataChipsTool().execute(symbol="000636.SZ")
        self.assertTrue(result.is_error)
        self.assertIn("chip_distribution_fetch_failed", result.text)

    async def test_missing_symbol_rejected(self) -> None:
        result = await DataChipsTool().execute()
        self.assertTrue(result.is_error)
        self.assertIn("invalid_symbol", result.text)

    async def test_blank_symbol_rejected(self) -> None:
        result = await DataChipsTool().execute(symbol="   ")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_symbol", result.text)

    async def test_invalid_days_type_rejected(self) -> None:
        result = await DataChipsTool().execute(symbol="000636.SZ", days="lots")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_days", result.text)

    async def test_days_out_of_range_rejected(self) -> None:
        result = await DataChipsTool().execute(symbol="000636.SZ", days=0)
        self.assertTrue(result.is_error)
        self.assertIn("invalid_days", result.text)

        result = await DataChipsTool().execute(symbol="000636.SZ", days=91)
        self.assertTrue(result.is_error)
        self.assertIn("invalid_days", result.text)

    async def test_unknown_data_source(self) -> None:
        result = await DataChipsTool().execute(symbol="000636.SZ", data_source="tushare")
        self.assertTrue(result.is_error)
        self.assertIn("unknown_data_source", result.text)

    async def test_unknown_argument_rejected(self) -> None:
        result = await DataChipsTool().execute(symbol="000636.SZ", bogus="x")
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
