"""Unit tests for the backtest summary compute module.

The module under test is ``doyoutrade.backtest.summary``. These tests treat
``compute_summary`` and ``summary_to_json`` as pure functions: no DB / no
network. They cover the metric formulas, FIFO matching edges, the equity
curve / max-drawdown coupling, the downsampling rule, and JSON shape
guarantees from the design spec.
"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from doyoutrade.backtest.summary import (
    EquityPoint,
    FillRecord,
    FinalPosition,
    compute_summary,
    summary_to_json,
)


def _ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _trading_dates(start: datetime, days: int) -> list[str]:
    return [
        (start.replace(hour=0, minute=0, second=0, microsecond=0) - start.replace(year=start.year))
        for _ in range(days)
    ]


def _consecutive_iso_dates(start: datetime, days: int) -> list[str]:
    out: list[str] = []
    base = start.date()
    for i in range(days):
        out.append(base.replace(day=base.day) if False else (base.fromordinal(base.toordinal() + i)).isoformat())
    return out


class ComputeSummaryFifoTests(unittest.TestCase):
    """FIFO matching, win rate, holding period, trade counts."""

    def _equity_history_flat(self) -> list[EquityPoint]:
        return [
            EquityPoint(t=_ts(2026, 1, 5), equity=Decimal("100000")),
            EquityPoint(t=_ts(2026, 1, 6), equity=Decimal("100000")),
            EquityPoint(t=_ts(2026, 1, 7), equity=Decimal("100000")),
            EquityPoint(t=_ts(2026, 1, 8), equity=Decimal("100000")),
            EquityPoint(t=_ts(2026, 1, 9), equity=Decimal("100000")),
        ]

    def test_single_buy_then_sell_one_closed_trade(self):
        fills = [
            FillRecord(
                symbol="600000.SH",
                side="buy",
                quantity=100,
                price=Decimal("10.00"),
                timestamp=_ts(2026, 1, 5),
                intent_id="i1",
                cycle_run_id="r1",
            ),
            FillRecord(
                symbol="600000.SH",
                side="sell",
                quantity=100,
                price=Decimal("11.00"),
                timestamp=_ts(2026, 1, 9),
                intent_id="i2",
                cycle_run_id="r2",
            ),
        ]
        summary = compute_summary(
            run_id="run_a",
            range_start_utc=_ts(2026, 1, 5),
            range_end_utc=_ts(2026, 1, 9),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100100"),
            final_cash=Decimal("100100"),
            final_positions=(),
            equity_history=self._equity_history_flat(),
            fills=fills,
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
            completed_at=_ts(2026, 1, 9),
        )
        self.assertEqual(summary.trade_count_closed, 1)
        self.assertEqual(summary.trade_count_open, 0)
        self.assertEqual(summary.win_rate, Decimal("1"))

    def test_multi_lot_partial_close_records_two_trades_remaining_open(self):
        fills = [
            FillRecord("600000.SH", "buy", 100, Decimal("10.00"), _ts(2026, 1, 5), "i1", "r1"),
            FillRecord("600000.SH", "buy", 200, Decimal("12.00"), _ts(2026, 1, 6), "i2", "r2"),
            FillRecord("600000.SH", "sell", 150, Decimal("14.00"), _ts(2026, 1, 7), "i3", "r3"),
        ]
        summary = compute_summary(
            run_id="run_b",
            range_start_utc=_ts(2026, 1, 5),
            range_end_utc=_ts(2026, 1, 9),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100000"),
            final_cash=Decimal("100000"),
            final_positions=(),
            equity_history=self._equity_history_flat(),
            fills=fills,
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
            completed_at=_ts(2026, 1, 9),
        )
        # Sell 150 against FIFO lots: consume lot1 (100, $10) fully → trade1 pnl = 100*(14-10) = 400.
        # Then 50 of lot2 (200@12) → trade2 pnl = 50*(14-12) = 100. Both win.
        # 150 remain in lot2 → 1 open trade.
        self.assertEqual(summary.trade_count_closed, 2)
        self.assertEqual(summary.trade_count_open, 1)
        self.assertEqual(summary.win_rate, Decimal("1"))

    def test_short_sell_raises_value_error(self):
        fills = [
            FillRecord("X", "buy", 50, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
            FillRecord("X", "sell", 100, Decimal("11"), _ts(2026, 1, 6), "i2", "r2"),
        ]
        with self.assertRaises(ValueError):
            compute_summary(
                run_id="run_c",
                range_start_utc=_ts(2026, 1, 5),
                range_end_utc=_ts(2026, 1, 9),
                bar_interval="1d",
                starting_equity=Decimal("100000"),
                ending_equity=Decimal("100000"),
                final_cash=Decimal("100000"),
                final_positions=(),
                equity_history=self._equity_history_flat(),
                fills=fills,
                trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
                completed_at=_ts(2026, 1, 9),
            )

    def test_zero_closed_no_open_last_price_win_rate_decimal_zero(self):
        # All fills are buys, but ``final_positions`` is empty so the open lot
        # has no last_price → mark-to-market sample size is 0 and the win_rate
        # falls back to ``Decimal('0')`` with sample_size=0.
        fills = [
            FillRecord("X", "buy", 50, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
        ]
        summary = compute_summary(
            run_id="run_d",
            range_start_utc=_ts(2026, 1, 5),
            range_end_utc=_ts(2026, 1, 9),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100500"),
            final_cash=Decimal("99500"),
            final_positions=(),
            equity_history=self._equity_history_flat(),
            fills=fills,
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
            completed_at=_ts(2026, 1, 9),
        )
        self.assertEqual(summary.trade_count_closed, 0)
        self.assertEqual(summary.trade_count_open, 1)
        self.assertEqual(summary.fills_count, 1)
        self.assertEqual(summary.win_rate, Decimal("0"))
        self.assertEqual(summary.win_rate_sample_size, 0)

    def test_open_lot_marked_to_market_winning_yields_full_win_rate(self):
        # 1 open buy lot at $10, last_price=$15 → mtm pnl > 0 → win_rate=1, sample=1.
        fills = [
            FillRecord("X", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
        ]
        summary = compute_summary(
            run_id="run_mtm_win",
            range_start_utc=_ts(2026, 1, 5),
            range_end_utc=_ts(2026, 1, 9),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100500"),
            final_cash=Decimal("99000"),
            final_positions=(
                FinalPosition(
                    symbol="X",
                    name=None,
                    quantity=100,
                    available=100,
                    cost_price=Decimal("10"),
                    last_price=Decimal("15"),
                    market_value=Decimal("1500"),
                ),
            ),
            equity_history=self._equity_history_flat(),
            fills=fills,
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
            completed_at=_ts(2026, 1, 9),
        )
        self.assertEqual(summary.fills_count, 1)
        self.assertEqual(summary.trade_count_closed, 0)
        self.assertEqual(summary.trade_count_open, 1)
        self.assertEqual(summary.win_rate, Decimal("1"))
        self.assertEqual(summary.win_rate_sample_size, 1)

    def test_open_lot_marked_to_market_losing_yields_zero_win_rate(self):
        fills = [
            FillRecord("X", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
        ]
        summary = compute_summary(
            run_id="run_mtm_loss",
            range_start_utc=_ts(2026, 1, 5),
            range_end_utc=_ts(2026, 1, 9),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("99500"),
            final_cash=Decimal("99000"),
            final_positions=(
                FinalPosition(
                    symbol="X",
                    name=None,
                    quantity=100,
                    available=100,
                    cost_price=Decimal("10"),
                    last_price=Decimal("8"),
                    market_value=Decimal("800"),
                ),
            ),
            equity_history=self._equity_history_flat(),
            fills=fills,
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
            completed_at=_ts(2026, 1, 9),
        )
        self.assertEqual(summary.fills_count, 1)
        self.assertEqual(summary.win_rate, Decimal("0"))
        self.assertEqual(summary.win_rate_sample_size, 1)

    def test_win_rate_combines_closed_loss_and_open_winning_mtm(self):
        # symbol A: buy 100 @ 10, sell 100 @ 9 → closed loss.
        # symbol B: buy 100 @ 10, last_price 15 → open mtm win.
        # win_rate = 1 / 2 = 0.5, sample_size = 2.
        fills = [
            FillRecord("A", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
            FillRecord("A", "sell", 100, Decimal("9"), _ts(2026, 1, 6), "i2", "r2"),
            FillRecord("B", "buy", 100, Decimal("10"), _ts(2026, 1, 7), "i3", "r3"),
        ]
        summary = compute_summary(
            run_id="run_mtm_combo",
            range_start_utc=_ts(2026, 1, 5),
            range_end_utc=_ts(2026, 1, 9),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100400"),
            final_cash=Decimal("99500"),
            final_positions=(
                FinalPosition(
                    symbol="B",
                    name=None,
                    quantity=100,
                    available=100,
                    cost_price=Decimal("10"),
                    last_price=Decimal("15"),
                    market_value=Decimal("1500"),
                ),
            ),
            equity_history=self._equity_history_flat(),
            fills=fills,
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
            completed_at=_ts(2026, 1, 9),
        )
        self.assertEqual(summary.fills_count, 3)
        self.assertEqual(summary.trade_count_closed, 1)
        self.assertEqual(summary.trade_count_open, 1)
        self.assertEqual(summary.win_rate, Decimal("0.5"))
        self.assertEqual(summary.win_rate_sample_size, 2)

    def test_open_lots_without_last_price_excluded_from_win_rate_but_included_in_holding(self):
        # Open lot exists but final_positions has no last_price → not in win_rate
        # sample, but holding period still counts.
        fills = [
            FillRecord("X", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
        ]
        summary = compute_summary(
            run_id="run_no_lp",
            range_start_utc=_ts(2026, 1, 5),
            range_end_utc=_ts(2026, 1, 9),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("99000"),
            final_cash=Decimal("99000"),
            final_positions=(
                FinalPosition(
                    symbol="X",
                    name=None,
                    quantity=100,
                    available=100,
                    cost_price=Decimal("10"),
                    last_price=None,
                    market_value=None,
                ),
            ),
            equity_history=self._equity_history_flat(),
            fills=fills,
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
            completed_at=_ts(2026, 1, 9),
        )
        self.assertEqual(summary.win_rate_sample_size, 0)
        # Holding from 2026-01-05 to range_end 2026-01-09 = 4 trading-day index gap.
        self.assertEqual(summary.avg_holding_trading_days, Decimal("4"))
        self.assertEqual(summary.avg_holding_sample_size, 1)

    def test_avg_holding_trading_days_uses_provided_calendar(self):
        # 2026-01-05 .. 2026-01-09 are 5 consecutive trading days.
        # Buy on 2026-01-05, sell on 2026-01-08 → 3 trading days.
        fills = [
            FillRecord("X", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
            FillRecord("X", "sell", 100, Decimal("12"), _ts(2026, 1, 8), "i2", "r2"),
        ]
        summary = compute_summary(
            run_id="run_e",
            range_start_utc=_ts(2026, 1, 5),
            range_end_utc=_ts(2026, 1, 9),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100200"),
            final_cash=Decimal("100200"),
            final_positions=(),
            equity_history=self._equity_history_flat(),
            fills=fills,
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
            completed_at=_ts(2026, 1, 9),
        )
        self.assertEqual(summary.avg_holding_trading_days, Decimal("3"))

    def test_avg_holding_falls_back_to_natural_days_when_calendar_empty(self):
        fills = [
            FillRecord("X", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
            FillRecord("X", "sell", 100, Decimal("12"), _ts(2026, 1, 9), "i2", "r2"),
        ]
        summary = compute_summary(
            run_id="run_f",
            range_start_utc=_ts(2026, 1, 5),
            range_end_utc=_ts(2026, 1, 9),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100200"),
            final_cash=Decimal("100200"),
            final_positions=(),
            equity_history=self._equity_history_flat(),
            fills=fills,
            trading_dates=(),
            completed_at=_ts(2026, 1, 9),
        )
        self.assertEqual(summary.avg_holding_trading_days, Decimal("4"))


class ComputeSummaryDrawdownTests(unittest.TestCase):
    """Max drawdown + equity curve coupling."""

    def _basic_kwargs(self):
        return dict(
            run_id="run_dd",
            range_start_utc=_ts(2026, 1, 1),
            range_end_utc=_ts(2026, 1, 31),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100000"),
            final_cash=Decimal("100000"),
            final_positions=(),
            fills=(),
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 1), 31),
            completed_at=_ts(2026, 1, 31),
        )

    def test_monotonically_increasing_series_has_zero_drawdown(self):
        eq = [
            EquityPoint(t=_ts(2026, 1, 1), equity=Decimal("100000")),
            EquityPoint(t=_ts(2026, 1, 2), equity=Decimal("100100")),
            EquityPoint(t=_ts(2026, 1, 3), equity=Decimal("100200")),
        ]
        summary = compute_summary(**self._basic_kwargs(), equity_history=eq)
        self.assertEqual(summary.max_drawdown_pct, Decimal("0"))
        self.assertIsNone(summary.max_drawdown_peak_at)
        self.assertIsNone(summary.max_drawdown_trough_at)

    def test_peak_then_trough_records_correct_window(self):
        eq = [
            EquityPoint(t=_ts(2026, 1, 1), equity=Decimal("100000")),
            EquityPoint(t=_ts(2026, 1, 2), equity=Decimal("110000")),  # peak
            EquityPoint(t=_ts(2026, 1, 3), equity=Decimal("105000")),
            EquityPoint(t=_ts(2026, 1, 4), equity=Decimal("99000")),   # trough; dd = 11000/110000 = 10%
            EquityPoint(t=_ts(2026, 1, 5), equity=Decimal("104000")),
        ]
        summary = compute_summary(**self._basic_kwargs(), equity_history=eq)
        self.assertEqual(summary.max_drawdown_pct, Decimal("10"))
        self.assertEqual(summary.max_drawdown_peak_at, _ts(2026, 1, 2))
        self.assertEqual(summary.max_drawdown_trough_at, _ts(2026, 1, 4))
        self.assertEqual(summary.max_drawdown_peak_equity, Decimal("110000"))
        self.assertEqual(summary.max_drawdown_trough_equity, Decimal("99000"))

    def test_tie_breaking_uses_earliest_peak_for_determinism(self):
        # Two equal-percent drawdowns; earliest peak should win.
        eq = [
            EquityPoint(t=_ts(2026, 1, 1), equity=Decimal("100")),
            EquityPoint(t=_ts(2026, 1, 2), equity=Decimal("90")),  # 10% from peak1
            EquityPoint(t=_ts(2026, 1, 3), equity=Decimal("100")),
            EquityPoint(t=_ts(2026, 1, 4), equity=Decimal("90")),  # 10% from peak2
        ]
        summary = compute_summary(**self._basic_kwargs(), equity_history=eq)
        self.assertEqual(summary.max_drawdown_pct, Decimal("10"))
        self.assertEqual(summary.max_drawdown_peak_at, _ts(2026, 1, 1))
        self.assertEqual(summary.max_drawdown_trough_at, _ts(2026, 1, 2))

    def test_empty_series_returns_zero_drawdown(self):
        summary = compute_summary(**self._basic_kwargs(), equity_history=())
        self.assertEqual(summary.max_drawdown_pct, Decimal("0"))
        self.assertIsNone(summary.max_drawdown_peak_at)
        self.assertIsNone(summary.max_drawdown_trough_at)

    def test_equity_curve_below_threshold_emits_identity(self):
        eq = [EquityPoint(t=_ts(2026, 1, 1), equity=Decimal(100 + i)) for i in range(50)]
        summary = compute_summary(
            **self._basic_kwargs(),
            equity_history=eq,
            equity_curve_max_points=100,
        )
        self.assertEqual(len(summary.equity_curve), 50)
        self.assertFalse(summary.equity_curve_meta_downsampled)
        self.assertEqual(summary.equity_curve_meta_raw_length, 50)

    def test_equity_curve_above_threshold_downsamples_and_keeps_endpoints(self):
        eq = [
            EquityPoint(t=datetime(2026, 1, 1, tzinfo=timezone.utc).replace(microsecond=i), equity=Decimal(100 + i))
            for i in range(1000)
        ]
        summary = compute_summary(
            **self._basic_kwargs(),
            equity_history=eq,
            equity_curve_max_points=100,
        )
        self.assertTrue(summary.equity_curve_meta_downsampled)
        self.assertEqual(summary.equity_curve_meta_raw_length, 1000)
        self.assertLessEqual(len(summary.equity_curve), 100)
        # First / last points always retained for chart fidelity.
        self.assertEqual(summary.equity_curve[0].equity, Decimal("100"))
        self.assertEqual(summary.equity_curve[-1].equity, Decimal("1099"))


class SummaryToJsonShapeTests(unittest.TestCase):
    """Serialization contract: every money/percent is decimal-string; ts is ISO."""

    def test_summary_to_json_serializes_decimals_as_strings(self):
        eq = [
            EquityPoint(t=_ts(2026, 1, 1), equity=Decimal("100000.00")),
            EquityPoint(t=_ts(2026, 1, 2), equity=Decimal("100150.50")),
        ]
        summary = compute_summary(
            run_id="run_x",
            range_start_utc=_ts(2026, 1, 1),
            range_end_utc=_ts(2026, 1, 2),
            bar_interval="1d",
            starting_equity=Decimal("100000.00"),
            ending_equity=Decimal("100150.50"),
            final_cash=Decimal("100150.50"),
            final_positions=(),
            equity_history=eq,
            fills=(),
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 1), 2),
            completed_at=_ts(2026, 1, 2),
        )
        out = summary_to_json(summary)

        # Money values are strings.
        for key in (
            "starting_equity",
            "ending_equity",
            "final_cash",
            "final_market_value",
            "max_drawdown_peak_equity",
            "max_drawdown_trough_equity",
        ):
            self.assertIsInstance(out[key], str, f"{key} must be str")

        # Percent / ratio values are strings.
        for key in (
            "return_pct",
            "win_rate",
            "avg_holding_trading_days",
            "max_drawdown_pct",
        ):
            self.assertIsInstance(out[key], str, f"{key} must be str")

        # New diagnostic counts surfaced for UI tooltips / debug.
        self.assertIsInstance(out["fills_count"], int)
        self.assertIsInstance(out["win_rate_sample_size"], int)
        self.assertIsInstance(out["avg_holding_sample_size"], int)

        # Times are ISO-8601 with 'Z' suffix.
        self.assertTrue(out["completed_at"].endswith("Z"))
        self.assertTrue(out["range_start_utc"].endswith("Z"))
        self.assertTrue(out["range_end_utc"].endswith("Z"))

        # Equity curve entries are decimal strings; meta is structured dict.
        self.assertEqual(out["equity_curve_meta"], {"downsampled": False, "raw_length": 2})
        for pt in out["equity_curve"]:
            self.assertIsInstance(pt["t"], str)
            self.assertIsInstance(pt["equity"], str)

        self.assertEqual(out["schema_version"], 1)
        self.assertEqual(out["run_id"], "run_x")
        # Sanity: return_pct = (100150.50 - 100000) / 100000 * 100 = 0.1505
        self.assertEqual(out["return_pct"], "0.1505")

    def test_zero_starting_equity_yields_zero_return_pct(self):
        summary = compute_summary(
            run_id="run_zero",
            range_start_utc=_ts(2026, 1, 1),
            range_end_utc=_ts(2026, 1, 2),
            bar_interval="1d",
            starting_equity=Decimal("0"),
            ending_equity=Decimal("100"),
            final_cash=Decimal("100"),
            final_positions=(),
            equity_history=(EquityPoint(t=_ts(2026, 1, 1), equity=Decimal("0")),),
            fills=(),
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 1), 2),
            completed_at=_ts(2026, 1, 2),
        )
        self.assertEqual(summary.return_pct, Decimal("0"))


class FinalPositionSerializationTests(unittest.TestCase):
    def test_final_positions_round_trip(self):
        pos = FinalPosition(
            symbol="600000.SH",
            name=None,
            quantity=1000,
            available=1000,
            cost_price=Decimal("10.0000"),
            last_price=Decimal("11.00"),
            market_value=Decimal("11000.00"),
        )
        summary = compute_summary(
            run_id="run_pos",
            range_start_utc=_ts(2026, 1, 1),
            range_end_utc=_ts(2026, 1, 2),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("111000"),
            final_cash=Decimal("100000"),
            final_positions=(pos,),
            equity_history=(EquityPoint(t=_ts(2026, 1, 1), equity=Decimal("100000")),),
            fills=(),
            trading_dates=_consecutive_iso_dates(_ts(2026, 1, 1), 2),
            completed_at=_ts(2026, 1, 2),
        )
        out = summary_to_json(summary)
        self.assertEqual(len(out["final_positions"]), 1)
        first = out["final_positions"][0]
        self.assertEqual(first["symbol"], "600000.SH")
        self.assertEqual(first["quantity"], 1000)
        self.assertEqual(first["cost_price"], "10")
        self.assertEqual(first["last_price"], "11")
        self.assertEqual(first["market_value"], "11000")
        # weight_pct = market_value / ending_equity * 100 = 11000 / 111000 * 100.
        self.assertEqual(
            Decimal(first["weight_pct"]),
            (Decimal("11000") / Decimal("111000") * Decimal(100)).quantize(Decimal("0.0001")),
        )
        # final_market_value must reflect the sum of position market values.
        self.assertEqual(out["final_market_value"], "11000")


class ByExitReasonTests(unittest.TestCase):
    """``by_exit_reason`` groups closed round-trips by the closing sell's reason."""

    def _equity(self) -> list[EquityPoint]:
        return [
            EquityPoint(t=_ts(2026, 1, 5), equity=Decimal("100000")),
            EquityPoint(t=_ts(2026, 1, 9), equity=Decimal("100000")),
        ]

    def _summary(self, fills: list[FillRecord]) -> dict:
        return summary_to_json(
            compute_summary(
                run_id="run_er",
                range_start_utc=_ts(2026, 1, 5),
                range_end_utc=_ts(2026, 1, 9),
                bar_interval="1d",
                starting_equity=Decimal("100000"),
                ending_equity=Decimal("100000"),
                final_cash=Decimal("100000"),
                final_positions=(),
                equity_history=self._equity(),
                fills=fills,
                trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
                completed_at=_ts(2026, 1, 9),
            )
        )

    def test_empty_when_no_reason_categorized(self):
        fills = [
            FillRecord("X", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
            FillRecord("X", "sell", 100, Decimal("11"), _ts(2026, 1, 6), "i2", "r2"),
        ]
        out = self._summary(fills)
        # Key always present (additive), but empty — no synthetic bucket.
        self.assertEqual(out["by_exit_reason"], [])

    def test_groups_closed_trades_by_exit_reason(self):
        fills = [
            FillRecord("A", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
            FillRecord(
                "A", "sell", 100, Decimal("12"), _ts(2026, 1, 6), "i2", "r2",
                exit_reason="take_profit",
            ),
            FillRecord("B", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i3", "r3"),
            FillRecord(
                "B", "sell", 100, Decimal("9"), _ts(2026, 1, 6), "i4", "r4",
                exit_reason="stop_loss",
            ),
        ]
        out = self._summary(fills)
        by_reason = {e["exit_reason"]: e for e in out["by_exit_reason"]}
        self.assertEqual(set(by_reason), {"take_profit", "stop_loss"})
        self.assertEqual(by_reason["take_profit"]["trade_count_closed"], 1)
        self.assertEqual(by_reason["take_profit"]["pnl"], "200")
        self.assertEqual(by_reason["take_profit"]["win_rate"], "1")
        self.assertEqual(by_reason["stop_loss"]["pnl"], "-100")
        self.assertEqual(by_reason["stop_loss"]["win_rate"], "0")
        # Sorted by descending |pnl|: take_profit (200) before stop_loss (100).
        self.assertEqual(out["by_exit_reason"][0]["exit_reason"], "take_profit")

    def test_partial_close_shares_reason_across_lots(self):
        # One reasoned sell closing two buy lots → two closed trades, same reason.
        fills = [
            FillRecord("X", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
            FillRecord("X", "buy", 100, Decimal("11"), _ts(2026, 1, 6), "i2", "r2"),
            FillRecord(
                "X", "sell", 200, Decimal("12"), _ts(2026, 1, 7), "i3", "r3",
                exit_reason="roi",
            ),
        ]
        out = self._summary(fills)
        by_reason = {e["exit_reason"]: e for e in out["by_exit_reason"]}
        self.assertEqual(by_reason["roi"]["trade_count_closed"], 2)


class ByTagTests(unittest.TestCase):
    """``by_tag`` groups closed round-trips by the entry lot's factor tag."""

    def _equity(self) -> list[EquityPoint]:
        return [
            EquityPoint(t=_ts(2026, 1, 5), equity=Decimal("100000")),
            EquityPoint(t=_ts(2026, 1, 9), equity=Decimal("100000")),
        ]

    def _summary(self, fills: list[FillRecord]) -> dict:
        return summary_to_json(
            compute_summary(
                run_id="run_tag",
                range_start_utc=_ts(2026, 1, 5),
                range_end_utc=_ts(2026, 1, 9),
                bar_interval="1d",
                starting_equity=Decimal("100000"),
                ending_equity=Decimal("100000"),
                final_cash=Decimal("100000"),
                final_positions=(),
                equity_history=self._equity(),
                fills=fills,
                trading_dates=_consecutive_iso_dates(_ts(2026, 1, 5), 5),
                completed_at=_ts(2026, 1, 9),
            )
        )

    def test_empty_when_no_entry_tag(self):
        fills = [
            FillRecord("X", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1"),
            FillRecord("X", "sell", 100, Decimal("11"), _ts(2026, 1, 6), "i2", "r2"),
        ]
        self.assertEqual(self._summary(fills)["by_tag"], [])

    def test_groups_closed_trades_by_entry_tag(self):
        fills = [
            FillRecord(
                "A", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i1", "r1",
                entry_tag="ma_cross",
            ),
            FillRecord("A", "sell", 100, Decimal("12"), _ts(2026, 1, 6), "i2", "r2"),
            FillRecord(
                "B", "buy", 100, Decimal("10"), _ts(2026, 1, 5), "i3", "r3",
                entry_tag="rsi_dip",
            ),
            FillRecord("B", "sell", 100, Decimal("9"), _ts(2026, 1, 6), "i4", "r4"),
        ]
        out = self._summary(fills)
        by_tag = {e["tag"]: e for e in out["by_tag"]}
        self.assertEqual(set(by_tag), {"ma_cross", "rsi_dip"})
        self.assertEqual(by_tag["ma_cross"]["pnl"], "200")
        self.assertEqual(by_tag["ma_cross"]["win_rate"], "1")
        self.assertEqual(by_tag["rsi_dip"]["pnl"], "-100")
        # Sorted by descending |pnl|.
        self.assertEqual(out["by_tag"][0]["tag"], "ma_cross")

    def test_entry_tag_rides_lot_to_closed_trade(self):
        # One tagged buy lot, partial close in two sells → both round-trips
        # attributed to the entry tag.
        fills = [
            FillRecord(
                "X", "buy", 200, Decimal("10"), _ts(2026, 1, 5), "i1", "r1",
                entry_tag="breakout",
            ),
            FillRecord("X", "sell", 100, Decimal("11"), _ts(2026, 1, 6), "i2", "r2"),
            FillRecord("X", "sell", 100, Decimal("12"), _ts(2026, 1, 7), "i3", "r3"),
        ]
        out = self._summary(fills)
        by_tag = {e["tag"]: e for e in out["by_tag"]}
        self.assertEqual(by_tag["breakout"]["trade_count_closed"], 2)


if __name__ == "__main__":
    unittest.main()
