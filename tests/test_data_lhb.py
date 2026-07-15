"""Tests for the ``doyoutrade-cli data lhb`` command / ``data_lhb`` tool.

Pins the contract:

* the akshare 龙虎榜 provider fetches ``stock_lhb_detail_em`` over a
  start/end range, parses the 中文 columns, canonicalizes codes to
  CODE.EXCHANGE, and never coerces an unparseable numeric to 0,
* an empty window returns ``[]`` (→ ``lhb_empty``) while a persistent upstream
  failure re-raises (→ ``lhb_fetch_failed``) — distinct failure modes,
* the tool resolves the window (single ``date`` OR ``start``/``end``, default
  today Asia/Shanghai), writes the CSV + manifest under the artifacts root, and
  surfaces stable error_codes for invalid_date / unknown_data_source /
  unknown_arguments.
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
from doyoutrade.api.operations.data_lhb import DataLhbTool
from doyoutrade.core.models import LhbRow, LhbSeatRow
from doyoutrade.data.lhb_akshare import (
    AkshareDragonTigerProvider,
    LhbNoSeatDataError,
)
from doyoutrade.data.protocols import DragonTigerProvider


# ---------------------------------------------------------------------------
# Fake akshare frame (mirrors the documented column names).
# ---------------------------------------------------------------------------


def _fake_lhb_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "序号": 1, "代码": "600519", "名称": "贵州茅台", "上榜日": "2026-07-03",
                "解读": "机构净买入", "收盘价": 1800.0, "涨跌幅": 9.98,
                "龙虎榜净买额": 1.23e8, "龙虎榜买入额": 3.0e8, "龙虎榜卖出额": 1.77e8,
                "龙虎榜成交额": 4.77e8, "市场总成交额": 1.0e12,
                "净买额占总成交比": 0.5, "成交额占总成交比": 1.2,
                "换手率": 3.2, "流通市值": 2.2e12, "上榜原因": "日涨幅偏离值达7%的证券",
                "上榜后1日": 1.0, "上榜后2日": 2.0, "上榜后5日": 5.0, "上榜后10日": 10.0,
            },
            {
                "序号": 2, "代码": "000001", "名称": "平安银行", "上榜日": "2026-07-03",
                "解读": "游资博弈", "收盘价": 12.0, "涨跌幅": 10.02,
                "龙虎榜净买额": -5.0e7, "龙虎榜买入额": 1.0e8, "龙虎榜卖出额": 1.5e8,
                "龙虎榜成交额": 2.5e8, "市场总成交额": 1.0e12,
                "净买额占总成交比": -0.2, "成交额占总成交比": 0.8,
                "换手率": 5.1, "流通市值": 2.0e11, "上榜原因": "连续三个交易日内涨幅偏离值累计达20%的证券",
                "上榜后1日": None, "上榜后2日": None, "上榜后5日": None, "上榜后10日": None,
            },
        ]
    )


def _fake_seat_frame(flag: str) -> pd.DataFrame:
    """Mirror akshare ``stock_lhb_stock_detail_em`` columns for one side.

    Row 0: 赵老哥's 华鑫上海分公司 seat (a starter-library keyword) → hot_money hit.
    Row 1: an 机构专用 desk → is_institution=True, no hot_money.
    """
    if flag == "买入":
        return pd.DataFrame(
            [
                {
                    "序号": 1,
                    "交易营业部名称": "华鑫证券有限责任公司上海分公司",
                    "买入金额": 5.0e7, "买入金额-占总成交比例": 3.1,
                    "卖出金额": 0.0, "卖出金额-占总成交比例": 0.0,
                    "净额": 5.0e7, "类型": "买一",
                },
                {
                    "序号": 2,
                    "交易营业部名称": "机构专用",
                    "买入金额": 3.0e7, "买入金额-占总成交比例": 1.9,
                    "卖出金额": 0.0, "卖出金额-占总成交比例": 0.0,
                    "净额": 3.0e7, "类型": "机构专用",
                },
            ]
        )
    return pd.DataFrame(
        [
            {
                "序号": 1,
                "交易营业部名称": "某不知名营业部",
                "买入金额": 0.0, "买入金额-占总成交比例": 0.0,
                "卖出金额": 4.0e7, "卖出金额-占总成交比例": 2.5,
                "净额": -4.0e7, "类型": "卖一",
            },
        ]
    )


def _patch_seat_calls(buy_frame, sell_frame):
    """Patch ak.stock_lhb_stock_detail_em to return per-flag frames / errors.

    ``buy_frame`` / ``sell_frame`` may be a DataFrame (returned) or an Exception
    (raised) — matched on the ``flag`` kwarg.
    """

    def _side_effect(*, symbol, date, flag):  # noqa: ANN001
        chosen = buy_frame if flag == "买入" else sell_frame
        if isinstance(chosen, Exception):
            raise chosen
        return chosen

    return patch(
        "doyoutrade.data.lhb_akshare.ak.stock_lhb_stock_detail_em",
        side_effect=_side_effect,
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


class AkshareDragonTigerProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_satisfies_dragon_tiger_provider_protocol(self) -> None:
        self.assertIsInstance(AkshareDragonTigerProvider(), DragonTigerProvider)

    async def test_parses_rows_and_canonicalizes(self) -> None:
        with patch(
            "doyoutrade.data.lhb_akshare.ak.stock_lhb_detail_em",
            return_value=_fake_lhb_frame(),
        ) as mock_fn:
            rows = await AkshareDragonTigerProvider().fetch_dragon_tiger(
                "20260703", "20260703"
            )
        mock_fn.assert_called_once_with(start_date="20260703", end_date="20260703")
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertIsInstance(first, LhbRow)
        self.assertEqual(first.code, "600519")
        self.assertEqual(first.symbol, "600519.SH")
        self.assertEqual(first.name, "贵州茅台")
        self.assertEqual(first.on_date, "2026-07-03")
        self.assertEqual(first.reason, "日涨幅偏离值达7%的证券")
        self.assertEqual(first.interpretation, "机构净买入")
        self.assertAlmostEqual(first.change_pct, 9.98)
        self.assertAlmostEqual(first.net_buy_amount, 1.23e8)
        self.assertAlmostEqual(first.turnover_rate, 3.2)
        self.assertAlmostEqual(first.circulating_mv, 2.2e12)
        self.assertEqual(rows[1].symbol, "000001.SZ")
        self.assertAlmostEqual(rows[1].net_buy_amount, -5.0e7)

    async def test_empty_frame_returns_empty_list(self) -> None:
        with patch(
            "doyoutrade.data.lhb_akshare.ak.stock_lhb_detail_em",
            return_value=pd.DataFrame(),
        ):
            rows = await AkshareDragonTigerProvider().fetch_dragon_tiger(
                "20260704", "20260704"
            )
        self.assertEqual(rows, [])

    async def test_persistent_failure_reraises(self) -> None:
        with patch(
            "doyoutrade.data.lhb_akshare.ak.stock_lhb_detail_em",
            side_effect=RuntimeError("network down"),
        ):
            with patch("doyoutrade.data.lhb_akshare.time.sleep", return_value=None):
                with self.assertRaises(RuntimeError):
                    await AkshareDragonTigerProvider().fetch_dragon_tiger(
                        "20260703", "20260703"
                    )

    async def test_row_missing_code_skipped(self) -> None:
        df = _fake_lhb_frame().copy()
        df.loc[0, "代码"] = None
        with patch(
            "doyoutrade.data.lhb_akshare.ak.stock_lhb_detail_em",
            return_value=df,
        ):
            rows = await AkshareDragonTigerProvider().fetch_dragon_tiger(
                "20260703", "20260703"
            )
        # The code-less row is dropped; the other survives.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "000001")

    async def test_unparseable_numeric_is_none_not_zero(self) -> None:
        df = _fake_lhb_frame().copy()
        df["龙虎榜净买额"] = df["龙虎榜净买额"].astype(object)
        df.loc[0, "龙虎榜净买额"] = "N/A"
        with patch(
            "doyoutrade.data.lhb_akshare.ak.stock_lhb_detail_em",
            return_value=df,
        ):
            rows = await AkshareDragonTigerProvider().fetch_dragon_tiger(
                "20260703", "20260703"
            )
        self.assertIsNone(rows[0].net_buy_amount)


# ---------------------------------------------------------------------------
# Provider — seat mode (stock_lhb_stock_detail_em)
# ---------------------------------------------------------------------------


class AkshareSeatDetailProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_buy_and_sell_sides(self) -> None:
        with _patch_seat_calls(_fake_seat_frame("买入"), _fake_seat_frame("卖出")) as mock_fn:
            rows = await AkshareDragonTigerProvider().fetch_seat_detail(
                "600519.SH", "20260703"
            )
        # Buy side is fetched with the bare 6-digit code (suffix stripped).
        mock_fn.assert_any_call(symbol="600519", date="20260703", flag="买入")
        mock_fn.assert_any_call(symbol="600519", date="20260703", flag="卖出")
        self.assertEqual(len(rows), 3)  # 2 buy + 1 sell
        self.assertTrue(all(isinstance(r, LhbSeatRow) for r in rows))
        buy = [r for r in rows if r.side == "买入"]
        sell = [r for r in rows if r.side == "卖出"]
        self.assertEqual(len(buy), 2)
        self.assertEqual(len(sell), 1)
        # Canonical symbol carried through; date preserved.
        self.assertEqual(buy[0].symbol, "600519.SH")
        self.assertEqual(buy[0].date, "20260703")
        self.assertAlmostEqual(buy[0].buy_amount, 5.0e7)
        self.assertAlmostEqual(buy[0].net_amount, 5.0e7)
        self.assertAlmostEqual(sell[0].sell_amount, 4.0e7)
        self.assertAlmostEqual(sell[0].net_amount, -4.0e7)

    async def test_hot_money_tag_hit_and_miss(self) -> None:
        with _patch_seat_calls(_fake_seat_frame("买入"), _fake_seat_frame("卖出")):
            rows = await AkshareDragonTigerProvider().fetch_seat_detail(
                "600519.SH", "20260703"
            )
        by_name = {r.seat_name: r for r in rows}
        # Substring keyword hit → 赵老哥.
        self.assertEqual(by_name["华鑫证券有限责任公司上海分公司"].hot_money, "赵老哥")
        # Unknown desk → no tag (None), NOT an error.
        self.assertIsNone(by_name["某不知名营业部"].hot_money)

    async def test_institution_seat_detected(self) -> None:
        with _patch_seat_calls(_fake_seat_frame("买入"), _fake_seat_frame("卖出")):
            rows = await AkshareDragonTigerProvider().fetch_seat_detail(
                "600519.SH", "20260703"
            )
        by_name = {r.seat_name: r for r in rows}
        self.assertTrue(by_name["机构专用"].is_institution)
        self.assertIsNone(by_name["机构专用"].hot_money)
        self.assertFalse(by_name["华鑫证券有限责任公司上海分公司"].is_institution)

    async def test_symbol_suffix_stripped_all_exchanges(self) -> None:
        for symbol, bare in (("000788.SZ", "000788"), ("830799.BJ", "830799"), ("600519", "600519")):
            with _patch_seat_calls(_fake_seat_frame("买入"), _fake_seat_frame("卖出")) as mock_fn:
                await AkshareDragonTigerProvider().fetch_seat_detail(symbol, "20260703")
            mock_fn.assert_any_call(symbol=bare, date="20260703", flag="买入")

    async def test_not_on_board_typeerror_maps_to_no_seat_data(self) -> None:
        # akshare's own None-subscript when the name isn't on the board.
        exc = TypeError("'NoneType' object is not subscriptable")
        with _patch_seat_calls(exc, exc):
            with self.assertRaises(LhbNoSeatDataError):
                await AkshareDragonTigerProvider().fetch_seat_detail(
                    "000788.SZ", "20260703"
                )

    async def test_unrelated_typeerror_is_not_no_seat_data(self) -> None:
        # A genuine bug (different TypeError) must NOT be swallowed as
        # no-seat-data — it retries and re-raises as itself.
        exc = TypeError("some other type problem")
        with patch("doyoutrade.data.lhb_akshare.time.sleep", return_value=None):
            with _patch_seat_calls(exc, exc):
                with self.assertRaises(TypeError) as ctx:
                    await AkshareDragonTigerProvider().fetch_seat_detail(
                        "000788.SZ", "20260703"
                    )
        self.assertNotIsInstance(ctx.exception, LhbNoSeatDataError)

    async def test_seat_row_missing_name_skipped(self) -> None:
        buy = _fake_seat_frame("买入").copy()
        buy.loc[0, "交易营业部名称"] = None
        with _patch_seat_calls(buy, _fake_seat_frame("卖出")):
            rows = await AkshareDragonTigerProvider().fetch_seat_detail(
                "600519.SH", "20260703"
            )
        # The name-less buy seat is dropped; the 机构专用 buy + 1 sell survive.
        self.assertEqual(len(rows), 2)
        self.assertNotIn("", {r.seat_name for r in rows})


# ---------------------------------------------------------------------------
# Tool — fake provider injected via _build_dragon_tiger_provider
# ---------------------------------------------------------------------------


def _row(code: str, symbol: str, name: str = "n") -> LhbRow:
    return LhbRow(
        code=code, symbol=symbol, name=name, on_date="2026-07-03",
        provider="akshare", reason="r", change_pct=9.98, net_buy_amount=1.0e8,
    )


def _seat(side: str, seat_name: str, **kw: Any) -> LhbSeatRow:
    return LhbSeatRow(
        side=side, seat_name=seat_name, symbol="600519.SH", date="20260703",
        provider="akshare", **kw,
    )


class _FakeProvider:
    def __init__(
        self,
        outcome: list[LhbRow] | Exception,
        seat_outcome: list[LhbSeatRow] | Exception | None = None,
    ) -> None:
        self._outcome = outcome
        self._seat_outcome = seat_outcome

    async def fetch_dragon_tiger(self, start_date: str, end_date: str) -> list[LhbRow]:
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def fetch_seat_detail(self, symbol: str, date: str) -> list[LhbSeatRow]:
        if isinstance(self._seat_outcome, Exception):
            raise self._seat_outcome
        return self._seat_outcome or []


def _patch_provider(outcome: list[LhbRow] | Exception):
    return patch(
        "doyoutrade.api.operations.data_lhb._build_dragon_tiger_provider",
        return_value=(_FakeProvider(outcome), "akshare"),
    )


def _patch_seat_provider(seat_outcome: list[LhbSeatRow] | Exception):
    return patch(
        "doyoutrade.api.operations.data_lhb._build_dragon_tiger_provider",
        return_value=(_FakeProvider([], seat_outcome=seat_outcome), "akshare"),
    )


class DataLhbToolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_ok_single_date_writes_csv_and_envelope(self) -> None:
        rows = [_row("600519", "600519.SH", "贵州茅台"), _row("000001", "000001.SZ")]
        with _patch_provider(rows):
            result = await DataLhbTool().execute(date="2026-07-03")

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data.get("mode"), "market")
        self.assertEqual(data["start_date"], "20260703")
        self.assertEqual(data["end_date"], "20260703")
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["data_source"], "akshare")
        self.assertEqual(len(data["latest"]), 2)
        self.assertEqual(data["latest"][0]["symbol"], "600519.SH")
        p = Path(data["lhb_path"])
        self.assertTrue(p.exists())
        df = pd.read_csv(p)
        self.assertEqual(list(df.columns)[:4], ["symbol", "code", "name", "on_date"])
        self.assertEqual(len(df), 2)
        self.assertTrue(Path(data["manifest_path"]).exists())

    async def test_ok_range_window(self) -> None:
        with _patch_provider([_row("600519", "600519.SH")]):
            result = await DataLhbTool().execute(start="2026-06-30", end="2026-07-03")
        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["start_date"], "20260630")
        self.assertEqual(data["end_date"], "20260703")

    async def test_default_date_used_when_omitted(self) -> None:
        with _patch_provider([_row("600519", "600519.SH")]):
            result = await DataLhbTool().execute()
        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertRegex(data["start_date"], r"^\d{8}$")
        self.assertEqual(data["start_date"], data["end_date"])

    async def test_empty_is_lhb_empty(self) -> None:
        with _patch_provider([]):
            result = await DataLhbTool().execute(date="2026-07-04")
        self.assertTrue(result.is_error)
        self.assertIn("lhb_empty", result.text)

    async def test_provider_raises_is_fetch_failed(self) -> None:
        with _patch_provider(RuntimeError("upstream exploded")):
            result = await DataLhbTool().execute(date="2026-07-03")
        self.assertTrue(result.is_error)
        self.assertIn("lhb_fetch_failed", result.text)

    async def test_invalid_date_rejected(self) -> None:
        result = await DataLhbTool().execute(date="2026-13-40")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_date", result.text)

    async def test_date_and_range_mutually_exclusive(self) -> None:
        result = await DataLhbTool().execute(date="2026-07-03", start="2026-07-01")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_date", result.text)

    async def test_range_requires_both_ends(self) -> None:
        result = await DataLhbTool().execute(start="2026-07-01")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_date", result.text)

    async def test_start_after_end_rejected(self) -> None:
        result = await DataLhbTool().execute(start="2026-07-05", end="2026-07-01")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_date", result.text)

    async def test_unknown_data_source(self) -> None:
        result = await DataLhbTool().execute(data_source="tushare")
        self.assertTrue(result.is_error)
        self.assertIn("unknown_data_source", result.text)

    async def test_unknown_argument_rejected(self) -> None:
        result = await DataLhbTool().execute(date="2026-07-03", bogus="x")
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)


# ---------------------------------------------------------------------------
# Tool — seat mode (--symbol)
# ---------------------------------------------------------------------------


class DataLhbSeatModeToolTests(_HomeArtifactsMixin, unittest.IsolatedAsyncioTestCase):
    async def test_seat_mode_writes_csv_and_envelope(self) -> None:
        seats = [
            _seat("买入", "华鑫证券有限责任公司上海分公司", buy_amount=5.0e7,
                  net_amount=5.0e7, hot_money="赵老哥"),
            _seat("买入", "机构专用", buy_amount=3.0e7, net_amount=3.0e7,
                  seat_type="机构专用", is_institution=True),
            _seat("卖出", "某不知名营业部", sell_amount=4.0e7, net_amount=-4.0e7),
        ]
        with _patch_seat_provider(seats):
            result = await DataLhbTool().execute(symbol="600519.SH", date="2026-07-03")

        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["mode"], "seats")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["symbol"], "600519.SH")
        self.assertEqual(data["date"], "20260703")
        self.assertEqual(data["buy_count"], 2)
        self.assertEqual(data["sell_count"], 1)
        self.assertEqual(len(data["buy_seats"]), 2)
        self.assertEqual(len(data["sell_seats"]), 1)
        # Seat dicts carry the required fields.
        first_buy = data["buy_seats"][0]
        for key in ("seat_name", "net_amount", "buy_amount", "sell_amount",
                    "seat_type", "hot_money", "is_institution"):
            self.assertIn(key, first_buy)
        self.assertEqual(first_buy["hot_money"], "赵老哥")
        # CSV exists with the documented column order.
        p = Path(data["seats_path"])
        self.assertTrue(p.exists())
        df = pd.read_csv(p)
        self.assertEqual(
            list(df.columns)[:6],
            ["symbol", "date", "side", "seat_name", "seat_type", "hot_money"],
        )
        self.assertEqual(len(df), 3)
        self.assertTrue(Path(data["manifest_path"]).exists())

    async def test_seat_mode_default_date_when_omitted(self) -> None:
        with _patch_seat_provider([_seat("买入", "华鑫证券有限责任公司上海分公司")]):
            result = await DataLhbTool().execute(symbol="600519.SH")
        self.assertFalse(result.is_error, msg=result.text)
        data = _payload(result)
        self.assertEqual(data["mode"], "seats")
        self.assertRegex(data["date"], r"^\d{8}$")

    async def test_seat_mode_range_rejected(self) -> None:
        result = await DataLhbTool().execute(
            symbol="600519.SH", start="2026-07-01", end="2026-07-03"
        )
        self.assertTrue(result.is_error)
        self.assertIn("invalid_date", result.text)

    async def test_seat_mode_no_seat_data(self) -> None:
        with _patch_seat_provider(LhbNoSeatDataError("not on board")):
            result = await DataLhbTool().execute(symbol="000788.SZ", date="2026-07-03")
        self.assertTrue(result.is_error)
        self.assertIn("lhb_no_seat_data", result.text)
        self.assertNotIn("lhb_fetch_failed", result.text)

    async def test_seat_mode_empty_is_no_seat_data(self) -> None:
        with _patch_seat_provider([]):
            result = await DataLhbTool().execute(symbol="600519.SH", date="2026-07-03")
        self.assertTrue(result.is_error)
        self.assertIn("lhb_no_seat_data", result.text)

    async def test_seat_mode_other_error_is_fetch_failed(self) -> None:
        with _patch_seat_provider(RuntimeError("network down")):
            result = await DataLhbTool().execute(symbol="600519.SH", date="2026-07-03")
        self.assertTrue(result.is_error)
        self.assertIn("lhb_fetch_failed", result.text)
        self.assertNotIn("lhb_no_seat_data", result.text)

    async def test_seat_mode_invalid_date(self) -> None:
        result = await DataLhbTool().execute(symbol="600519.SH", date="2026-13-40")
        self.assertTrue(result.is_error)
        self.assertIn("invalid_date", result.text)

    async def test_seat_mode_unknown_argument_rejected(self) -> None:
        result = await DataLhbTool().execute(symbol="600519.SH", bogus="x")
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)


if __name__ == "__main__":
    unittest.main()
