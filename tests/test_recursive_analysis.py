"""Tests for the ``validate_recursive`` operation + ``sdk validate-recursive``.

The operation quantifies how much a strategy's indicators drift with
``startup_history``. These tests patch the real OHLCV fetch with a
synthetic trending series so no network / DB is needed, and assert that:

* a recursive indicator (EMA span 60) declared with too-small
  startup_history is flagged ``unstable`` with a recommended history;
* a stateless indicator (SMA window 3) is ``stable``;
* a strategy with no indicators reports the no-op path;
* compile errors and the kwargs contract surface as ``is_error`` results;
* the pure drift / ladder helpers behave at the boundaries.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pandas as pd

from doyoutrade.api.operations.market_data import MarketDataFetcher
from doyoutrade.api.operations.recursive_analysis import (
    ValidateRecursiveStabilityTool,
    _default_ladder,
    _drift_pct,
    _last_indicator_values,
)
from doyoutrade.cli._envelope import parse_tool_result


_EMA_STRATEGY = """\
from __future__ import annotations
from doyoutrade.strategy_sdk import Strategy, Signal, indicators


class Strategy(Strategy):
    name = "ema_probe"
    timeframe = "1d"
    startup_history = 10

    def populate_indicators(self, df, ctx):
        df["ema_fast"] = indicators.ema(df["close"], 60)
        return df

    def on_bar(self, df, ctx) -> Signal:
        return Signal.hold(tag="noop")
"""

_SMA_STRATEGY = """\
from __future__ import annotations
from doyoutrade.strategy_sdk import Strategy, Signal, indicators


class Strategy(Strategy):
    name = "sma_probe"
    timeframe = "1d"
    startup_history = 10

    def populate_indicators(self, df, ctx):
        df["sma_fast"] = indicators.sma(df["close"], 3)
        return df

    def on_bar(self, df, ctx) -> Signal:
        return Signal.hold(tag="noop")
"""

_NO_INDICATOR_STRATEGY = """\
from __future__ import annotations
from doyoutrade.strategy_sdk import Strategy, Signal


class Strategy(Strategy):
    name = "noop_probe"
    timeframe = "1d"
    startup_history = 10

    def on_bar(self, df, ctx) -> Signal:
        return Signal.hold(tag="noop")
"""

_MISSING_ON_BAR = """\
from __future__ import annotations
from doyoutrade.strategy_sdk import Strategy


class Strategy(Strategy):
    name = "broken"
    timeframe = "1d"
    startup_history = 10
