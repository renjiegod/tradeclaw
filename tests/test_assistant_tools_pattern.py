from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from doyoutrade.api.operations.pattern import (
    PatternRecognitionTool,
    find_peaks_valleys,
    candlestick_patterns,
    _candlestick_summary,
    support_resistance,
    trend_line_slope,
    head_and_shoulders,
    double_top_bottom,
    triangle,
    broadening,
)

from tests._tool_result_helpers import payload as _payload
class TestPatternRecognitionTool(unittest.IsolatedAsyncioTestCase):
    """Tests for PatternRecognitionTool."""

    def test_tool_name(self) -> None:
        tool = PatternRecognitionTool()
        self.assertEqual(tool.name, "pattern_recognition")

    def test_tool_category(self) -> None:
        tool = PatternRecognitionTool()
        self.assertEqual(tool.category, "analysis")

    def test_tool_parameters(self) -> None:
        tool = PatternRecognitionTool()
        params = tool.parameters
        self.assertEqual(params["type"], "object")
        self.assertIn("code", params["properties"])
        self.assertEqual(params["properties"]["code"]["type"], "string")
        self.assertIn("required", params)
        self.assertIn("code", params["required"])

        self.assertIn("patterns", params["properties"])
        self.assertEqual(params["properties"]["patterns"]["type"], "string")
        self.assertEqual(params["properties"]["patterns"]["default"], "all")

        self.assertIn("window", params["properties"])
        self.assertEqual(params["properties"]["window"]["type"], "integer")
        self.assertEqual(params["properties"]["window"]["default"], 10)

    def test_csv_not_found(self) -> None:
        """Execute with non-existent code returns error status."""
        tool = PatternRecognitionTool()
        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                result = tool.execute_sync(code="NONEXISTENT.SZ")
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertTrue(result.is_error)
            self.assertIn("CSV not found", result.text)


class TestPatternFunctions(unittest.TestCase):
    """Test pure pandas pattern functions directly."""

    def _make_ohlcv(self, n: int = 50) -> pd.DataFrame:
        """Create synthetic OHLCV data for testing."""
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        np.random.seed(42)
        close = 10.0 + np.cumsum(np.random.randn(n) * 0.5)
        high = close + np.random.rand(n) * 0.5
        low = close - np.random.rand(n) * 0.5
        open_ = close + (np.random.rand(n) - 0.5) * 0.3
        volume = np.random.randint(100000, 1000000, n)
        df = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=dates,
        )
        return df

    def test_find_peaks_valleys(self) -> None:
        close = pd.Series([1, 2, 3, 2, 1, 2, 3, 2, 1])
        result = find_peaks_valleys(close, window=1)
        self.assertIn("peaks", result)
        self.assertIn("valleys", result)
        self.assertIsInstance(result["peaks"], list)
        self.assertIsInstance(result["valleys"], list)

    def test_candlestick_patterns(self) -> None:
        df = self._make_ohlcv(20)
        result = candlestick_patterns(df["open"], df["high"], df["low"], df["close"])
        self.assertEqual(len(result), len(df))
        self.assertTrue(set(result.unique()).issubset({-1, 0, 1}))

    def test_support_resistance(self) -> None:
        close = pd.Series([1, 2, 3, 2, 1, 2, 3, 2, 1, 2, 3, 2, 1] * 3)
        result = support_resistance(close, window=2, num_levels=3)
        self.assertIn("support", result)
        self.assertIn("resistance", result)
        self.assertIsInstance(result["support"], list)
        self.assertIsInstance(result["resistance"], list)

    def test_trend_line_slope(self) -> None:
        close = pd.Series(range(20, 70, 2))  # upward trend
        result = trend_line_slope(close, window=10)
        self.assertEqual(len(result), len(close))
        mean_slope = result.dropna().mean()
        self.assertGreater(mean_slope, 0)

    def test_head_and_shoulders(self) -> None:
        close = pd.Series([1, 2, 3, 2, 1, 2, 1, 2, 3, 2, 1])
        result = head_and_shoulders(close, window=2)
        self.assertEqual(len(result), len(close))
        self.assertTrue(set(result.unique()).issubset({0, 1}))

    def test_double_top_bottom(self) -> None:
        # Double top pattern: two peaks at similar levels
        close = pd.Series([1, 2, 3, 2, 1, 2, 3.05, 2, 1])
        result = double_top_bottom(close, window=2)
        self.assertEqual(len(result), len(close))
        self.assertTrue(set(result.unique()).issubset({-1, 0, 1}))

    def test_triangle(self) -> None:
        close = pd.Series(range(20))
        result = triangle(close, window=5)
        self.assertEqual(len(result), len(close))
        self.assertTrue(set(result.unique()).issubset({-1, 0, 1}))

    def test_broadening(self) -> None:
        # Broadening pattern: peaks rising, valleys falling
        peaks = [1, 3, 5, 7, 9]
        valleys = [9, 7, 5, 3, 1]
        close = pd.Series(peaks + valleys)
        result = broadening(close, window=3)
        self.assertEqual(len(result), len(close))
        self.assertTrue(set(result.unique()).issubset({0, 1}))

    def test_candlestick_summary_no_double_count(self) -> None:
        """Each bar should be counted exactly once (Bullish/Bearish/Neutral, not both Hammer and Engulfing)."""
        df = self._make_ohlcv(50)
        result = _candlestick_summary(df["open"], df["high"], df["low"], df["close"])
        self.assertIn("Bullish", result)
        self.assertIn("Bearish", result)
        self.assertIn("Neutral", result)
        self.assertIn("Doji", result)
        # No double-counting: Bullish + Bearish + Neutral should equal total bars
        total = result["Bullish"] + result["Bearish"] + result["Neutral"]
        self.assertEqual(total, len(df))
        # Hammer and Bullish_Engulfing must NOT both appear (the old double-count bug)
        self.assertNotIn("Hammer", result)
        self.assertNotIn("Bullish_Engulfing", result)
        self.assertNotIn("Bearish_Engulfing", result)


