"""Unit tests for the ``data_events`` axis (suspension / 停牌雷).

Coverage:

* ``DataEventsTool`` happy path (CSV written, per-symbol counts) + rejections.
* ``AkshareEventProvider`` parses the 停复牌 snapshot, normalizes bare codes,
  filters to the universe; a missing code column raises (loud, not empty);
  persistent upstream failure re-raises.
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

from doyoutrade.api.operations.data_events import DataEventsTool
from doyoutrade.core.models import EventItem
from doyoutrade.data.protocols import ProviderCapabilities, PROVIDER_NAME_AKSHARE


def _caps():
    return ProviderCapabilities(name=PROVIDER_NAME_AKSHARE, supported_intervals=frozenset())


class _FakeEv:
    capabilities = _caps()

    def __init__(self, suspended, *, raise_=False):
        self._s = set(suspended)
        self._raise = raise_

    async def get_events_batch(self, symbols, *, asof=None):
        if self._raise:
            raise RuntimeError("boom")
        return {
            s: [EventItem(code=s, event_type="suspension", event_date=asof or "",
                          detail="重大事项", provider="akshare")]
            for s in symbols if s in self._s
        }

    async def get_events(self, symbol, *, asof=None):
        return (await self.get_events_batch([symbol], asof=asof)).get(symbol, [])


def _payload(result) -> dict[str, Any]:
    return json.loads(re.search(r"```json\n(.*)\n```", result.text, re.DOTALL).group(1))


class DataEventsToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    async def test_happy_path_writes_csv(self) -> None:
        out = Path(self.tmp.name) / "e.csv"
        tool = DataEventsTool(event_provider_factory=lambda ds: _FakeEv({"600519.SH"}))
        result = await tool.execute(symbols="600519.SH,000001.SZ", asof="2026-05-29", output_path=str(out))
        self.assertFalse(result.is_error, msg=result.text)
        p = _payload(result)
        self.assertEqual(p["symbols_with_events"], 1)
        self.assertEqual(p["event_count"], 1)
        self.assertTrue(out.exists())
        self.assertIn("suspension", out.read_text())

    async def test_no_events_is_ok(self) -> None:
        tool = DataEventsTool(event_provider_factory=lambda ds: _FakeEv(set()))
        result = await tool.execute(code="600519.SH")
        self.assertFalse(result.is_error, msg=result.text)
        p = _payload(result)
        self.assertEqual(p["event_count"], 0)
        self.assertEqual(p["status"], "ok")

    async def test_rejects_unknown_kwarg(self) -> None:
        tool = DataEventsTool(event_provider_factory=lambda ds: _FakeEv(set()))
        result = await tool.execute(bogus=1)
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)

    async def test_rejects_invalid_asof(self) -> None:
        tool = DataEventsTool(event_provider_factory=lambda ds: _FakeEv(set()))
        result = await tool.execute(code="600519.SH", asof="not-a-date")
        self.assertTrue(result.is_error)
        self.assertIn("[error:invalid_date]", result.text)

    async def test_fetch_failure_surfaces_error_code(self) -> None:
        tool = DataEventsTool(event_provider_factory=lambda ds: _FakeEv(set(), raise_=True))
        result = await tool.execute(code="600519.SH")
        self.assertTrue(result.is_error)
        self.assertIn("[error:events_fetch_failed]", result.text)


class AkshareEventProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_suspension_membership_and_normalize(self) -> None:
        from doyoutrade.data.event_akshare import AkshareEventProvider

        df = pd.DataFrame({
            "代码": ["600519", "000001"],
            "名称": ["贵州茅台", "平安银行"],
            "停牌时间": ["2026-05-28", "2026-05-29"],
            "停牌原因": ["重大资产重组", "临时停牌"],
        })
        with patch("akshare.stock_tfp_em", return_value=df):
            out = await AkshareEventProvider().get_events_batch(
                ["600519.SH", "300750.SZ"], asof="2026-05-29"
            )
        # 600519 is suspended; 300750 is not in the snapshot.
        self.assertIn("600519.SH", out)
        self.assertNotIn("300750.SZ", out)
        self.assertEqual(out["600519.SH"][0].event_type, "suspension")
        self.assertEqual(out["600519.SH"][0].detail, "重大资产重组")

    async def test_missing_code_column_raises(self) -> None:
        from doyoutrade.data.event_akshare import AkshareEventProvider, _EventSchemaError

        df = pd.DataFrame({"名称": ["x"], "停牌原因": ["y"]})
        with patch("akshare.stock_tfp_em", return_value=df):
            with self.assertRaises(_EventSchemaError):
                await AkshareEventProvider().get_events_batch(["600519.SH"], asof="2026-05-29")

    async def test_empty_snapshot_returns_empty(self) -> None:
        from doyoutrade.data.event_akshare import AkshareEventProvider

        with patch("akshare.stock_tfp_em", return_value=pd.DataFrame()):
            out = await AkshareEventProvider().get_events_batch(["600519.SH"], asof="2026-05-29")
        self.assertEqual(out, {})

    async def test_persistent_failure_reraises(self) -> None:
        from doyoutrade.data.event_akshare import AkshareEventProvider

        with patch("akshare.stock_tfp_em", side_effect=RuntimeError("net")), \
             patch("time.sleep", return_value=None):
            with self.assertRaises(RuntimeError):
                await AkshareEventProvider().get_events_batch(["600519.SH"], asof="2026-05-29")


if __name__ == "__main__":
    unittest.main()
