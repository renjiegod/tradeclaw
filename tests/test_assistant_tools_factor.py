from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from doyoutrade.api.operations.factor import FactorAnalysisTool

from tests._tool_result_helpers import payload as _payload
def _write_factor_return_csvs(
    tmp_home: str,
    factor_df: pd.DataFrame,
    return_df: pd.DataFrame,
) -> tuple[str, str]:
    """Write factor and return CSVs to tmp_home, return their paths."""
    artifacts = Path(tmp_home) / ".doyoutrade" / "assistant" / "artifacts" / "factor_input"
    artifacts.mkdir(parents=True, exist_ok=True)
    factor_path = str(artifacts / "factor.csv")
    return_path = str(artifacts / "return.csv")
    factor_df.to_csv(factor_path, index=True, index_label="date")
    return_df.to_csv(return_path, index=True, index_label="date")
    return factor_path, return_path


class TestFactorAnalysisToolMetadata(unittest.TestCase):
    """Tests for tool name, category, and parameters."""

    def test_tool_name(self) -> None:
        tool = FactorAnalysisTool()
        self.assertEqual(tool.name, "factor_analysis")

    def test_tool_category(self) -> None:
        tool = FactorAnalysisTool()
        self.assertEqual(tool.category, "analysis")

    def test_tool_parameters(self) -> None:
        tool = FactorAnalysisTool()
        params = tool.parameters
        self.assertEqual(params["type"], "object")

        # Required parameters
        self.assertIn("factor_csv", params["properties"])
        self.assertEqual(params["properties"]["factor_csv"]["type"], "string")
        self.assertIn("factor_csv", params["required"])

        self.assertIn("return_csv", params["properties"])
        self.assertEqual(params["properties"]["return_csv"]["type"], "string")
        self.assertIn("return_csv", params["required"])

        # Optional parameters with defaults
        self.assertIn("output_dir", params["properties"])
        self.assertEqual(params["properties"]["output_dir"]["type"], "string")
        self.assertEqual(
            params["properties"]["output_dir"]["default"],
            "~/.doyoutrade/assistant/artifacts/factor_output/",
        )

        self.assertIn("n_groups", params["properties"])
        self.assertEqual(params["properties"]["n_groups"]["type"], "integer")
        self.assertEqual(params["properties"]["n_groups"]["default"], 5)


