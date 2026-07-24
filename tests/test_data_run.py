"""Tests for the unified ``doyoutrade-cli data run`` command.

The command runs a fetch + indicator + (optional) sandboxed script
pipeline across one or many symbols. These tests pin the contract:

* multi-symbol input via positional ``code``, ``--symbols``, or
  ``--universe-file`` (mutually exclusive),
* envelope shape (``symbols[]`` array + manifest, ``script_source`` is
  metadata only — never raw code),
* AST sandbox rejects disallowed imports / silent except / lookahead,
* warmup auto-sizes from selected built-ins plus a script
  ``REQUIRED_HISTORY`` literal, and refuses to default to 0 when a pure
  script gives no hint,
* runtime failures are sub-typed (``script_name_error`` etc.) and
  scalar broadcasts raise ``script_output_scalar_broadcast``.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd
from click.testing import CliRunner

from tests._tool_result_helpers import payload as _payload
from doyoutrade.cli._envelope import EXIT_OK, EXIT_VALIDATION
from doyoutrade.cli._invoke import invoke_tool
from doyoutrade.cli.commands.data import data as data_group
from doyoutrade.api.operations.data_run import DataRunTool
from doyoutrade.api.operations.market_data import MarketDataFetcher
from doyoutrade.core.models import Bar


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_ohlcv(start: date, end: date) -> pd.DataFrame:
    dates = pd.date_range(start.isoformat(), end.isoformat(), freq="B")
    n = len(dates)
    close = pd.Series([10.0 + i for i in range(n)], index=dates)
    return pd.DataFrame(
        {
            "open": close - 0.5,
            "high": close + 0.5,
            "low": close - 1.0,
            "close": close,
            "volume": [1000.0 + i for i in range(n)],
        },
        index=pd.Index(dates, name="date"),
    )


def _first_symbol(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return ``symbols[0]`` from a tool payload, asserting it's present."""

    symbols = envelope.get("symbols")
    assert isinstance(symbols, list) and symbols, f"missing symbols[] in {envelope}"
    return symbols[0]


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
# Single-symbol pipeline
# ---------------------------------------------------------------------------


class DataRunToolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_fetches_warmup_window_but_returns_requested_ohlcv_window(self) -> None:
        captured: dict[str, Any] = {}

        async def _fetch(
            self,
            code: str,
            *,
            start_dt: date,
            end_dt: date,
            period_label: str,
            interval: str,
            data_source: str,
        ) -> pd.DataFrame:
            captured.update(start_dt=start_dt, end_dt=end_dt, period_label=period_label)
            return _fake_ohlcv(start_dt, end_dt)

        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                code="600519.SH",
                start_date="2026-04-13",
                end_date="2026-04-20",
                indicators=["rsi"],
                warmup_bars=5,
                tail=2,
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["requested_start"], "2026-04-13")
        self.assertEqual(data["requested_end"], "2026-04-20")
        self.assertLess(captured["start_dt"], date(2026, 4, 13))
        self.assertEqual(captured["end_dt"], date(2026, 4, 20))
        self.assertEqual(data["warmup_bars_default"], 5)
        self.assertTrue(data["warmup_bars_explicit"])
        self.assertEqual(data["symbols_total"], 1)
        self.assertEqual(data["symbols_succeeded"], 1)
        self.assertEqual(data["symbols_failed"], 0)
        symbol = _first_symbol(data)
        self.assertEqual(symbol["code"], "600519.SH")
        self.assertEqual(symbol["ohlcv_rows"], 6)
        self.assertIn("rsi", symbol["indicator_columns"])
        self.assertEqual(len(symbol["latest"]["rsi"]), 2)
        self.assertTrue(Path(symbol["ohlcv_path"]).exists())
        self.assertTrue(Path(symbol["indicator_path"]).exists())
        # Manifest written and lists the same symbols payload.
        self.assertTrue(Path(data["manifest_path"]).exists())
        manifest = json.loads(Path(data["manifest_path"]).read_text())
        self.assertEqual(manifest["symbols_total"], 1)
        self.assertEqual(manifest["symbols"][0]["code"], "600519.SH")

    async def test_executes_inline_custom_script_with_required_history(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        script = textwrap.dedent(
            """
            REQUIRED_HISTORY = 3

            def compute(df, target_df, params):
                return {
                    "gap_from_fetch_start": df["close"] - df["close"].iloc[0],
                    "target_marker": target_df["close"] * params["scale"],
                }
            """
        )
        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                code="000001.SZ",
                start_date="2026-04-13",
                end_date="2026-04-20",
                script=script,
                script_params={"scale": 2},
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        symbol = _first_symbol(data)
        self.assertIn("custom.gap_from_fetch_start", symbol["indicator_columns"])
        self.assertIn("custom.target_marker", symbol["indicator_columns"])
        # Auto-sized warmup picked up the script REQUIRED_HISTORY literal.
        self.assertEqual(data["warmup_bars_default"], 3)
        self.assertFalse(data["warmup_bars_explicit"])
        # Script source metadata: never raw code in envelope.
        meta = data["script_source"]
        self.assertEqual(meta["kind"], "inline")
        self.assertNotIn("code", meta)
        self.assertEqual(meta["required_history"], 3)
        self.assertIn("persisted_path", meta)
        self.assertTrue(Path(meta["persisted_path"]).exists())

    async def test_executes_custom_script_file(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        script_path = Path(self._tmp.name) / "factor.py"
        script_path.write_text(
            "result = {'close_x3': target_df['close'] * 3}\n",
            encoding="utf-8",
        )
        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                code="000001.SZ",
                start_date="2026-04-13",
                end_date="2026-04-20",
                script_file=str(script_path),
                warmup_bars=0,
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        symbol = _first_symbol(data)
        self.assertIn("custom.close_x3", symbol["indicator_columns"])
        self.assertEqual(data["script_source"]["kind"], "file")
        self.assertEqual(data["script_source"]["source_path"], str(script_path))

    async def test_rejects_inline_script_and_script_file_together(self) -> None:
        result = await DataRunTool().execute(
            code="000001.SZ",
            start_date="2026-04-13",
            end_date="2026-04-20",
            script="result = {}",
            script_file="/tmp/factor.py",
        )

        self.assertTrue(result.is_error)
        self.assertIn("[error:conflicting_script_args]", result.text)

    async def test_rejects_scalar_broadcast_output(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                code="000001.SZ",
                start_date="2026-04-13",
                end_date="2026-04-20",
                script="REQUIRED_HISTORY = 0\nresult = {'my_factor': 42}",
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        # Partial outcome: per-symbol failure surfaced with sub-typed code.
        self.assertEqual(data["status"], "failed")
        symbol = _first_symbol(data)
        self.assertEqual(symbol["status"], "failed")
        self.assertEqual(symbol["error_code"], "script_output_scalar_broadcast")

    async def test_rejects_top_level_scalar_return(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                code="000001.SZ",
                start_date="2026-04-13",
                end_date="2026-04-20",
                script="REQUIRED_HISTORY = 0\nresult = 42",
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        symbol = _first_symbol(data)
        self.assertEqual(symbol["status"], "failed")
        self.assertEqual(symbol["error_code"], "script_output_invalid")


# ---------------------------------------------------------------------------
# AST sandbox + signature checks (no provider involved)
# ---------------------------------------------------------------------------


class DataRunScriptSandboxTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_disallowed_import_rejected_at_validate_time(self) -> None:
        result = await DataRunTool().execute(
            code="000001.SZ",
            start_date="2026-04-13",
            end_date="2026-04-20",
            script="import os\nresult = {'x': target_df['close']}",
        )

        self.assertTrue(result.is_error)
        self.assertIn("[error:script_disallowed_import]", result.text)

    async def test_silent_except_rejected_at_validate_time(self) -> None:
        result = await DataRunTool().execute(
            code="000001.SZ",
            start_date="2026-04-13",
            end_date="2026-04-20",
            script="try:\n  x = 1\nexcept Exception:\n  pass\nresult = {'x': target_df['close']}",
        )

        self.assertTrue(result.is_error)
        self.assertIn("[error:script_silent_exception_swallow]", result.text)

    async def test_shift_negative_rejected(self) -> None:
        result = await DataRunTool().execute(
            code="000001.SZ",
            start_date="2026-04-13",
            end_date="2026-04-20",
            script="REQUIRED_HISTORY = 0\nresult = {'x': target_df['close'].shift(-1)}",
        )

        self.assertTrue(result.is_error)
        # df.shift(-N) genuinely shifts in data from the future.
        self.assertIn("[error:script_lookahead_access]", result.text)

    async def test_signature_check_rejects_wrong_arity(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                code="000001.SZ",
                start_date="2026-04-13",
                end_date="2026-04-20",
                script="REQUIRED_HISTORY = 0\ndef compute(df, target_df):\n  return target_df['close']",
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        symbol = _first_symbol(data)
        self.assertEqual(symbol["status"], "failed")
        self.assertEqual(symbol["error_code"], "script_compute_signature_invalid")

    async def test_name_error_subtyped(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                code="000001.SZ",
                start_date="2026-04-13",
                end_date="2026-04-20",
                script=(
                    "REQUIRED_HISTORY = 0\n"
                    "def compute(df, target_df, params):\n"
                    "  return {'x': unknown_symbol}\n"
                ),
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        symbol = _first_symbol(data)
        self.assertEqual(symbol["status"], "failed")
        self.assertEqual(symbol["error_code"], "script_name_error")
        self.assertEqual(symbol["error_type"], "NameError")

    async def test_pure_script_without_warmup_hint_is_rejected(self) -> None:
        result = await DataRunTool().execute(
            code="000001.SZ",
            start_date="2026-04-13",
            end_date="2026-04-20",
            script="def compute(df, target_df, params):\n  return {'x': target_df['close']}",
        )

        self.assertTrue(result.is_error)
        self.assertIn("[error:script_warmup_unspecified]", result.text)


# ---------------------------------------------------------------------------
# Multi-symbol
# ---------------------------------------------------------------------------


class DataRunMultiSymbolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_symbols_csv_runs_per_symbol(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                symbols="600519.SH,000001.SZ",
                start_date="2026-04-13",
                end_date="2026-04-20",
                indicators=["rsi"],
                warmup_bars=5,
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["symbols_total"], 2)
        self.assertEqual(data["symbols_succeeded"], 2)
        codes = [row["code"] for row in data["symbols"]]
        self.assertEqual(codes, ["600519.SH", "000001.SZ"])
        for row in data["symbols"]:
            self.assertEqual(row["status"], "ok")
            self.assertTrue(Path(row["ohlcv_path"]).exists())

    async def test_universe_file_skips_blanks_and_comments(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        universe = Path(self._tmp.name) / "u.txt"
        universe.write_text(
            "# header comment\n600519.SH\n\n# blank above\n000001.SZ\n",
            encoding="utf-8",
        )

        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                universe_file=str(universe),
                start_date="2026-04-13",
                end_date="2026-04-20",
                indicators=["rsi"],
                warmup_bars=5,
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["symbols_total"], 2)

    async def test_universe_file_accepts_csv_with_header(self) -> None:
        # Agents reflexively dump a universe via pandas ``to_csv`` — a
        # ``symbol,name`` header plus per-row ``CODE.EXCHANGE,中文名`` columns
        # (tmp/messages.json turn 10, where the header literal surfaced as
        # ``invalid_symbol``). The loader must skip the header and take the
        # first CSV column as the symbol.
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        universe = Path(self._tmp.name) / "u.csv"
        universe.write_text(
            "symbol,name\n600519.SH,贵州茅台\n000001.SZ,平安银行\n",
            encoding="utf-8",
        )

        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                universe_file=str(universe),
                start_date="2026-04-13",
                end_date="2026-04-20",
                indicators=["rsi"],
                warmup_bars=5,
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["symbols_total"], 2)
        self.assertEqual(
            sorted(row["code"] for row in data["symbols"]),
            ["000001.SZ", "600519.SH"],
        )

    async def test_conflicting_symbol_inputs_rejected(self) -> None:
        result = await DataRunTool().execute(
            code="600519.SH",
            symbols="000001.SZ",
            start_date="2026-04-13",
            end_date="2026-04-20",
        )

        self.assertTrue(result.is_error)
        self.assertIn("[error:conflicting_symbol_args]", result.text)

    async def test_partial_failure_keeps_envelope_non_error(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            if code == "BROKEN.SH":
                raise RuntimeError("provider down")
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                symbols="600519.SH,BROKEN.SH",
                start_date="2026-04-13",
                end_date="2026-04-20",
                indicators=["rsi"],
                warmup_bars=5,
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["symbols_succeeded"], 1)
        self.assertEqual(data["symbols_failed"], 1)
        ok = next(row for row in data["symbols"] if row["status"] == "ok")
        fail = next(row for row in data["symbols"] if row["status"] == "failed")
        self.assertEqual(ok["code"], "600519.SH")
        self.assertEqual(fail["code"], "BROKEN.SH")
        self.assertEqual(fail["error_code"], "data_fetch_failed")

    async def test_interval_not_supported_for_symbol_gets_dedicated_error_code(self) -> None:
        # An index + minute-interval rejection (raised by _fetch_ohlcv's
        # supports_interval_for_symbol pre-flight) is a known, named
        # constraint — it must not be folded into the generic
        # data_fetch_failed bucket alongside real upstream failures.
        from doyoutrade.api.operations.market_data import _IntervalNotSupportedForSymbol

        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            raise _IntervalNotSupportedForSymbol(
                f"data_source='baostock' does not support interval='60m' for {code!r}"
            )

        with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
            result = await DataRunTool().execute(
                code="000001.SH",
                start_date="2026-04-13",
                end_date="2026-04-20",
                interval="60m",
                data_source="baostock",
            )

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["symbols"][0]["error_code"], "interval_not_supported_for_instrument_type")


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


class DataRunCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self._api_patch = patch(
            "doyoutrade.cli.commands.data.invoke_api",
            new=self._fake_invoke_api,
        )
        self._api_patch.start()

    def tearDown(self) -> None:
        self._api_patch.stop()

    @staticmethod
    async def _fake_invoke_api(method: str, path: str, *, json=None, meta=None, **kwargs):
        if method != "POST" or path != "/data/run":
            raise AssertionError(f"unexpected API call: {method} {path}")
        return await invoke_tool(DataRunTool(), json or {}, meta=meta)

    def _invoke(self, args: list[str]) -> Any:
        return self.runner.invoke(
            data_group,
            args,
            obj={"fmt": "json"},
            catch_exceptions=False,
        )

    def test_cli_forwards_data_run_options(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HOME": home}, clear=False):
                with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
                    result = self._invoke(
                        [
                            "run",
                            "600519.SH",
                            "--start",
                            "2026-04-13",
                            "--end",
                            "2026-04-20",
                            "--indicators",
                            "rsi,macd",
                            "--indicator-params",
                            '{"rsi":{"period":7}}',
                            "--warmup-bars",
                            "10",
                            "--tail",
                            "3",
                        ]
                    )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"])
        data = envelope["data"]
        self.assertEqual(data["warmup_bars_default"], 10)
        symbol = data["symbols"][0]
        self.assertIn("macd.hist", symbol["indicator_columns"])

    def test_cli_rejects_bad_indicator_params_json(self) -> None:
        result = self._invoke(
            [
                "run",
                "600519.SH",
                "--start",
                "2026-04-13",
                "--end",
                "2026-04-20",
                "--indicator-params",
                "{bad",
            ]
        )

        self.assertEqual(result.exit_code, EXIT_VALIDATION, msg=result.output)
        envelope = json.loads(result.output)
        self.assertEqual(envelope["error"]["error_code"], "invalid_indicator_params_json")

    def test_cli_accepts_script_file(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        with tempfile.TemporaryDirectory() as home:
            script_path = Path(home) / "factor.py"
            script_path.write_text(
                textwrap.dedent(
                    """
                    REQUIRED_HISTORY = 0

                    def compute(df, target_df, params):
                        return {"close_plus_one": target_df["close"] + 1}
                    """
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"HOME": home}, clear=False):
                with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
                    result = self._invoke(
                        [
                            "run",
                            "600519.SH",
                            "--start",
                            "2026-04-13",
                            "--end",
                            "2026-04-20",
                            "--script-file",
                            str(script_path),
                        ]
                    )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        symbol = envelope["data"]["symbols"][0]
        self.assertIn("custom.close_plus_one", symbol["indicator_columns"])

    def test_cli_accepts_symbols_flag(self) -> None:
        async def _fetch(self, code: str, **kwargs: Any) -> pd.DataFrame:
            return _fake_ohlcv(kwargs["start_dt"], kwargs["end_dt"])

        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HOME": home}, clear=False):
                with patch("doyoutrade.api.operations.market_data.MarketDataFetcher._fetch_ohlcv", new=_fetch):
                    result = self._invoke(
                        [
                            "run",
                            "--symbols",
                            "600519.SH,000001.SZ",
                            "--start",
                            "2026-04-13",
                            "--end",
                            "2026-04-20",
                            "--indicators",
                            "rsi",
                            "--warmup-bars",
                            "5",
                        ]
                    )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertEqual(envelope["data"]["symbols_total"], 2)


class BarsToDataFrameAmountTests(unittest.TestCase):
    """``_bars_to_dataframe`` carries 成交额 (amount) through when present and
    keeps the legacy OHLCV column set when no bar reports it."""

    @staticmethod
    def _bar(ts: str, *, amount: float | None) -> Bar:
        return Bar(
            symbol="600519.SH",
            timestamp=ts,
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            volume=1000.0,
            amount=amount,
        )

    def test_amount_column_present_when_any_bar_has_turnover(self) -> None:
        bars = [
            self._bar("2026-06-18", amount=1.23e8),
            self._bar("2026-06-19", amount=2.34e8),
        ]
        df = MarketDataFetcher()._bars_to_dataframe(bars)
        self.assertIn("amount", df.columns)
        self.assertEqual(list(df["amount"]), [1.23e8, 2.34e8])

    def test_amount_column_absent_when_no_bar_has_turnover(self) -> None:
        bars = [self._bar("2026-06-18", amount=None), self._bar("2026-06-19", amount=None)]
        df = MarketDataFetcher()._bars_to_dataframe(bars)
        self.assertNotIn("amount", df.columns)
        self.assertEqual(
            sorted(df.columns), sorted(["open", "high", "low", "close", "volume"])
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
