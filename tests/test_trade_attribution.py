"""FIFO round-trip attribution over the ``trades/`` KB partition.

Covers :mod:`doyoutrade.knowledge.attribution`:

* FIFO correctness — single complete round-trip, batched-buy → single sell,
  single buy → batched sells, same-day T+0, sell-exceeds-buy (orphan),
  un-flattened tail (not counted as realised), multi-symbol.
* Multi-broker column-name tolerance (2-3 synthetic header layouts).
* Missing core columns → unparsed (no bogus P&L).
* Money is decimal strings; win_rate / profit_factor / avg_hold_days maths.
* ``months`` window filtering.

Each test uses a temp ``DOYOUTRADE_HOME`` so ``knowledge_root()`` resolves to an
isolated KB (or passes ``root=`` directly). No network, no DB, pure CSV.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from doyoutrade.knowledge.attribution import read_trade_attribution


def _write_csv(root: Path, rel: str, header: str, rows: list[str]) -> None:
    p = root / "trades" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    body = header + "\n" + "\n".join(rows) + "\n"
    p.write_text(body, encoding="utf-8")


class TradeAttributionFifoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "knowledge"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- Standard (华泰-style) header used across most tests ---
    _HT_HEADER = "成交日期,成交时间,证券代码,证券名称,买卖标志,成交价格,成交数量,成交金额"

    def test_single_complete_round_trip(self):
        _write_csv(
            self.root,
            "huatai/2026-05.csv",
            self._HT_HEADER,
            [
                "2026-05-06,09:35:00,600519,贵州茅台,买入,1800,100,180000",
                "2026-05-08,14:20:00,600519,贵州茅台,卖出,1900,100,190000",
            ],
        )
        result = read_trade_attribution(root=self.root)
        rts = result["round_trips"]
        self.assertEqual(len(rts), 1)
        rt = rts[0]
        self.assertEqual(rt["symbol"], "600519")
        self.assertEqual(rt["name"], "贵州茅台")
        self.assertEqual(rt["open_date"], "2026-05-06")
        self.assertEqual(rt["close_date"], "2026-05-08")
        self.assertEqual(rt["qty"], "100")
        self.assertEqual(rt["avg_buy"], "1800")
        self.assertEqual(rt["avg_sell"], "1900")
        self.assertEqual(rt["realized_pnl"], "10000")  # (1900-1800)*100
        self.assertEqual(rt["return_pct"], round(10000 / 180000 * 100, 4))
        self.assertEqual(rt["hold_days"], 2)
        # Money is a string, not a float.
        self.assertIsInstance(rt["realized_pnl"], str)
        # summary
        s = result["summary"]
        self.assertEqual(s["round_trips"], 1)
        self.assertEqual(s["win_count"], 1)
        self.assertEqual(s["loss_count"], 0)
        self.assertEqual(s["win_rate"], 1.0)
        self.assertEqual(s["total_realized_pnl"], "10000")
        self.assertEqual(s["open_positions"], 0)
        # profit_factor: no losses → None (don't fabricate ∞).
        self.assertIsNone(s["profit_factor"])
        self.assertEqual(s["avg_hold_days"], 2.0)

    def test_batched_buys_then_single_sell(self):
        # Two buys at different prices, one sell that flattens everything.
        # weighted avg buy = (100*10 + 100*20)/200 = 15
        _write_csv(
            self.root,
            "2026-05.csv",
            self._HT_HEADER,
            [
                "2026-05-01,09:31:00,000001,平安银行,买入,10,100,1000",
                "2026-05-02,09:31:00,000001,平安银行,买入,20,100,2000",
                "2026-05-05,10:00:00,000001,平安银行,卖出,18,200,3600",
            ],
        )
        result = read_trade_attribution(root=self.root)
        rts = result["round_trips"]
        self.assertEqual(len(rts), 1)
        rt = rts[0]
        self.assertEqual(rt["qty"], "200")
        self.assertEqual(rt["avg_buy"], "15")
        self.assertEqual(rt["avg_sell"], "18")
        # proceeds 3600 - cost 3000 = 600
        self.assertEqual(rt["realized_pnl"], "600")
        self.assertEqual(rt["open_date"], "2026-05-01")  # first buy
        self.assertEqual(rt["close_date"], "2026-05-05")

    def test_single_buy_then_batched_sells(self):
        # One buy of 200; sold in two lots. FIFO drains the single lot; the
        # position only reaches flat on the SECOND sell → ONE round-trip.
        _write_csv(
            self.root,
            "2026-05.csv",
            self._HT_HEADER,
            [
                "2026-05-01,09:31:00,000002,万科A,买入,10,200,2000",
                "2026-05-03,09:31:00,000002,万科A,卖出,12,100,1200",
                "2026-05-04,09:31:00,000002,万科A,卖出,8,100,800",
            ],
        )
        result = read_trade_attribution(root=self.root)
        rts = result["round_trips"]
        self.assertEqual(len(rts), 1)
        rt = rts[0]
        self.assertEqual(rt["qty"], "200")
        # buy cost 2000; proceeds 1200+800=2000 → flat P&L 0
        self.assertEqual(rt["realized_pnl"], "0")
        self.assertEqual(rt["close_date"], "2026-05-04")  # closed on last sell
        # A flat round-trip counts as neither win nor loss.
        s = result["summary"]
        self.assertEqual(s["win_count"], 0)
        self.assertEqual(s["loss_count"], 0)
        self.assertEqual(s["flat_count"], 1)
        self.assertIsNone(s["win_rate"])  # no decided round-trips

    def test_same_day_t0_ordered_by_time(self):
        # Buy then sell same day; T+0-style. Ordered by成交时间.
        _write_csv(
            self.root,
            "2026-05.csv",
            self._HT_HEADER,
            [
                "2026-05-06,09:31:00,300750,宁德时代,买入,200,100,20000",
                "2026-05-06,14:55:00,300750,宁德时代,卖出,210,100,21000",
            ],
        )
        result = read_trade_attribution(root=self.root)
        rts = result["round_trips"]
        self.assertEqual(len(rts), 1)
        rt = rts[0]
        self.assertEqual(rt["realized_pnl"], "1000")
        self.assertEqual(rt["hold_days"], 0)  # same calendar day
        self.assertEqual(rt["open_date"], rt["close_date"])

    def test_orphan_sell_no_phantom_position(self):
        # A sell with no matching buy → orphan_sell in unparsed, no round-trip,
        # no negative position.
        _write_csv(
            self.root,
            "2026-05.csv",
            self._HT_HEADER,
            [
                "2026-05-06,09:31:00,600000,浦发银行,卖出,10,100,1000",
            ],
        )
        result = read_trade_attribution(root=self.root)
        self.assertEqual(result["round_trips"], [])
        self.assertEqual(result["summary"]["round_trips"], 0)
        self.assertEqual(result["summary"]["open_positions"], 0)
        orphans = [u for u in result["unparsed"] if u["reason"] == "orphan_sell"]
        self.assertEqual(len(orphans), 1)
        self.assertIn("600000", orphans[0]["path"])

    def test_partial_orphan_sell_pairs_what_it_can(self):
        # Buy 100, sell 150 → 100 paired into a round-trip, 50 orphaned.
        _write_csv(
            self.root,
            "2026-05.csv",
            self._HT_HEADER,
            [
                "2026-05-01,09:31:00,600000,浦发银行,买入,10,100,1000",
                "2026-05-02,09:31:00,600000,浦发银行,卖出,12,150,1800",
            ],
        )
        result = read_trade_attribution(root=self.root)
        rts = result["round_trips"]
        self.assertEqual(len(rts), 1)
        # Only the 100 matched shares count. proceeds 100*12=1200, cost 1000.
        self.assertEqual(rts[0]["qty"], "100")
        self.assertEqual(rts[0]["realized_pnl"], "200")
        orphans = [u for u in result["unparsed"] if u["reason"] == "orphan_sell"]
        self.assertEqual(len(orphans), 1)

    def test_open_tail_not_counted_as_realized(self):
        # Buy 200, sell 100 → one round-trip on the sold 100 is NOT created
        # because the position never went flat; instead this is an OPEN
        # position. No realised P&L, open_positions=1.
        _write_csv(
            self.root,
            "2026-05.csv",
            self._HT_HEADER,
            [
                "2026-05-01,09:31:00,000001,平安银行,买入,10,200,2000",
                "2026-05-02,09:31:00,000001,平安银行,卖出,12,100,1200",
            ],
        )
        result = read_trade_attribution(root=self.root)
        # Position: bought 200, sold 100 → still holding 100. Never flat, so no
        # finalised round-trip.
        self.assertEqual(result["round_trips"], [])
        s = result["summary"]
        self.assertEqual(s["round_trips"], 0)
        self.assertEqual(s["open_positions"], 1)
        self.assertEqual(s["total_realized_pnl"], "0")

    def test_two_round_trips_then_reopen(self):
        # Full round-trip, then rebuild+flatten again = 2 round-trips, then a
        # dangling buy = 1 open position.
        _write_csv(
            self.root,
            "2026-05.csv",
            self._HT_HEADER,
            [
                "2026-05-01,09:31:00,600519,贵州茅台,买入,100,10,1000",
                "2026-05-02,09:31:00,600519,贵州茅台,卖出,110,10,1100",  # +100
                "2026-05-10,09:31:00,600519,贵州茅台,买入,120,10,1200",
                "2026-05-11,09:31:00,600519,贵州茅台,卖出,115,10,1150",  # -50
                "2026-05-20,09:31:00,600519,贵州茅台,买入,130,10,1300",  # open tail
            ],
        )
        result = read_trade_attribution(root=self.root)
        rts = result["round_trips"]
        self.assertEqual(len(rts), 2)
        # Sorted close_date descending → the -50 (2026-05-11) comes first.
        self.assertEqual(rts[0]["realized_pnl"], "-50")
        self.assertEqual(rts[1]["realized_pnl"], "100")
        s = result["summary"]
        self.assertEqual(s["round_trips"], 2)
        self.assertEqual(s["win_count"], 1)
        self.assertEqual(s["loss_count"], 1)
        self.assertEqual(s["win_rate"], 0.5)
        self.assertEqual(s["total_realized_pnl"], "50")  # 100 - 50
        self.assertEqual(s["avg_win"], "100")
        self.assertEqual(s["avg_loss"], "-50")
        # profit_factor = gross win 100 / gross loss 50 = 2.0
        self.assertEqual(s["profit_factor"], 2.0)
        self.assertEqual(s["open_positions"], 1)  # the dangling 2026-05-20 buy
        # best / worst
        self.assertEqual(s["best"]["realized_pnl"], "100")
        self.assertEqual(s["worst"]["realized_pnl"], "-50")

    def test_multi_symbol_grouping(self):
        _write_csv(
            self.root,
            "2026-05.csv",
            self._HT_HEADER,
            [
                "2026-05-01,09:31:00,600519,贵州茅台,买入,1800,10,18000",
                "2026-05-02,09:31:00,600519,贵州茅台,卖出,1900,10,19000",  # +1000
                "2026-05-01,09:31:00,000001,平安银行,买入,10,100,1000",
                "2026-05-03,09:31:00,000001,平安银行,卖出,9,100,900",  # -100
            ],
        )
        result = read_trade_attribution(root=self.root)
        by_symbol = result["by_symbol"]
        self.assertEqual(len(by_symbol), 2)
        # sorted by realized_pnl desc → 600519 (+1000) first.
        self.assertEqual(by_symbol[0]["symbol"], "600519")
        self.assertEqual(by_symbol[0]["realized_pnl"], "1000")
        self.assertEqual(by_symbol[0]["win_rate"], 1.0)
        self.assertEqual(by_symbol[1]["symbol"], "000001")
        self.assertEqual(by_symbol[1]["realized_pnl"], "-100")
        self.assertEqual(by_symbol[1]["win_rate"], 0.0)

    def test_cross_file_symbol_history_paired(self):
        # A symbol's buy in one monthly file, sell in the next → still pairs.
        _write_csv(
            self.root,
            "2026-04.csv",
            self._HT_HEADER,
            ["2026-04-20,09:31:00,600519,贵州茅台,买入,1800,10,18000"],
        )
        _write_csv(
            self.root,
            "2026-05.csv",
            self._HT_HEADER,
            ["2026-05-06,09:31:00,600519,贵州茅台,卖出,1900,10,19000"],
        )
        result = read_trade_attribution(root=self.root)
        self.assertEqual(len(result["round_trips"]), 1)
        self.assertEqual(result["round_trips"][0]["realized_pnl"], "1000")
        self.assertEqual(result["round_trips"][0]["hold_days"], 16)


class TradeAttributionBrokerFormatTests(unittest.TestCase):
    """Column-alias tolerance across different broker export layouts."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "knowledge"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_guojun_style_headers(self):
        # 国君-style: 交易日期 / 股票代码 / 委托类别 / 成交均价 / 成交股数,
        # no explicit amount column (falls back to price*qty), verbose side.
        header = "交易日期,股票代码,股票名称,委托类别,成交均价,成交股数"
        _write_csv(
            self.root,
            "guojun/2026-05.csv",
            header,
            [
                "2026-05-01,600519,贵州茅台,证券买入,1800,100",
                "2026-05-02,600519,贵州茅台,证券卖出,1850,100",
            ],
        )
        result = read_trade_attribution(root=self.root)
        rts = result["round_trips"]
        self.assertEqual(len(rts), 1)
        # amount derived from price*qty; pnl = (1850-1800)*100 = 5000
        self.assertEqual(rts[0]["realized_pnl"], "5000")
        self.assertEqual(rts[0]["symbol"], "600519")

    def test_eastmoney_compact_headers(self):
        # 东财-style compact: 日期 / 代码 / 名称 / 操作 / 价格 / 数量 / 金额,
        # compact side codes.
        header = "日期,代码,名称,操作,价格,数量,金额"
        _write_csv(
            self.root,
            "eastmoney/2026-05.csv",
            header,
            [
                "20260501,000001,平安银行,买,10,100,1000",
                "20260502,000001,平安银行,卖,11,100,1100",
            ],
        )
        result = read_trade_attribution(root=self.root)
        rts = result["round_trips"]
        self.assertEqual(len(rts), 1)
        self.assertEqual(rts[0]["symbol"], "000001")
        self.assertEqual(rts[0]["open_date"], "2026-05-01")  # 20260501 parsed
        self.assertEqual(rts[0]["realized_pnl"], "100")

    def test_bom_and_fullwidth_and_slash_date(self):
        # BOM on first header cell, slash-form dates, verbose 买卖方向 side.
        header = "﻿发生日期,证券编码,证券简称,买卖方向,成交价,数量,发生金额"
        _write_csv(
            self.root,
            "yinhe/2026-05.csv",
            header,
            [
                "2026/05/01,600000,浦发银行,买入,10,1000,10000",
                "2026/05/02,600000,浦发银行,卖出,12,1000,12000",
            ],
        )
        result = read_trade_attribution(root=self.root)
        rts = result["round_trips"]
        self.assertEqual(len(rts), 1)
        self.assertEqual(rts[0]["symbol"], "600000")
        self.assertEqual(rts[0]["open_date"], "2026-05-01")
        self.assertEqual(rts[0]["realized_pnl"], "2000")

    def test_missing_core_columns_unparsed(self):
        # A CSV with no recognisable side / price columns → unparsed, no P&L.
        header = "日期,代码,备注"
        _write_csv(
            self.root,
            "mystery/2026-05.csv",
            header,
            ["2026-05-01,600519,随便写点"],
        )
        result = read_trade_attribution(root=self.root)
        self.assertEqual(result["round_trips"], [])
        unp = result["unparsed"]
        self.assertEqual(len(unp), 1)
        self.assertEqual(unp[0]["reason"], "core_columns_unmapped")
        self.assertIn("mystery/2026-05.csv", unp[0]["path"])

    def test_non_trade_rows_recorded_not_paired(self):
        # 红利 / 申购 rows are not buy/sell → recorded as non_trade_side skips.
        header = TradeAttributionFifoTests._HT_HEADER
        _write_csv(
            self.root,
            "2026-05.csv",
            header,
            [
                "2026-05-01,09:31:00,600519,贵州茅台,买入,1800,100,180000",
                "2026-05-02,09:31:00,600519,贵州茅台,红利入账,0,0,50",
                "2026-05-03,09:31:00,600519,贵州茅台,卖出,1900,100,190000",
            ],
        )
        result = read_trade_attribution(root=self.root)
        self.assertEqual(len(result["round_trips"]), 1)
        self.assertEqual(result["round_trips"][0]["realized_pnl"], "10000")
        non_trade = [u for u in result["unparsed"] if u["reason"] == "non_trade_side"]
        self.assertEqual(len(non_trade), 1)

    def test_bad_row_values_skipped_loudly(self):
        # A row with an unparseable price → bad_row_values, others still pair.
        header = TradeAttributionFifoTests._HT_HEADER
        _write_csv(
            self.root,
            "2026-05.csv",
            header,
            [
                "2026-05-01,09:31:00,600519,贵州茅台,买入,N/A,100,180000",  # bad price
                "2026-05-02,09:31:00,000001,平安银行,买入,10,100,1000",
                "2026-05-03,09:31:00,000001,平安银行,卖出,11,100,1100",
            ],
        )
        result = read_trade_attribution(root=self.root)
        self.assertEqual(len(result["round_trips"]), 1)
        self.assertEqual(result["round_trips"][0]["symbol"], "000001")
        bad = [u for u in result["unparsed"] if u["reason"] == "bad_row_values"]
        self.assertEqual(len(bad), 1)


class TradeAttributionWindowAndEmptyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "knowledge"
        self.root.mkdir(parents=True)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_kb_structured_empty(self):
        result = read_trade_attribution(root=self.root)
        self.assertEqual(result["round_trips"], [])
        self.assertEqual(result["by_symbol"], [])
        self.assertEqual(result["unparsed"], [])
        s = result["summary"]
        self.assertEqual(s["round_trips"], 0)
        self.assertIsNone(s["win_rate"])
        self.assertIsNone(s["profit_factor"])
        self.assertIsNone(s["avg_hold_days"])
        self.assertIsNone(s["best"])
        self.assertIsNone(s["worst"])
        self.assertEqual(s["total_realized_pnl"], "0")
        self.assertEqual(s["open_positions"], 0)

    def test_months_window_drops_older(self):
        header = TradeAttributionFifoTests._HT_HEADER
        _write_csv(
            self.root,
            "trades.csv",
            header,
            [
                # Old round-trip closed 2026-03.
                "2026-03-01,09:31:00,600519,贵州茅台,买入,1800,10,18000",
                "2026-03-02,09:31:00,600519,贵州茅台,卖出,1900,10,19000",
                # Recent round-trip closed 2026-07.
                "2026-07-01,09:31:00,000001,平安银行,买入,10,100,1000",
                "2026-07-02,09:31:00,000001,平安银行,卖出,11,100,1100",
            ],
        )
        # Newest close is 2026-07; months=1 keeps only >= 2026-07-01.
        result = read_trade_attribution(root=self.root, months=1)
        rts = result["round_trips"]
        self.assertEqual(len(rts), 1)
        self.assertEqual(rts[0]["symbol"], "000001")
        self.assertEqual(rts[0]["close_date"], "2026-07-02")
        # Without a window, both are present.
        allr = read_trade_attribution(root=self.root)
        self.assertEqual(len(allr["round_trips"]), 2)

    def test_uses_knowledge_root_env(self):
        # When root is not passed, knowledge_root() (honouring DOYOUTRADE_HOME)
        # is used.
        header = TradeAttributionFifoTests._HT_HEADER
        home = self.tmp / "home"
        kb = home / "knowledge"
        _write_csv(
            kb,
            "2026-05.csv",
            header,
            [
                "2026-05-01,09:31:00,600519,贵州茅台,买入,1800,10,18000",
                "2026-05-02,09:31:00,600519,贵州茅台,卖出,1900,10,19000",
            ],
        )
        prev = os.environ.get("DOYOUTRADE_HOME")
        os.environ["DOYOUTRADE_HOME"] = str(home)
        try:
            result = read_trade_attribution()
        finally:
            if prev is None:
                os.environ.pop("DOYOUTRADE_HOME", None)
            else:
                os.environ["DOYOUTRADE_HOME"] = prev
        self.assertEqual(len(result["round_trips"]), 1)
        self.assertEqual(result["round_trips"][0]["realized_pnl"], "1000")

    def test_money_is_decimal_string_not_float(self):
        # Fractional prices must stay exact decimal strings.
        header = TradeAttributionFifoTests._HT_HEADER
        _write_csv(
            self.root,
            "2026-05.csv",
            header,
            [
                "2026-05-01,09:31:00,600519,贵州茅台,买入,10.10,100,1010",
                "2026-05-02,09:31:00,600519,贵州茅台,卖出,10.30,100,1030",
            ],
        )
        result = read_trade_attribution(root=self.root)
        rt = result["round_trips"][0]
        self.assertIsInstance(rt["realized_pnl"], str)
        # (10.30-10.10)*100 = 20.00 exactly (not 19.999...)
        self.assertEqual(Decimal(rt["realized_pnl"]), Decimal("20"))
        self.assertEqual(rt["realized_pnl"], "20")


if __name__ == "__main__":
    unittest.main()