class TestFactorAnalysisToolICComputation(unittest.IsolatedAsyncioTestCase):
    """Test IC computation with synthetic perfectly-correlated data."""

    def _make_perfectly_correlated_data(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Create factor and return DataFrames that are perfectly rank-correlated."""
        np.random.seed(42)
        dates = pd.date_range("2024-01-01", periods=20, freq="B")
        codes = ["CODE_A", "CODE_B", "CODE_C", "CODE_D", "CODE_E"]

        # Factor values: monotonically increasing per code (different slopes)
        factor_data = {}
        return_data = {}
        for i, code in enumerate(codes):
            # Each code has a fixed factor rank across all dates
            # Code A = highest factor, E = lowest
            factor_values = np.array([len(codes) - i] * len(dates)) + np.random.randn(len(dates)) * 0.01
            # Return values: perfectly correlated with factor rank
            return_values = factor_values + np.random.randn(len(dates)) * 0.1
            factor_data[code] = factor_values
            return_data[code] = return_values

        factor_df = pd.DataFrame(factor_data, index=dates)
        return_df = pd.DataFrame(return_data, index=dates)
        return factor_df, return_df

    async def test_ic_computation_perfectly_correlated(self) -> None:
        """IC for perfectly rank-correlated data should be close to 1.0."""
        factor_df, return_df = self._make_perfectly_correlated_data()

        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                factor_path, return_path = _write_factor_return_csvs(tmp_home, factor_df, return_df)
                output_dir = str(Path(tmp_home) / ".doyoutrade" / "assistant" / "artifacts" / "factor_output")

                tool = FactorAnalysisTool()
                result = await tool.execute(
                    factor_csv=factor_path,
                    return_csv=return_path,
                    output_dir=output_dir,
                    n_groups=5,
                )
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertEqual(_payload(result)["status"], "ok", msg=f"Got error: {_payload(result).get('error')}")

            # IC should be positive and reasonably large
            ic_summary = _payload(result)["ic_summary"]
            self.assertGreater(ic_summary["ic_mean"], 0.0)
            self.assertLessEqual(ic_summary["ic_mean"], 1.0)
            self.assertGreater(ic_summary["ir"], 0.0)
            self.assertGreater(ic_summary["ic_positive_ratio"], 0.5)

    async def test_ic_bounds(self) -> None:
        """IC values must always be between -1 and 1."""
        dates = pd.date_range("2024-01-01", periods=10, freq="B")
        codes = ["C1", "C2", "C3", "C4", "C5"]

        np.random.seed(0)
        factor_df = pd.DataFrame(
            np.random.randn(len(dates), len(codes)),
            index=dates,
            columns=codes,
        )
        return_df = pd.DataFrame(
            np.random.randn(len(dates), len(codes)),
            index=dates,
            columns=codes,
        )

        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                factor_path, return_path = _write_factor_return_csvs(tmp_home, factor_df, return_df)
                output_dir = str(Path(tmp_home) / ".doyoutrade" / "assistant" / "artifacts" / "factor_output")

                tool = FactorAnalysisTool()
                result = await tool.execute(
                    factor_csv=factor_path,
                    return_csv=return_path,
                    output_dir=output_dir,
                )
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertEqual(_payload(result)["status"], "ok")
            ic_mean = _payload(result)["ic_summary"]["ic_mean"]
            self.assertGreaterEqual(ic_mean, -1.0)
            self.assertLessEqual(ic_mean, 1.0)


class TestFactorAnalysisToolMissingFile(unittest.IsolatedAsyncioTestCase):
    """Test error handling when input files are missing."""

    async def test_execute_missing_factor_csv(self) -> None:
        """Missing factor_csv returns error status."""
        tool = FactorAnalysisTool()
        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                result = await tool.execute(
                    factor_csv="/nonexistent/factor.csv",
                    return_csv="/nonexistent/return.csv",
                )
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertTrue(result.is_error)
            self.assertIn("factor", result.text.lower())

    async def test_execute_missing_return_csv(self) -> None:
        """Missing return_csv returns error status."""
        tool = FactorAnalysisTool()
        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                # Write a dummy factor CSV so factor_csv exists, but return_csv doesn't
                artifacts = Path(tmp_home) / ".doyoutrade" / "assistant" / "artifacts" / "factor_input"
                artifacts.mkdir(parents=True, exist_ok=True)
                factor_path = str(artifacts / "factor.csv")
                dates = pd.date_range("2024-01-01", periods=5, freq="B")
                codes = ["C1", "C2"]
                pd.DataFrame(np.ones((len(dates), len(codes))), index=dates, columns=codes).to_csv(factor_path)

                result = await tool.execute(
                    factor_csv=factor_path,
                    return_csv="/nonexistent/return.csv",
                )
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertTrue(result.is_error)
            self.assertIn("return", result.text.lower())


class TestFactorAnalysisToolQuantileGroups(unittest.IsolatedAsyncioTestCase):
    """Test quantile group computation."""

    async def test_quantile_groups_basic(self) -> None:
        """Quantile groups with 5 groups and perfectly correlated data."""
        np.random.seed(99)
        dates = pd.date_range("2024-01-01", periods=20, freq="B")
        codes = ["C" + str(i) for i in range(10)]

        # Factor: monotonically increasing by code index
        factor_data = {}
        return_data = {}
        for i, code in enumerate(codes):
            # Factor rank = i (higher i = higher factor)
            factor_data[code] = np.array([i] * len(dates)) + np.random.randn(len(dates)) * 0.01
            # Return perfectly correlated with factor
            return_data[code] = factor_data[code] * 1.5 + np.random.randn(len(dates)) * 0.05

        factor_df = pd.DataFrame(factor_data, index=dates)
        return_df = pd.DataFrame(return_data, index=dates)

        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                factor_path, return_path = _write_factor_return_csvs(tmp_home, factor_df, return_df)
                output_dir = str(Path(tmp_home) / ".doyoutrade" / "assistant" / "artifacts" / "factor_output")

                tool = FactorAnalysisTool()
                result = await tool.execute(
                    factor_csv=factor_path,
                    return_csv=return_path,
                    output_dir=output_dir,
                    n_groups=5,
                )
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertEqual(_payload(result)["status"], "ok", msg=f"Got error: {_payload(result).get('error')}")
            self.assertEqual(_payload(result)["n_groups"], 5)

            # Check output files exist
            for fname in ["ic_series.csv", "ic_summary.json", "group_equity.csv"]:
                fpath = Path(output_dir) / fname
                self.assertTrue(fpath.exists(), f"{fname} not found at {fpath}")

            # IC summary fields
            ic = _payload(result)["ic_summary"]
            self.assertIn("ic_mean", ic)
            self.assertIn("ic_std", ic)
            self.assertIn("ir", ic)
            self.assertIn("ic_positive_ratio", ic)

            # IR = ic_mean / ic_std
            self.assertAlmostEqual(ic["ir"], ic["ic_mean"] / ic["ic_std"], places=5)

            # Group equity should have n_groups columns (plus date index)
            group_equity = pd.read_csv(Path(output_dir) / "group_equity.csv", index_col=0)
            self.assertEqual(group_equity.shape[1], 5)

    async def test_quantile_groups_n_groups(self) -> None:
        """n_groups parameter is respected."""
        np.random.seed(7)
        dates = pd.date_range("2024-01-01", periods=15, freq="B")
        codes = ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10"]

        factor_df = pd.DataFrame(
            np.random.randn(len(dates), len(codes)),
            index=dates,
            columns=codes,
        )
        return_df = pd.DataFrame(
            np.random.randn(len(dates), len(codes)),
            index=dates,
            columns=codes,
        )

        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                factor_path, return_path = _write_factor_return_csvs(tmp_home, factor_df, return_df)
                output_dir = str(Path(tmp_home) / ".doyoutrade" / "assistant" / "artifacts" / "factor_output")

                tool = FactorAnalysisTool()
                result = await tool.execute(
                    factor_csv=factor_path,
                    return_csv=return_path,
                    output_dir=output_dir,
                    n_groups=3,
                )
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertEqual(_payload(result)["status"], "ok")
            self.assertEqual(_payload(result)["n_groups"], 3)

            group_equity = pd.read_csv(Path(output_dir) / "group_equity.csv", index_col=0)
            self.assertEqual(group_equity.shape[1], 3)


class TestFactorAnalysisToolOutputStructure(unittest.IsolatedAsyncioTestCase):
    """Test output file structure and JSON return format."""

    async def test_output_json_format(self) -> None:
        """Result JSON has required fields."""
        np.random.seed(123)
        dates = pd.date_range("2024-01-01", periods=10, freq="B")
        codes = ["X1", "X2", "X3"]

        factor_df = pd.DataFrame(np.random.randn(len(dates), len(codes)), index=dates, columns=codes)
        return_df = pd.DataFrame(np.random.randn(len(dates), len(codes)), index=dates, columns=codes)

        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                factor_path, return_path = _write_factor_return_csvs(tmp_home, factor_df, return_df)
                output_dir = str(Path(tmp_home) / ".doyoutrade" / "assistant" / "artifacts" / "factor_output")

                tool = FactorAnalysisTool()
                result = await tool.execute(
                    factor_csv=factor_path,
                    return_csv=return_path,
                    output_dir=output_dir,
                    n_groups=5,
                )
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertEqual(_payload(result)["status"], "ok")
            self.assertEqual(_payload(result)["factor_csv"], factor_path)
            self.assertEqual(_payload(result)["return_csv"], return_path)
            self.assertEqual(_payload(result)["n_groups"], 5)
            self.assertIn("ic_summary", _payload(result))
            self.assertIn("output_files", _payload(result))
            self.assertEqual(len(_payload(result)["output_files"]), 3)

    async def test_ic_series_has_date_index(self) -> None:
        """ic_series.csv has date index and IC values."""
        np.random.seed(456)
        dates = pd.date_range("2024-01-01", periods=15, freq="B")
        codes = ["A", "B", "C", "D", "E"]

        factor_df = pd.DataFrame(np.random.randn(len(dates), len(codes)), index=dates, columns=codes)
        return_df = pd.DataFrame(np.random.randn(len(dates), len(codes)), index=dates, columns=codes)

        with tempfile.TemporaryDirectory() as tmp_home:
            original_home = os.environ.get("HOME", "")
            os.environ["HOME"] = tmp_home
            try:
                factor_path, return_path = _write_factor_return_csvs(tmp_home, factor_df, return_df)
                output_dir = str(Path(tmp_home) / ".doyoutrade" / "assistant" / "artifacts" / "factor_output")

                tool = FactorAnalysisTool()
                result = await tool.execute(
                    factor_csv=factor_path,
                    return_csv=return_path,
                    output_dir=output_dir,
                )
            finally:
                if original_home:
                    os.environ["HOME"] = original_home
                else:
                    os.environ.pop("HOME", None)
            self.assertEqual(_payload(result)["status"], "ok")

            ic_series = pd.read_csv(Path(output_dir) / "ic_series.csv", index_col=0)
            self.assertGreater(len(ic_series), 0)
            # IC values should be in (-1, 1)
            ic_values = ic_series.iloc[:, 0]
            self.assertTrue(all((ic_values >= -1.0) & (ic_values <= 1.0)))


if __name__ == "__main__":
    unittest.main()