"""


def _trending_frame(rows: int = 260, start: float = 100.0, step: float = 0.4) -> pd.DataFrame:
    """A strongly trending OHLCV frame so an EMA visibly depends on seed window."""

    base = datetime(2024, 1, 1)
    closes = [start + step * i for i in range(rows)]
    index = pd.DatetimeIndex([base + timedelta(days=i) for i in range(rows)], name="date")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1_000_000.0] * rows,
        },
        index=index,
    )


def _patched_fetch(frame: pd.DataFrame):
    async def _fake(self, code, *, start_dt, end_dt, period_label, interval, data_source):  # noqa: ANN001
        return frame

    return _fake


def _run_tool(source_code: str, frame: pd.DataFrame, **extra) -> tuple[dict, bool]:
    tool = ValidateRecursiveStabilityTool()
    with patch.object(MarketDataFetcher, "_fetch_ohlcv", new=_patched_fetch(frame)):
        result = asyncio.run(
            tool.execute(source_code=source_code, symbol="600519.SH", as_of="2024-09-01", **extra)
        )
    data, _summary, error_info = parse_tool_result(result.text, is_error=result.is_error)
    payload = data if isinstance(data, dict) else (error_info or {})
    return payload, result.is_error


class DriftHelperTests(unittest.TestCase):
    def test_reference_nan(self) -> None:
        drift, converged, note = _drift_pct(None, 1.0)
        self.assertIsNone(drift)
        self.assertFalse(converged)
        self.assertEqual(note, "reference_nan")

    def test_candidate_not_warmed(self) -> None:
        drift, _conv, note = _drift_pct(10.0, None)
        self.assertIsNone(drift)
        self.assertEqual(note, "not_warmed")

    def test_normal_drift(self) -> None:
        drift, _conv, note = _drift_pct(100.0, 105.0)
        self.assertAlmostEqual(drift, 5.0, places=3)
        self.assertIsNone(note)

    def test_near_zero_reference_equal(self) -> None:
        drift, _conv, note = _drift_pct(0.0, 0.0)
        self.assertEqual(drift, 0.0)
        self.assertEqual(note, "near_zero_reference")

    def test_default_ladder_includes_declared(self) -> None:
        ladder = _default_ladder(10, 260)
        self.assertIn(10, ladder)
        self.assertTrue(all(1 <= h < 260 for h in ladder))
        self.assertEqual(ladder, sorted(set(ladder)))

    def test_last_indicator_values_skips_ohlcv(self) -> None:
        df = pd.DataFrame(
            {"close": [1.0, 2.0], "open": [1.0, 2.0], "rsi": [50.0, 55.0], "flag": [True, False]}
        )
        vals = _last_indicator_values(df)
        self.assertIn("rsi", vals)
        self.assertNotIn("close", vals)  # base OHLCV excluded
        self.assertNotIn("flag", vals)  # bool excluded
        self.assertEqual(vals["rsi"], 55.0)


class ValidateRecursiveOperationTests(unittest.TestCase):
    def test_recursive_indicator_flagged_unstable(self) -> None:
        payload, is_error = _run_tool(_EMA_STRATEGY, _trending_frame())
        self.assertFalse(is_error, msg=f"payload: {payload}")
        self.assertEqual(payload.get("status"), "unstable", msg=f"payload: {payload}")
        self.assertIn("ema_fast", payload.get("unstable_columns", []))
        self.assertEqual(payload.get("declared_startup_history"), 10)
        rec = payload.get("recommended_startup_history")
        self.assertIsNotNone(rec)
        self.assertGreater(rec, 10)

    def test_stateless_indicator_stable(self) -> None:
        payload, is_error = _run_tool(_SMA_STRATEGY, _trending_frame())
        self.assertFalse(is_error, msg=f"payload: {payload}")
        self.assertEqual(payload.get("status"), "stable", msg=f"payload: {payload}")
        self.assertEqual(payload.get("unstable_columns"), [])
        self.assertIn("sma_fast", payload.get("indicators", {}))

    def test_no_indicators_reports_noop(self) -> None:
        payload, is_error = _run_tool(_NO_INDICATOR_STRATEGY, _trending_frame())
        self.assertFalse(is_error)
        self.assertEqual(payload.get("status"), "stable")
        self.assertEqual(payload.get("indicator_count"), 0)
        self.assertIn("note", payload)

    def test_threshold_widening_can_pass(self) -> None:
        # With a very large threshold even the EMA drift is tolerated.
        payload, is_error = _run_tool(_EMA_STRATEGY, _trending_frame(), threshold_pct=1000.0)
        self.assertFalse(is_error)
        self.assertEqual(payload.get("status"), "stable", msg=f"payload: {payload}")

    def test_compile_error_is_error(self) -> None:
        payload, is_error = _run_tool(_MISSING_ON_BAR, _trending_frame())
        self.assertTrue(is_error)
        self.assertIn("error_code", payload)

    def test_insufficient_history_is_error(self) -> None:
        # Only 5 bars but startup_history=10 → cannot even warm up.
        payload, is_error = _run_tool(_EMA_STRATEGY, _trending_frame(rows=5))
        self.assertTrue(is_error)
        self.assertEqual(payload.get("error_code"), "insufficient_history")

    def test_unknown_kwarg_rejected(self) -> None:
        tool = ValidateRecursiveStabilityTool()
        with patch.object(MarketDataFetcher, "_fetch_ohlcv", new=_patched_fetch(_trending_frame())):
            result = asyncio.run(
                tool.execute(source_code=_EMA_STRATEGY, symbol="600519.SH", bogus_kwarg=1)
            )
        self.assertTrue(result.is_error)
        self.assertIn("bogus_kwarg", result.text)

    def test_invalid_symbol_is_error(self) -> None:
        payload, is_error = _run_tool(_EMA_STRATEGY, _trending_frame(), )
        # sanity: valid path already covered; now invalid symbol
        tool = ValidateRecursiveStabilityTool()
        with patch.object(MarketDataFetcher, "_fetch_ohlcv", new=_patched_fetch(_trending_frame())):
            result = asyncio.run(
                tool.execute(source_code=_EMA_STRATEGY, symbol="not a symbol!")
            )
        data, _s, error_info = parse_tool_result(result.text, is_error=result.is_error)
        self.assertTrue(result.is_error)
        self.assertEqual((error_info or {}).get("error_code"), "invalid_symbol")


class ValidateRecursiveCliTests(unittest.TestCase):
    """CLI-layer tests: arg parsing, payload shape, and gate exit codes.

    ``invoke_api`` is patched so no server is needed; we assert the command
    builds the right payload and maps ``status='unstable'`` to exit 1.
    """

    def _invoke(self, source_code: str, fake_envelope: dict, extra_args=None):
        import tempfile as _tf
        from click.testing import CliRunner

        from doyoutrade.cli.commands.sdk import sdk as sdk_group

        captured: dict = {}

        async def _fake_invoke_api(method, path, *, json=None, meta=None, **kwargs):
            captured["method"] = method
            captured["path"] = path
            captured["json"] = json
            return fake_envelope, 0

        runner = CliRunner()
        with _tf.NamedTemporaryFile(suffix=".py", mode="w", encoding="utf-8", delete=False) as tmp:
            tmp.write(source_code)
            tmp_path = tmp.name
        try:
            with patch("doyoutrade.cli.commands.sdk.invoke_api", new=_fake_invoke_api):
                args = ["validate-recursive", tmp_path, "--symbol", "600519.SH"] + (extra_args or [])
                result = runner.invoke(sdk_group, args, catch_exceptions=False, obj={"fmt": "json"})
            return result, captured
        finally:
            from pathlib import Path as _P

            _P(tmp_path).unlink(missing_ok=True)

    def test_stable_exits_0(self) -> None:
        env = {"ok": True, "data": {"status": "stable", "unstable_columns": []}}
        result, captured = self._invoke(_SMA_STRATEGY, env)
        self.assertEqual(result.exit_code, 0, msg=f"out: {result.output}")
        self.assertEqual(captured["path"], "/sdk/validate-recursive")
        self.assertEqual(captured["json"]["symbol"], "600519.SH")
        self.assertIn("source_code", captured["json"])

    def test_unstable_exits_1_gate(self) -> None:
        env = {"ok": True, "data": {"status": "unstable", "unstable_columns": ["ema_fast"]}}
        result, _captured = self._invoke(_EMA_STRATEGY, env)
        self.assertEqual(result.exit_code, 1, msg=f"out: {result.output}")

    def test_ladder_parsed_to_ints(self) -> None:
        env = {"ok": True, "data": {"status": "stable"}}
        _result, captured = self._invoke(_SMA_STRATEGY, env, extra_args=["--ladder", "10,20,40"])
        self.assertEqual(captured["json"]["ladder"], [10, 20, 40])

    def test_bad_ladder_usage_error(self) -> None:
        env = {"ok": True, "data": {"status": "stable"}}
        result, _captured = self._invoke(_SMA_STRATEGY, env, extra_args=["--ladder", "ten,twenty"])
        self.assertEqual(result.exit_code, 2)


if __name__ == "__main__":
    unittest.main()
