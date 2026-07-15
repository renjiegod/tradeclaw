"""Unit tests for ``doyoutrade.api.operations.indicators_compute``.

Coverage:

* Success: compute rsi + macd + kdj over a real cached OHLCV CSV; assert the
  envelope status, latest values, namespaced multi-output columns, and that
  the full-series CSV lands on disk.
* ``"all"`` computes every dispatch-table indicator.
* Unknown indicator name -> ``unknown_indicator``.
* Bad JSON params -> ``invalid_params_json``; bad JSON indicators ->
  ``invalid_indicators_json``.
* Missing OHLCV cache -> ``ohlcv_csv_missing`` (structured error).
* Unknown top-level kwarg -> ``unknown_arguments`` (kwargs contract).
* ``tail`` controls the number of trailing values returned.

Artifacts are isolated by pointing ``$HOME`` at a tempdir (matching the
pattern-tool tests), so ``_get_artifacts_root`` resolves under the temp home.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from doyoutrade.api.operations.indicators_compute import (
    ALL_INDICATORS,
    IndicatorComputeTool,
)

from tests._tool_result_helpers import payload as _payload


def _make_ohlcv(n: int = 80) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    rng = np.random.RandomState(7)
    close = 10.0 + np.cumsum(rng.randn(n) * 0.4)
    high = close + rng.rand(n) * 0.6
    low = close - rng.rand(n) * 0.6
    open_ = close + (rng.rand(n) - 0.5) * 0.3
    volume = rng.randint(100_000, 1_000_000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=dates,
    )


class _HomeArtifactsMixin:
    """Provides a temp ``$HOME`` and a helper to seed the OHLCV cache."""

    def setUp(self) -> None:  # noqa: D102
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_home = os.environ.get("HOME", "")
        os.environ["HOME"] = self._tmp.name

    def tearDown(self) -> None:  # noqa: D102
        if self._orig_home:
            os.environ["HOME"] = self._orig_home
        else:
            os.environ.pop("HOME", None)
        self._tmp.cleanup()

    def _write_ohlcv(self, code: str, df: pd.DataFrame) -> None:
        artifacts = Path(self._tmp.name) / ".doyoutrade" / "assistant" / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        safe = code.replace("/", "_").replace("\\", "_").replace(":", "_")
        df.to_csv(artifacts / f"ohlcv_{safe}.csv", index=True, index_label="date")

    def _artifacts_dir(self) -> Path:
        return Path(self._tmp.name) / ".doyoutrade" / "assistant" / "artifacts"


class TestSchema(unittest.TestCase):
    def test_name_and_category(self) -> None:
        tool = IndicatorComputeTool()
        self.assertEqual(tool.name, "compute_indicators")
        self.assertEqual(tool.category, "analysis")

    def test_schema_is_closed(self) -> None:
        params = IndicatorComputeTool().parameters
        self.assertFalse(params.get("additionalProperties", True))
        self.assertIn("code", params["required"])
        self.assertEqual(params["properties"]["tail"]["default"], 1)

    def test_dispatch_covers_expected_indicators(self) -> None:
        # Spot-check a few scalar + multi-output indicators are present.
        for name in (
            "sma", "rsi", "macd", "bollinger", "adx", "kdj", "ichimoku", "psar", "zigzag"
        ):
            self.assertIn(name, ALL_INDICATORS)


class TestCompute(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_success_rsi_macd_kdj(self) -> None:
        tool = IndicatorComputeTool()
        self._write_ohlcv("600519.SH", _make_ohlcv(80))
        result = await tool.execute(
            code="600519.SH", indicators=["rsi", "macd", "kdj"], tail=1
        )
        self.assertFalse(result.is_error)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["code"], "600519.SH")
        self.assertEqual(set(data["indicators"]), {"rsi", "macd", "kdj"})

        # Multi-output indicators expand to namespaced columns.
        self.assertIn("rsi", data["latest"])
        self.assertIn("macd.macd", data["latest"])
        self.assertIn("macd.signal", data["latest"])
        self.assertIn("macd.hist", data["latest"])
        self.assertIn("kdj.k", data["latest"])

        # tail=1 -> single latest value per column.
        self.assertEqual(len(data["latest"]["rsi"]), 1)

        # report_path CSV lands on disk with the full series.
        report_path = Path(data["report_path"])
        self.assertTrue(report_path.exists())
        self.assertEqual(report_path, self._artifacts_dir() / "indicators_600519.SH.csv")
        out = pd.read_csv(report_path, index_col=0)
        self.assertIn("rsi", out.columns)
        self.assertIn("macd.signal", out.columns)
        self.assertEqual(len(out), 80)

    async def test_all_indicators(self) -> None:
        tool = IndicatorComputeTool()
        self._write_ohlcv("TEST.SZ", _make_ohlcv(120))
        result = await tool.execute(code="TEST.SZ", indicators="all", tail=2)
        self.assertFalse(result.is_error)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(set(data["indicators"]), set(ALL_INDICATORS))
        # tail=2 -> two trailing values per column.
        self.assertEqual(len(data["latest"]["sma"]), 2)

    async def test_default_indicators_is_all(self) -> None:
        tool = IndicatorComputeTool()
        self._write_ohlcv("DEF.SZ", _make_ohlcv(70))
        result = await tool.execute(code="DEF.SZ")
        data = _payload(result)
        self.assertEqual(set(data["indicators"]), set(ALL_INDICATORS))

    async def test_param_override(self) -> None:
        tool = IndicatorComputeTool()
        self._write_ohlcv("P.SZ", _make_ohlcv(80))
        r_default = await tool.execute(code="P.SZ", indicators=["rsi"], tail=1)
        r_override = await tool.execute(
            code="P.SZ", indicators=["rsi"], params={"rsi": {"period": 21}}, tail=1
        )
        v_default = _payload(r_default)["latest"]["rsi"][0]
        v_override = _payload(r_override)["latest"]["rsi"][0]
        # Different period should produce a different RSI value on this series.
        self.assertNotEqual(v_default, v_override)

    async def test_unknown_indicator(self) -> None:
        tool = IndicatorComputeTool()
        self._write_ohlcv("U.SZ", _make_ohlcv(60))
        result = await tool.execute(code="U.SZ", indicators=["rsi", "not_a_real_one"])
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_indicator]", result.text)

    async def test_bad_params_json(self) -> None:
        tool = IndicatorComputeTool()
        self._write_ohlcv("J.SZ", _make_ohlcv(60))
        # A JSON-string that does not parse -> invalid_params_json.
        result = await tool.execute(code="J.SZ", indicators=["rsi"], params="{not json")
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_params_json]", result.text)

    async def test_bad_indicators_json(self) -> None:
        tool = IndicatorComputeTool()
        self._write_ohlcv("I.SZ", _make_ohlcv(60))
        # A non-array, non-"all" JSON string -> invalid_indicators_json.
        result = await tool.execute(code="I.SZ", indicators="{not json")
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_indicators_json]", result.text)

    async def test_missing_ohlcv_cache(self) -> None:
        tool = IndicatorComputeTool()
        result = await tool.execute(code="NOPE.SZ", indicators=["rsi"])
        self.assertTrue(result.is_error)
        self.assertIn("[error:ohlcv_csv_missing]", result.text)

    async def test_unknown_top_level_kwarg(self) -> None:
        tool = IndicatorComputeTool()
        self._write_ohlcv("K.SZ", _make_ohlcv(60))
        result = await tool.execute(code="K.SZ", indicatorz=["rsi"])
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)

    async def test_json_array_string_indicators(self) -> None:
        tool = IndicatorComputeTool()
        self._write_ohlcv("C.SZ", _make_ohlcv(70))
        # A JSON array string should be coerced into a list.
        result = await tool.execute(code="C.SZ", indicators='["rsi", "sma"]')
        self.assertFalse(result.is_error)
        data = _payload(result)
        self.assertEqual(set(data["indicators"]), {"rsi", "sma"})

    async def test_plain_comma_string_indicators(self) -> None:
        # The exact shape the CLI sends (`--indicators kdj,cci`): a bare
        # comma-separated string, NOT JSON. Regression guard — this used to
        # be rejected by the array coercion before reaching _resolve_indicators.
        tool = IndicatorComputeTool()
        self._write_ohlcv("C.SZ", _make_ohlcv(70))
        result = await tool.execute(code="C.SZ", indicators="kdj,cci")
        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(set(data["indicators"]), {"kdj", "cci"})

    async def test_single_unknown_comma_name_is_unknown_indicator(self) -> None:
        # A bare (non-JSON) string with an unknown name resolves to
        # unknown_indicator, not invalid_indicators_json.
        tool = IndicatorComputeTool()
        self._write_ohlcv("C.SZ", _make_ohlcv(70))
        result = await tool.execute(code="C.SZ", indicators="not_real")
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_indicator]", result.text)


if __name__ == "__main__":
    unittest.main()
