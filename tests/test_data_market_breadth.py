"""Tests for the ``doyoutrade-cli data breadth`` command / ``data_market_breadth`` tool.

Pins the contract:

* the akshare limit-pool provider fetches the three 打板 pools
  (``stock_zt_pool_em`` / ``stock_zt_pool_dtgc_em`` / ``stock_zt_pool_zbgc_em``),
  parses the 中文 columns, canonicalizes codes to CODE.EXCHANGE, aggregates
  the 连板梯队 ladder + 炸板率, and never coerces an unparseable numeric to 0,
* an all-empty day returns three empty lists (→ ``market_breadth_empty``) while
  a per-pool upstream failure is recorded on ``MarketBreadth.pool_errors`` (never
  silently swallowed) so the tool can surface ``partial`` — distinct failure
  modes,
* the sentiment thermometer is a rule-based, ordered, single-day, non-predictive
  label with the fixed disclaimer and echoed raw inputs,
* the tool resolves the trade date (default today, Asia/Shanghai), writes the
  three pool CSVs + manifest under the artifacts root, and surfaces stable
  error_codes for invalid_date / unknown_data_source / unknown_arguments.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pandas as pd

from tests._tool_result_helpers import payload as _payload
from doyoutrade.api.operations.data_market_breadth import (
    DataMarketBreadthTool,
    _classify_sentiment,
)
from doyoutrade.core.models import LimitPoolStock, MarketBreadth
from doyoutrade.data.limit_pool_akshare import AkshareMarketBreadthProvider
from doyoutrade.data.protocols import MarketBreadthProvider


# ---------------------------------------------------------------------------
# Fake akshare pool frames (mirror the documented column names).
# ---------------------------------------------------------------------------


def _fake_zt_pool() -> pd.DataFrame:
    """涨停池 stock_zt_pool_em — carries 连板数."""
    return pd.DataFrame(
        [
            {
                "序号": 1, "代码": "600519", "名称": "贵州茅台", "涨跌幅": 10.0,
                "最新价": 1800.0, "成交额": 5.0e9, "流通市值": 2.2e12,
                "总市值": 2.2e12, "换手率": 1.2, "封板资金": 1.0e8,
                "首次封板时间": "09:35:00", "最后封板时间": "09:35:00",
                "炸板次数": 0, "涨停统计": "3/3", "连板数": 3, "所属行业": "白酒",
            },
            {
                "序号": 2, "代码": "000001", "名称": "平安银行", "涨跌幅": 10.02,
                "最新价": 12.0, "成交额": 3.0e9, "流通市值": 2.0e11,
                "总市值": 2.3e11, "换手率": 3.4, "封板资金": 2.0e8,
                "首次封板时间": "10:01:00", "最后封板时间": "10:01:00",
                "炸板次数": 1, "涨停统计": "1/1", "连板数": 1, "所属行业": "银行",
            },
            {
                "序号": 3, "代码": "300750", "名称": "宁德时代", "涨跌幅": 20.0,
                "最新价": 200.0, "成交额": 8.0e9, "流通市值": 8.0e11,
                "总市值": 8.8e11, "换手率": 2.1, "封板资金": 5.0e8,
                "首次封板时间": "09:30:00", "最后封板时间": "09:30:00",
                "炸板次数": 0, "涨停统计": "1/1", "连板数": 1, "所属行业": "电池",
            },
        ]
    )


def _fake_dt_pool() -> pd.DataFrame:
    """跌停池 stock_zt_pool_dtgc_em."""
    return pd.DataFrame(
        [
            {
                "序号": 1, "代码": "002415", "名称": "海康威视", "涨跌幅": -10.0,
                "最新价": 30.0, "成交额": 2.0e9, "流通市值": 3.0e11,
                "总市值": 3.0e11, "动态市盈率": 15.0, "换手率": 1.1,
                "封单资金": 1.0e8, "最后封板时间": "14:30:00", "板上成交额": 5.0e7,
                "连续跌停": 1, "开板次数": 0, "所属行业": "安防",
            },
        ]
    )


def _fake_zb_pool() -> pd.DataFrame:
    """炸板池 stock_zt_pool_zbgc_em."""
    return pd.DataFrame(
        [
            {
                "序号": 1, "代码": "601899", "名称": "紫金矿业", "涨跌幅": 6.5,
                "最新价": 18.0, "涨停价": 19.8, "成交额": 4.0e9, "流通市值": 4.0e11,
                "总市值": 4.5e11, "换手率": 2.0, "涨速": 0.1,
                "首次封板时间": "10:15:00", "炸板次数": 2, "涨停统计": "0/1",
                "振幅": 8.0, "所属行业": "有色",
            },
        ]
    )


def _patch_ak(zt=None, dt=None, zb=None, *, zt_exc=None, dt_exc=None, zb_exc=None):
    """Build the three akshare-pool patchers (frames or exceptions).

    Returns a list of three unentered context managers the caller enters
    together (``with ps[0], ps[1], ps[2]:``).
    """
    patches = []
    for target, frame, exc in (
        ("doyoutrade.data.limit_pool_akshare.ak.stock_zt_pool_em", zt, zt_exc),
        ("doyoutrade.data.limit_pool_akshare.ak.stock_zt_pool_dtgc_em", dt, dt_exc),
        ("doyoutrade.data.limit_pool_akshare.ak.stock_zt_pool_zbgc_em", zb, zb_exc),
    ):
        if exc is not None:
            patches.append(patch(target, side_effect=exc))
        else:
            patches.append(
                patch(target, return_value=frame if frame is not None else pd.DataFrame())
            )
    return patches


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
# Provider
# ---------------------------------------------------------------------------


class AkshareMarketBreadthProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_satisfies_market_breadth_provider_protocol(self) -> None:
        self.assertIsInstance(AkshareMarketBreadthProvider(), MarketBreadthProvider)

    async def test_aggregates_pools_ladder_and_canonicalizes(self) -> None:
        ps = _patch_ak(_fake_zt_pool(), _fake_dt_pool(), _fake_zb_pool())
        with ps[0], ps[1], ps[2]:
            breadth = await AkshareMarketBreadthProvider().fetch_market_breadth("20260703")

        self.assertIsInstance(breadth, MarketBreadth)
        self.assertEqual(breadth.limit_up_count, 3)
        self.assertEqual(breadth.limit_down_count, 1)
        self.assertEqual(breadth.broken_board_count, 1)
        # Ladder: two names at 1-board, one at 3-board.
        self.assertEqual(breadth.ladder, {"1": 2, "3": 1})
        self.assertEqual(breadth.max_streak, 3)
        # 炸板率 = 1 / (3 + 1) = 0.25.
        self.assertAlmostEqual(breadth.broken_board_rate, 0.25)
        self.assertEqual(breadth.pool_errors, {})
        # Canonicalization + fields.
        first = breadth.limit_up[0]
        self.assertIsInstance(first, LimitPoolStock)
        self.assertEqual(first.code, "600519")
        self.assertEqual(first.symbol, "600519.SH")
        self.assertEqual(first.streak, 3)
        self.assertEqual(first.pool, "limit_up")
        self.assertEqual(first.provider, "akshare")
        self.assertEqual(breadth.limit_up[1].symbol, "000001.SZ")
        self.assertEqual(breadth.limit_up[2].symbol, "300750.SZ")
        self.assertEqual(breadth.limit_down[0].symbol, "002415.SZ")
        self.assertEqual(breadth.broken_board[0].symbol, "601899.SH")
        # Down / broken pools carry no 连板数 → streak None.
        self.assertIsNone(breadth.limit_down[0].streak)
        self.assertIsNone(breadth.broken_board[0].streak)

    async def test_all_empty_day_returns_empty_lists_no_errors(self) -> None:
        ps = _patch_ak(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        with ps[0], ps[1], ps[2]:
            breadth = await AkshareMarketBreadthProvider().fetch_market_breadth("20260704")
        self.assertEqual(breadth.limit_up_count, 0)
        self.assertEqual(breadth.limit_down_count, 0)
        self.assertEqual(breadth.broken_board_count, 0)
        self.assertEqual(breadth.pool_errors, {})
        self.assertEqual(breadth.max_streak, 0)
        self.assertEqual(breadth.broken_board_rate, 0.0)

    async def test_persistent_pool_failure_recorded_not_swallowed(self) -> None:
        # 涨停池 fails, the other two succeed.
        ps = _patch_ak(
            None, _fake_dt_pool(), _fake_zb_pool(),
            zt_exc=RuntimeError("network down"),
        )
        with ps[0], ps[1], ps[2]:
            with patch("doyoutrade.data.limit_pool_akshare.time.sleep", return_value=None):
                breadth = await AkshareMarketBreadthProvider().fetch_market_breadth("20260703")
        self.assertEqual(breadth.limit_up_count, 0)
        self.assertEqual(breadth.limit_down_count, 1)
        self.assertIn("limit_up", breadth.pool_errors)
        self.assertIn("RuntimeError", breadth.pool_errors["limit_up"])

    async def test_unparseable_streak_excluded_from_ladder(self) -> None:
        df = _fake_zt_pool().copy()
        df["连板数"] = df["连板数"].astype(object)
        df.loc[0, "连板数"] = "N/A"  # unparseable -> None -> not in ladder
        ps = _patch_ak(df, pd.DataFrame(), pd.DataFrame())
        with ps[0], ps[1], ps[2]:
            breadth = await AkshareMarketBreadthProvider().fetch_market_breadth("20260703")
        self.assertIsNone(breadth.limit_up[0].streak)
        # Only the two 1-board names remain in the ladder; the bad row is excluded.
        self.assertEqual(breadth.ladder, {"1": 2})
        self.assertEqual(breadth.max_streak, 1)

    async def test_row_missing_code_skipped(self) -> None:
        df = _fake_zt_pool().copy()
        df.loc[0, "代码"] = None
        ps = _patch_ak(df, pd.DataFrame(), pd.DataFrame())
        with ps[0], ps[1], ps[2]:
            breadth = await AkshareMarketBreadthProvider().fetch_market_breadth("20260703")
        # The code-less row is dropped; the other two survive.
        self.assertEqual(breadth.limit_up_count, 2)


# ---------------------------------------------------------------------------
# Sentiment rule layer
# ---------------------------------------------------------------------------


class SentimentClassifierTests(unittest.TestCase):
    def _label(self, **kw) -> str:
        return _classify_sentiment(**kw)["label"]

    def test_ebb_when_limit_down_ge_limit_up(self) -> None:
        self.assertEqual(
            self._label(zt=30, dt=30, zb=5, max_streak=4, broken_rate=0.1),
            "退潮/低迷",
        )

    def test_ebb_when_broken_rate_high(self) -> None:
        self.assertEqual(
            self._label(zt=60, dt=5, zb=45, max_streak=5, broken_rate=0.43),
            "退潮/低迷",
        )

    def test_ebb_when_thin_and_low_streak(self) -> None:
        self.assertEqual(
            self._label(zt=20, dt=5, zb=3, max_streak=2, broken_rate=0.1),
            "退潮/低迷",
        )

    def test_euphoria(self) -> None:
        self.assertEqual(
            self._label(zt=92, dt=3, zb=10, max_streak=7, broken_rate=0.1),
            "高潮/亢奋",
        )

    def test_fermenting(self) -> None:
        self.assertEqual(
            self._label(zt=55, dt=5, zb=12, max_streak=5, broken_rate=0.18),
            "发酵/活跃",
        )

    def test_divergence(self) -> None:
        self.assertEqual(
            self._label(zt=40, dt=10, zb=15, max_streak=4, broken_rate=0.27),
            "分歧加剧",
        )

    def test_neutral(self) -> None:
        self.assertEqual(
            self._label(zt=40, dt=10, zb=5, max_streak=4, broken_rate=0.11),
            "中性",
        )

    def test_carries_disclaimer_reason_and_inputs(self) -> None:
        s = _classify_sentiment(zt=92, dt=3, zb=10, max_streak=7, broken_rate=0.1)
        self.assertIn("非预测", s["disclaimer"])
        self.assertIn("92", s["reason"])
        self.assertEqual(s["inputs"]["limit_up_count"], 92)
        self.assertEqual(s["inputs"]["max_streak"], 7)
        self.assertEqual(s["inputs"]["broken_board_rate"], 0.1)


# ---------------------------------------------------------------------------
# Tool — fake provider injected via _build_market_breadth_provider
# ---------------------------------------------------------------------------


def _stock(pool: str, code: str, symbol: str, streak: int | None = None) -> LimitPoolStock:
    return LimitPoolStock(
        pool=pool, code=code, symbol=symbol, name="n", provider="akshare",
        change_pct=10.0, latest_price=1.0, streak=streak,
    )


class _FakeProvider:
    def __init__(self, breadth: MarketBreadth | Exception) -> None:
        self._breadth = breadth

    async def fetch_market_breadth(self, trade_date: str) -> MarketBreadth:
        if isinstance(self._breadth, Exception):
            raise self._breadth
        return self._breadth


def _patch_provider(outcome: MarketBreadth | Exception):
    return patch(
        "doyoutrade.api.operations.data_market_breadth._build_market_breadth_provider",
        return_value=(_FakeProvider(outcome), "akshare"),
    )


def _breadth(
    *,
    limit_up=None,
    limit_down=None,
    broken_board=None,
    ladder=None,
    max_streak=0,
    broken_rate=0.0,
    pool_errors=None,
    trade_date="20260703",
) -> MarketBreadth:
    return MarketBreadth(
        trade_date=trade_date,
        provider="akshare",
        limit_up=limit_up or [],
        limit_down=limit_down or [],
        broken_board=broken_board or [],
        ladder=ladder or {},
        max_streak=max_streak,
        broken_board_rate=broken_rate,
        pool_errors=pool_errors or {},
    )


class DataMarketBreadthToolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_ok_writes_csvs_and_envelope(self) -> None:
        breadth = _breadth(
            limit_up=[
                _stock("limit_up", "600519", "600519.SH", streak=3),
                _stock("limit_up", "000001", "000001.SZ", streak=1),
                _stock("limit_up", "300750", "300750.SZ", streak=1),
            ],
            limit_down=[_stock("limit_down", "002415", "002415.SZ")],
            broken_board=[_stock("broken_board", "601899", "601899.SH")],
            ladder={"1": 2, "3": 1},
            max_streak=3,
            broken_rate=0.25,
        )
        with _patch_provider(breadth):
            result = await DataMarketBreadthTool().execute(date="2026-07-03")

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["trade_date"], "20260703")
        self.assertEqual(data["limit_up_count"], 3)
        self.assertEqual(data["limit_down_count"], 1)
        self.assertEqual(data["broken_board_count"], 1)
        self.assertEqual(data["broken_board_rate"], 0.25)
        self.assertEqual(data["max_streak"], 3)
        self.assertEqual(data["ladder"], {"1": 2, "3": 1})
        self.assertEqual(data["data_source"], "akshare")
        # Sentiment present with the required shape.
        self.assertIn("label", data["sentiment"])
        self.assertIn("非预测", data["sentiment"]["disclaimer"])
        self.assertEqual(data["sentiment"]["inputs"]["limit_up_count"], 3)
        # Three CSVs written with documented columns.
        for key in ("limit_up_path", "limit_down_path", "broken_board_path"):
            p = Path(data[key])
            self.assertTrue(p.exists(), msg=key)
        df = pd.read_csv(data["limit_up_path"])
        self.assertEqual(list(df.columns)[:3], ["symbol", "code", "name"])
        self.assertEqual(len(df), 3)
        self.assertTrue(Path(data["manifest_path"]).exists())

    async def test_default_date_used_when_omitted(self) -> None:
        breadth = _breadth(
            limit_up=[_stock("limit_up", "600519", "600519.SH", streak=1)],
            ladder={"1": 1},
            max_streak=1,
        )
        with _patch_provider(breadth):
            result = await DataMarketBreadthTool().execute()
        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        # trade_date resolved to a concrete YYYYMMDD (from the fake breadth).
        self.assertRegex(data["trade_date"], r"^\d{8}$")

    async def test_empty_day_is_distinct_error(self) -> None:
        with _patch_provider(_breadth()):
            result = await DataMarketBreadthTool().execute(date="2026-07-04")
        self.assertTrue(result.is_error)
        self.assertIn("market_breadth_empty", result.text)

    async def test_all_pools_failed_is_fetch_failed(self) -> None:
        breadth = _breadth(
            pool_errors={
                "limit_up": "RuntimeError: boom",
                "limit_down": "RuntimeError: boom",
                "broken_board": "RuntimeError: boom",
            }
        )
        with _patch_provider(breadth):
            result = await DataMarketBreadthTool().execute(date="2026-07-03")
        self.assertTrue(result.is_error)
        self.assertIn("market_breadth_fetch_failed", result.text)

    async def test_partial_when_one_pool_failed(self) -> None:
        breadth = _breadth(
            limit_up=[_stock("limit_up", "600519", "600519.SH", streak=2)],
            ladder={"2": 1},
            max_streak=2,
            pool_errors={"broken_board": "RuntimeError: boom"},
        )
        with _patch_provider(breadth):
            result = await DataMarketBreadthTool().execute(date="2026-07-03")
        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "partial")
        self.assertIn("broken_board", data["pool_errors"])

    async def test_provider_raises_is_fetch_failed(self) -> None:
        with _patch_provider(RuntimeError("upstream exploded")):
            result = await DataMarketBreadthTool().execute(date="2026-07-03")
        self.assertTrue(result.is_error)
        self.assertIn("market_breadth_fetch_failed", result.text)

    async def test_invalid_date_rejected(self) -> None:
        result = await DataMarketBreadthTool().execute(date="2026-13-40")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_date", result.text)

    async def test_invalid_date_shape_rejected(self) -> None:
        result = await DataMarketBreadthTool().execute(date="July 3rd")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_date", result.text)

    async def test_unknown_data_source(self) -> None:
        result = await DataMarketBreadthTool().execute(data_source="tushare")
        self.assertTrue(result.is_error)
        self.assertIn("unknown_data_source", result.text)

    async def test_unknown_argument_rejected(self) -> None:
        result = await DataMarketBreadthTool().execute(date="2026-07-03", bogus="x")
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)


if __name__ == "__main__":
    unittest.main()