class TestPatternRecognitionToolExecute(unittest.IsolatedAsyncioTestCase):
    """Integration tests for PatternRecognitionTool.execute()."""

    def _write_csv(self, home: str, code: str, df: pd.DataFrame) -> None:
        artifacts = Path(home) / ".doyoutrade" / "assistant" / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        safe = code.replace("/", "_").replace("\\", "_").replace(":", "_")
        df.to_csv(artifacts / f"ohlcv_{safe}.csv", index=True, index_label="date")

    def test_execute_with_real_csv(self) -> None:
        """Execute with a real CSV returns ok status."""
        tool = PatternRecognitionTool()
        df = pd.DataFrame(
            {
                "open": [10.0, 10.5, 10.3, 10.8, 11.0],
                "high": [10.8, 11.0, 10.9, 11.2, 11.5],
                "low": [9.8, 10.2, 10.1, 10.5, 10.8],
                "close": [10.5, 10.8, 10.6, 11.0, 11.2],
                "volume": [100000, 110000, 105000, 120000, 130000],
            },
            index=pd.date_range("2024-01-01", periods=5, freq="B"),
        )

        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                self._write_csv(tmp_home, "TEST123.SZ", df)
                result = tool.execute_sync(code="TEST123.SZ")
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertEqual(_payload(result)["status"], "ok")
            self.assertEqual(_payload(result)["code"], "TEST123.SZ")
            self.assertEqual(_payload(result)["window"], 10)
            self.assertIn("patterns", _payload(result))

    def test_execute_unknown_pattern_returns_error(self) -> None:
        """Unknown pattern names in patterns parameter return an error."""
        tool = PatternRecognitionTool()
        df = pd.DataFrame(
            {
                "open": [10.0, 10.5, 10.3],
                "high": [10.8, 11.0, 10.9],
                "low": [9.8, 10.2, 10.1],
                "close": [10.5, 10.8, 10.6],
                "volume": [100000, 110000, 105000],
            },
            index=pd.date_range("2024-01-01", periods=3, freq="B"),
        )
        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                self._write_csv(tmp_home, "TEST456.SZ", df)
                result = tool.execute_sync(code="TEST456.SZ", patterns="candlestick,unknown_pattern")
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertTrue(result.is_error)
            self.assertIn("Unknown patterns", result.text)
            self.assertIn("[error:unknown_patterns]", result.text)

    def test_execute_subset_patterns(self) -> None:
        """When patterns parameter selects a subset, only those patterns appear in result."""
        tool = PatternRecognitionTool()
        df = pd.DataFrame(
            {
                "open": [10.0, 10.5, 10.3],
                "high": [10.8, 11.0, 10.9],
                "low": [9.8, 10.2, 10.1],
                "close": [10.5, 10.8, 10.6],
                "volume": [100000, 110000, 105000],
            },
            index=pd.date_range("2024-01-01", periods=3, freq="B"),
        )
        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                self._write_csv(tmp_home, "TEST789.SZ", df)
                result = tool.execute_sync(code="TEST789.SZ", patterns="candlestick,trend_slope")
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertEqual(_payload(result)["status"], "ok")
            # Only the two requested patterns should be present
            self.assertIn("candlestick", _payload(result)["patterns"])
            self.assertIn("trend_slope", _payload(result)["patterns"])
            self.assertNotIn("support_resistance", _payload(result)["patterns"])
            self.assertNotIn("head_and_shoulders", _payload(result)["patterns"])
            self.assertNotIn("double_top_bottom", _payload(result)["patterns"])
            self.assertNotIn("triangle", _payload(result)["patterns"])
            self.assertNotIn("broadening", _payload(result)["patterns"])
