"""Verify akshare tqdm progress bars are suppressed during ``stock lookup``.

The CLI captures stdout/stderr of tool calls into the model's tool-result
payload. akshare's request layer writes a tqdm progress bar (~14 lines per
call) which (a) eats model tokens and (b) risks corrupting the JSON envelope.

These tests stub ``ak.stock_info_a_code_name`` / ``ak.stock_info_bj_name_code``
so they print tqdm-shaped lines + a fake non-tqdm warning, then assert:

* ``_sync_fetch_spot_rows`` swallows the tqdm noise (no ``it/s`` / no
  percentage-bar leakage onto the real stdout/stderr).
* Genuine (non-tqdm) stderr is forwarded to ``logger.warning`` so it stays
  visible per the CLAUDE.md "no silent swallow" rule.
* The async cache layer still works (akshare called once across two lookups).
* ``LookupStockSymbolTool`` invoked end-to-end leaves the captured I/O
  clean for the CLI envelope writer.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import pandas as pd

from doyoutrade.data.instrument_universe.akshare_a import (
    _silence_akshare_progress,
    _sync_fetch_spot_rows,
    clear_akshare_a_spot_cache,
    search_akshare_a,
)


_FAKE_A_DF = pd.DataFrame(
    [
        {"code": "600519", "name": "贵州茅台"},
        {"code": "000001", "name": "平安银行"},
    ]
)
_FAKE_BJ_DF = pd.DataFrame(
    [
        {"证券代码": "430047", "证券简称": "诺思兰德"},
    ]
)


# Shape mimics tqdm's progress-bar frames (carriage-return overwrites).
_TQDM_NOISE = (
    "  0%|          | 0/1000 [00:00<?, ?it/s]\r"
    " 25%|██▌       | 250/1000 [00:00<00:03, 250.00it/s]\r"
    " 75%|███████▌  | 750/1000 [00:01<00:00, 600.00it/s]\r"
    "100%|██████████| 1000/1000 [00:01<00:00, 800.00it/s]\n"
)
_REAL_WARNING = "UserWarning: akshare upstream API may be flaky today\n"


def _make_a_stub(emit_stderr: str = "", emit_stdout: str = ""):
    def _stub(*_args, **_kwargs):
        if emit_stdout:
            sys.stdout.write(emit_stdout)
        if emit_stderr:
            sys.stderr.write(emit_stderr)
        return _FAKE_A_DF.copy()

    return _stub


def _make_bj_stub(emit_stderr: str = ""):
    def _stub(*_args, **_kwargs):
        if emit_stderr:
            sys.stderr.write(emit_stderr)
        return _FAKE_BJ_DF.copy()

    return _stub


class SilenceAkshareProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_akshare_a_spot_cache()

    def test_restores_stdout_stderr_and_env(self) -> None:
        sentinel_stdout = sys.stdout
        sentinel_stderr = sys.stderr
        prev_env = os.environ.get("TQDM_DISABLE")
        try:
            with _silence_akshare_progress():
                self.assertIsNot(sys.stdout, sentinel_stdout)
                self.assertIsNot(sys.stderr, sentinel_stderr)
                self.assertEqual(os.environ.get("TQDM_DISABLE"), "1")
            self.assertIs(sys.stdout, sentinel_stdout)
            self.assertIs(sys.stderr, sentinel_stderr)
        finally:
            # Ensure no leakage into other tests regardless of pass/fail.
            if prev_env is None:
                os.environ.pop("TQDM_DISABLE", None)
            else:
                os.environ["TQDM_DISABLE"] = prev_env

        if prev_env is None:
            self.assertNotIn("TQDM_DISABLE", os.environ)
        else:
            self.assertEqual(os.environ.get("TQDM_DISABLE"), prev_env)

    def test_restores_env_even_on_exception(self) -> None:
        prev_env = os.environ.get("TQDM_DISABLE")
        with self.assertRaises(RuntimeError):
            with _silence_akshare_progress():
                raise RuntimeError("boom")
        # Same env state we started in.
        if prev_env is None:
            self.assertNotIn("TQDM_DISABLE", os.environ)
        else:
            self.assertEqual(os.environ.get("TQDM_DISABLE"), prev_env)

    def test_tqdm_lines_do_not_reach_outer_stderr(self) -> None:
        outer_stdout = io.StringIO()
        outer_stderr = io.StringIO()
        with redirect_stdout(outer_stdout), redirect_stderr(outer_stderr):
            with _silence_akshare_progress():
                sys.stderr.write(_TQDM_NOISE)
                sys.stdout.write("100%|##########| 200/200 [00:00<00:00, 1000.00it/s]\n")

        leaked_err = outer_stderr.getvalue()
        leaked_out = outer_stdout.getvalue()
        self.assertNotIn("it/s", leaked_err)
        self.assertNotIn("it/s", leaked_out)
        self.assertNotIn("█", leaked_err)
        self.assertNotIn("█", leaked_out)

    def test_non_tqdm_stderr_forwarded_to_logger(self) -> None:
        with self.assertLogs(
            "doyoutrade.data.instrument_universe.akshare_a", level="WARNING"
        ) as cap:
            with _silence_akshare_progress():
                sys.stderr.write(_TQDM_NOISE)
                sys.stderr.write(_REAL_WARNING)
        merged = "\n".join(cap.output)
        self.assertIn("akshare upstream API may be flaky today", merged)
        # tqdm frames must not appear in the logger output.
        self.assertNotRegex(merged, r"\d+%\|.*it/s")


class SyncFetchSpotRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_akshare_a_spot_cache()

    def test_fetch_does_not_leak_tqdm_to_real_stdio(self) -> None:
        outer_stdout = io.StringIO()
        outer_stderr = io.StringIO()
        a_stub = _make_a_stub(emit_stderr=_TQDM_NOISE)
        bj_stub = _make_bj_stub(emit_stderr=_TQDM_NOISE)

        with patch(
            "doyoutrade.data.instrument_universe.akshare_a.ak.stock_info_a_code_name",
            side_effect=a_stub,
        ), patch(
            "doyoutrade.data.instrument_universe.akshare_a.ak.stock_info_bj_name_code",
            side_effect=bj_stub,
        ):
            with redirect_stdout(outer_stdout), redirect_stderr(outer_stderr):
                rows = _sync_fetch_spot_rows()

        # A-share + BJ rows were captured.
        symbols = {r["symbol"] for r in rows}
        self.assertIn("600519.SH", symbols)
        self.assertIn("000001.SZ", symbols)
        self.assertIn("430047.BJ", symbols)

        # No tqdm pollution on the real streams.
        self.assertNotIn("it/s", outer_stdout.getvalue())
        self.assertNotIn("it/s", outer_stderr.getvalue())
        self.assertFalse(
            re.search(r"\d+%\|", outer_stdout.getvalue()),
            outer_stdout.getvalue(),
        )
        self.assertFalse(
            re.search(r"\d+%\|", outer_stderr.getvalue()),
            outer_stderr.getvalue(),
        )

    def test_bj_failure_is_logged_not_silently_swallowed(self) -> None:
        a_stub = _make_a_stub()

        def _bj_raise(*_args, **_kwargs):
            raise RuntimeError("BJ endpoint down")

        with patch(
            "doyoutrade.data.instrument_universe.akshare_a.ak.stock_info_a_code_name",
            side_effect=a_stub,
        ), patch(
            "doyoutrade.data.instrument_universe.akshare_a.ak.stock_info_bj_name_code",
            side_effect=_bj_raise,
        ):
            with self.assertLogs(
                "doyoutrade.data.instrument_universe.akshare_a", level="WARNING"
            ) as cap:
                rows = _sync_fetch_spot_rows()

        # A-share rows still made it through.
        self.assertTrue(any(r["symbol"] == "600519.SH" for r in rows))
        # BJ failure surfaced via the logger.
        merged = "\n".join(cap.output)
        self.assertIn("stock_info_bj_name_code", merged)
        self.assertIn("RuntimeError", merged)
        self.assertIn("BJ endpoint down", merged)

    def test_cache_dedupes_across_two_searches(self) -> None:
        a_stub = _make_a_stub(emit_stderr=_TQDM_NOISE)
        bj_stub = _make_bj_stub()

        with patch(
            "doyoutrade.data.instrument_universe.akshare_a.ak.stock_info_a_code_name",
            side_effect=a_stub,
        ) as a_mock, patch(
            "doyoutrade.data.instrument_universe.akshare_a.ak.stock_info_bj_name_code",
            side_effect=bj_stub,
        ) as bj_mock:
            outer_stdout = io.StringIO()
            outer_stderr = io.StringIO()
            with redirect_stdout(outer_stdout), redirect_stderr(outer_stderr):
                # Suppress propagation of the captured-stderr warning forwarder
                # so it doesn't spam the test runner's own stderr.
                lg = logging.getLogger(
                    "doyoutrade.data.instrument_universe.akshare_a"
                )
                prev_level = lg.level
                lg.setLevel(logging.ERROR)
                try:
                    first = asyncio.run(search_akshare_a(q="贵州", limit=5))
                    second = asyncio.run(search_akshare_a(q="平安", limit=5))
                finally:
                    lg.setLevel(prev_level)

        self.assertEqual(a_mock.call_count, 1)
        self.assertEqual(bj_mock.call_count, 1)
        self.assertTrue(any(r["symbol"] == "600519.SH" for r in first))
        self.assertTrue(any(r["symbol"] == "000001.SZ" for r in second))
        self.assertNotIn("it/s", outer_stdout.getvalue())
        self.assertNotIn("it/s", outer_stderr.getvalue())


class LookupStockSymbolToolStdioTests(unittest.TestCase):
    """End-to-end check: invoking the tool leaves no tqdm noise on stdio."""

    def setUp(self) -> None:
        clear_akshare_a_spot_cache()

    def test_execute_does_not_leak_tqdm(self) -> None:
        from doyoutrade.api.operations.stock_lookup import LookupStockSymbolTool

        a_stub = _make_a_stub(emit_stderr=_TQDM_NOISE)
        bj_stub = _make_bj_stub(emit_stderr=_TQDM_NOISE)

        outer_stdout = io.StringIO()
        outer_stderr = io.StringIO()
        with patch(
            "doyoutrade.data.instrument_universe.akshare_a.ak.stock_info_a_code_name",
            side_effect=a_stub,
        ), patch(
            "doyoutrade.data.instrument_universe.akshare_a.ak.stock_info_bj_name_code",
            side_effect=bj_stub,
        ):
            with redirect_stdout(outer_stdout), redirect_stderr(outer_stderr):
                tool = LookupStockSymbolTool()
                result = asyncio.run(
                    tool.execute(q="贵州茅台", limit=5, source="akshare_a")
                )

        self.assertFalse(result.is_error, result.text)
        self.assertIn("600519.SH", result.text)
        self.assertNotIn("it/s", outer_stdout.getvalue())
        self.assertNotIn("it/s", outer_stderr.getvalue())
        self.assertNotIn("█", outer_stdout.getvalue())
        self.assertNotIn("█", outer_stderr.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
