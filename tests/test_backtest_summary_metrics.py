"""Unit tests for the extended backtest summary metrics.

Covers fields added on top of the original ``BacktestSummary`` schema:

- Risk-adjusted return: ``annual_return_pct`` / ``volatility_annual_pct`` /
  ``sharpe`` / ``sortino`` / ``calmar``.
- Closed-trade aggregates: ``profit_factor`` / ``avg_win_pnl`` /
  ``avg_loss_pnl`` / ``profit_loss_ratio`` / ``max_consecutive_losses``.
- ``by_symbol`` breakdown shape and ordering.

Like the original suite these treat ``compute_summary`` / ``summary_to_json``
as pure functions — no DB, no network. Determinism comes from feeding fully
specified accumulators and asserting on the resulting dataclass / JSON.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from doyoutrade.backtest.summary import (
    BacktestSummary,
    EquityPoint,
    FillRecord,
    compute_summary,
    render_summary_markdown,
    summary_to_json,
)


def _ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _flat_calendar(start: datetime, days: int) -> list[str]:
    base = start.date()
    return [base.fromordinal(base.toordinal() + i).isoformat() for i in range(days)]


def _linear_equity(
    start: datetime, days: int, start_eq: Decimal, end_eq: Decimal
) -> list[EquityPoint]:
    """Equity points one per day from ``start`` for ``days`` bars, linearly
    interpolated. Used by risk-metric tests so the math is fully predictable.
    """

    if days < 2:
        return [EquityPoint(t=start, equity=start_eq)]
    step = (end_eq - start_eq) / Decimal(days - 1)
    out: list[EquityPoint] = []
    for i in range(days):
        out.append(
            EquityPoint(
                t=start + timedelta(days=i),
                equity=start_eq + step * Decimal(i),
            )
        )
    return out


class RiskMetricsTests(unittest.TestCase):
    """Annualized return / volatility / Sharpe / Sortino / Calmar."""

    def test_flat_equity_yields_zero_volatility_and_none_sharpe(self):
        # 5 bars, all at 100k. No returns, no risk, no Sharpe.
        days = 5
        eq = _linear_equity(_ts(2026, 1, 5), days, Decimal("100000"), Decimal("100000"))
        summary = compute_summary(
            run_id="r",
            range_start_utc=eq[0].t,
            range_end_utc=eq[-1].t,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100000"),
            final_cash=Decimal("100000"),
            final_positions=(),
            equity_history=eq,
            fills=(),
            trading_dates=_flat_calendar(_ts(2026, 1, 5), days),
            completed_at=eq[-1].t,
        )
        self.assertIsNotNone(summary.annual_return_pct)
        self.assertEqual(summary.annual_return_pct, Decimal("0.000000"))
        self.assertEqual(summary.volatility_annual_pct, Decimal("0.000000"))
        # Flat returns → stdev=0 → Sharpe / Sortino undefined.
        self.assertIsNone(summary.sharpe)
        self.assertIsNone(summary.sortino)
        # Zero drawdown → Calmar undefined.
        self.assertIsNone(summary.calmar)

    def test_single_bar_equity_history_returns_no_risk_metrics(self):
        eq = [EquityPoint(t=_ts(2026, 1, 5), equity=Decimal("100000"))]
        summary = compute_summary(
            run_id="r",
            range_start_utc=_ts(2026, 1, 5),
            range_end_utc=_ts(2026, 1, 5),
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100000"),
            final_cash=Decimal("100000"),
            final_positions=(),
            equity_history=eq,
            fills=(),
            trading_dates=["2026-01-05"],
            completed_at=_ts(2026, 1, 5),
        )
        self.assertIsNone(summary.annual_return_pct)
        self.assertIsNone(summary.sharpe)
        self.assertIsNone(summary.calmar)

    def test_one_year_doubling_yields_100pct_cagr(self):
        # Exactly 365.25 days from start to end, equity doubles.
        start = _ts(2026, 1, 1)
        end_dt = start + timedelta(days=int(round(365.25)))
        eq = [
            EquityPoint(t=start, equity=Decimal("100000")),
            EquityPoint(t=end_dt, equity=Decimal("200000")),
        ]
        summary = compute_summary(
            run_id="r",
            range_start_utc=start,
            range_end_utc=end_dt,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("200000"),
            final_cash=Decimal("200000"),
            final_positions=(),
            equity_history=eq,
            fills=(),
            trading_dates=[],
            completed_at=end_dt,
        )
        annual = summary.annual_return_pct
        self.assertIsNotNone(annual)
        assert annual is not None  # narrow for type checker
        # 365 vs 365.25 → CAGR ~ 99.95%. Allow tight tolerance.
        self.assertAlmostEqual(float(annual), 100.0, delta=0.1)
        self.assertIsNone(summary.calmar)  # no MDD

    def test_drawdown_and_recovery_yield_finite_calmar(self):
        # Up to 110k then down to 90k then back to 105k over 1y.
        start = _ts(2026, 1, 1)
        eq = [
            EquityPoint(t=start + timedelta(days=0), equity=Decimal("100000")),
            EquityPoint(t=start + timedelta(days=120), equity=Decimal("110000")),
            EquityPoint(t=start + timedelta(days=240), equity=Decimal("90000")),
            EquityPoint(t=start + timedelta(days=365), equity=Decimal("105000")),
        ]
        summary = compute_summary(
            run_id="r",
            range_start_utc=eq[0].t,
            range_end_utc=eq[-1].t,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("105000"),
            final_cash=Decimal("105000"),
            final_positions=(),
            equity_history=eq,
            fills=(),
            trading_dates=[],
            completed_at=eq[-1].t,
        )
        # MDD = (110000 - 90000) / 110000 = 18.18...%
        self.assertGreater(summary.max_drawdown_pct, Decimal("18"))
        self.assertLess(summary.max_drawdown_pct, Decimal("19"))
        self.assertIsNotNone(summary.calmar)
        self.assertIsNotNone(summary.annual_return_pct)
        # Volatility must be positive given the path.
        vol = summary.volatility_annual_pct
        self.assertIsNotNone(vol)
        assert vol is not None  # narrow for type checker
        self.assertGreater(vol, Decimal("0"))

    def test_sortino_uses_only_downside_dispersion(self):
        # Alternating +5%/-1% on a daily grid → positive mean, asymmetric stdev.
        # Sortino > Sharpe because upside dispersion is excluded.
        start = _ts(2026, 1, 1)
        equity = Decimal("100000")
        eq = [EquityPoint(t=start, equity=equity)]
        for i in range(1, 20):
            equity = equity * (Decimal("1.05") if i % 2 == 1 else Decimal("0.99"))
            eq.append(EquityPoint(t=start + timedelta(days=i), equity=equity))
        summary = compute_summary(
            run_id="r",
            range_start_utc=eq[0].t,
            range_end_utc=eq[-1].t,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=equity,
            final_cash=equity,
            final_positions=(),
            equity_history=eq,
            fills=(),
            trading_dates=[],
            completed_at=eq[-1].t,
        )
        sharpe = summary.sharpe
        sortino = summary.sortino
        self.assertIsNotNone(sharpe)
        self.assertIsNotNone(sortino)
        assert sharpe is not None and sortino is not None  # narrow for type checker
        self.assertGreater(sortino, sharpe)


class TradeAggregateTests(unittest.TestCase):
    """profit_factor / avg win / avg loss / max consecutive losses."""

    def _make_summary(self, fills: list[FillRecord]) -> BacktestSummary:
        eq = _linear_equity(
            _ts(2026, 1, 1), 10, Decimal("100000"), Decimal("100000")
        )
        return compute_summary(
            run_id="r",
            range_start_utc=eq[0].t,
            range_end_utc=eq[-1].t,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100000"),
            final_cash=Decimal("100000"),
            final_positions=(),
            equity_history=eq,
            fills=fills,
            trading_dates=_flat_calendar(_ts(2026, 1, 1), 10),
            completed_at=eq[-1].t,
        )

    def test_no_closed_trades_yields_none_aggregates(self):
        summary = self._make_summary([])
        self.assertIsNone(summary.profit_factor)
        self.assertIsNone(summary.avg_win_pnl)
        self.assertIsNone(summary.avg_loss_pnl)
        self.assertIsNone(summary.profit_loss_ratio)
        self.assertEqual(summary.max_consecutive_losses, 0)

    def test_single_winning_trade_no_profit_factor_but_avg_win_set(self):
        fills = [
            FillRecord("AAA", "buy", 100, Decimal("10"), _ts(2026, 1, 2), None, "r1"),
            FillRecord("AAA", "sell", 100, Decimal("12"), _ts(2026, 1, 3), None, "r2"),
        ]
        summary = self._make_summary(fills)
        # No losing trades → profit_factor is mathematically infinite → None.
        self.assertIsNone(summary.profit_factor)
        self.assertEqual(summary.avg_win_pnl, Decimal("200.0000"))
        self.assertIsNone(summary.avg_loss_pnl)
        self.assertIsNone(summary.profit_loss_ratio)
        self.assertEqual(summary.max_consecutive_losses, 0)

    def test_mixed_trades_compute_profit_factor_and_ratio(self):
        # AAA: +200 win. BBB: -100 loss. → profit_factor = 2, ratio = 2.
        fills = [
            FillRecord("AAA", "buy", 100, Decimal("10"), _ts(2026, 1, 2), None, "r1"),
            FillRecord("AAA", "sell", 100, Decimal("12"), _ts(2026, 1, 3), None, "r2"),
            FillRecord("BBB", "buy", 100, Decimal("20"), _ts(2026, 1, 4), None, "r3"),
            FillRecord("BBB", "sell", 100, Decimal("19"), _ts(2026, 1, 5), None, "r4"),
        ]
        summary = self._make_summary(fills)
        self.assertEqual(summary.avg_win_pnl, Decimal("200.0000"))
        self.assertEqual(summary.avg_loss_pnl, Decimal("-100.0000"))
        self.assertEqual(summary.profit_factor, Decimal("2.000000"))
        self.assertEqual(summary.profit_loss_ratio, Decimal("2.000000"))

    def test_max_consecutive_losses_walks_timeline_across_symbols(self):
        # Timeline (by exit_time): loss, loss, win, loss, loss, loss, win → max = 3.
        fills = []
        plan = [
            ("AAA", Decimal("10"), Decimal("9"), 2, 3),    # loss
            ("BBB", Decimal("20"), Decimal("19"), 3, 4),   # loss
            ("CCC", Decimal("30"), Decimal("31"), 4, 5),   # win
            ("DDD", Decimal("40"), Decimal("39"), 5, 6),   # loss
            ("EEE", Decimal("50"), Decimal("49"), 6, 7),   # loss
            ("FFF", Decimal("60"), Decimal("59"), 7, 8),   # loss
            ("GGG", Decimal("70"), Decimal("71"), 8, 9),   # win
        ]
        for i, (sym, buy_px, sell_px, buy_day, sell_day) in enumerate(plan):
            fills.append(
                FillRecord(sym, "buy", 100, buy_px, _ts(2026, 1, buy_day), None, f"r{i}b")
            )
            fills.append(
                FillRecord(
                    sym, "sell", 100, sell_px, _ts(2026, 1, sell_day), None, f"r{i}s"
                )
            )
        summary = self._make_summary(fills)
        self.assertEqual(summary.max_consecutive_losses, 3)


class BySymbolTests(unittest.TestCase):
    """``by_symbol`` shape and ordering."""

    def test_empty_when_no_closed_trades(self):
        eq = _linear_equity(_ts(2026, 1, 1), 5, Decimal("100000"), Decimal("100000"))
        summary = compute_summary(
            run_id="r",
            range_start_utc=eq[0].t,
            range_end_utc=eq[-1].t,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100000"),
            final_cash=Decimal("100000"),
            final_positions=(),
            equity_history=eq,
            fills=(),
            trading_dates=_flat_calendar(_ts(2026, 1, 1), 5),
            completed_at=eq[-1].t,
        )
        self.assertEqual(summary.by_symbol, ())

    def test_ordered_by_absolute_pnl_descending(self):
        # AAA: +500 win, BBB: -1000 loss, CCC: +50 win
        # Order: BBB (1000), AAA (500), CCC (50).
        fills = [
            FillRecord("AAA", "buy", 100, Decimal("10"), _ts(2026, 1, 2), None, "r1"),
            FillRecord("AAA", "sell", 100, Decimal("15"), _ts(2026, 1, 3), None, "r2"),
            FillRecord("BBB", "buy", 100, Decimal("20"), _ts(2026, 1, 2), None, "r3"),
            FillRecord("BBB", "sell", 100, Decimal("10"), _ts(2026, 1, 3), None, "r4"),
            FillRecord("CCC", "buy", 100, Decimal("30"), _ts(2026, 1, 2), None, "r5"),
            FillRecord("CCC", "sell", 100, Decimal("30.50"), _ts(2026, 1, 3), None, "r6"),
        ]
        eq = _linear_equity(_ts(2026, 1, 1), 5, Decimal("100000"), Decimal("100000"))
        summary = compute_summary(
            run_id="r",
            range_start_utc=eq[0].t,
            range_end_utc=eq[-1].t,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100000"),
            final_cash=Decimal("100000"),
            final_positions=(),
            equity_history=eq,
            fills=fills,
            trading_dates=_flat_calendar(_ts(2026, 1, 1), 5),
            completed_at=eq[-1].t,
        )
        symbols = [s.symbol for s in summary.by_symbol]
        self.assertEqual(symbols, ["BBB", "AAA", "CCC"])
        self.assertEqual(summary.by_symbol[0].pnl, Decimal("-1000"))
        self.assertEqual(summary.by_symbol[0].win_rate, Decimal("0"))
        self.assertEqual(summary.by_symbol[1].pnl, Decimal("500"))
        self.assertEqual(summary.by_symbol[1].win_rate, Decimal("1"))


class JsonSerializationTests(unittest.TestCase):
    """JSON wire shape: new fields appear, ``None`` becomes JSON ``null``."""

    def test_new_fields_present_and_decimal_or_null(self):
        eq = _linear_equity(_ts(2026, 1, 1), 5, Decimal("100000"), Decimal("100000"))
        summary = compute_summary(
            run_id="r",
            range_start_utc=eq[0].t,
            range_end_utc=eq[-1].t,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100000"),
            final_cash=Decimal("100000"),
            final_positions=(),
            equity_history=eq,
            fills=(),
            trading_dates=_flat_calendar(_ts(2026, 1, 1), 5),
            completed_at=eq[-1].t,
        )
        out = summary_to_json(summary)
        for key in (
            "annual_return_pct",
            "volatility_annual_pct",
            "sharpe",
            "sortino",
            "calmar",
            "profit_factor",
            "avg_win_pnl",
            "avg_loss_pnl",
            "profit_loss_ratio",
            "max_consecutive_losses",
            "by_symbol",
        ):
            self.assertIn(key, out)
        # Flat path → all risk ratios undefined → JSON null.
        self.assertIsNone(out["sharpe"])
        self.assertIsNone(out["sortino"])
        self.assertIsNone(out["calmar"])
        self.assertIsNone(out["profit_factor"])
        self.assertEqual(out["max_consecutive_losses"], 0)
        self.assertEqual(out["by_symbol"], [])

    def test_by_symbol_serialized_with_string_decimals(self):
        fills = [
            FillRecord("AAA", "buy", 100, Decimal("10"), _ts(2026, 1, 2), None, "r1"),
            FillRecord("AAA", "sell", 100, Decimal("12"), _ts(2026, 1, 3), None, "r2"),
        ]
        eq = _linear_equity(_ts(2026, 1, 1), 5, Decimal("100000"), Decimal("100000"))
        summary = compute_summary(
            run_id="r",
            range_start_utc=eq[0].t,
            range_end_utc=eq[-1].t,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100200"),
            final_cash=Decimal("100200"),
            final_positions=(),
            equity_history=eq,
            fills=fills,
            trading_dates=_flat_calendar(_ts(2026, 1, 1), 5),
            completed_at=eq[-1].t,
        )
        out = summary_to_json(summary)
        self.assertEqual(len(out["by_symbol"]), 1)
        row = out["by_symbol"][0]
        self.assertEqual(row["symbol"], "AAA")
        self.assertEqual(row["trade_count_closed"], 1)
        self.assertIsInstance(row["pnl"], str)
        self.assertEqual(row["pnl"], "200")
        self.assertEqual(row["win_rate"], "1")


_SAMPLE_SUMMARY = {
    "schema_version": 1,
    "run_id": "run-final-cycle-1",
    "backtest_job_id": "btjob-render-1",
    "completed_at": "2026-05-15T08:00:00Z",
    "range_start_utc": "2026-04-01T00:00:00Z",
    "range_end_utc": "2026-05-15T00:00:00Z",
    "bar_interval": "1d",
    "data_provider": "auto",
    "data_provider_effective": "akshare",
    "starting_equity": "100000",
    "ending_equity": "108000",
    "return_pct": "8.00",
    "annual_return_pct": "120.50",
    "final_cash": "20000",
    "final_market_value": "88000",
    "sharpe": "1.42",
    "sortino": "2.10",
    "calmar": "47.52",
    "volatility_annual_pct": "12.87",
    "max_drawdown_pct": "2.53",
    "max_drawdown_peak_equity": "110000",
    "max_drawdown_trough_equity": "107200",
    "max_drawdown_peak_at": "2026-05-01T00:00:00Z",
    "max_drawdown_trough_at": "2026-05-04T00:00:00Z",
    "fills_count": 4,
    "trade_count_closed": 2,
    "trade_count_open": 0,
    "win_rate": "0.50",
    "win_rate_sample_size": 2,
    "avg_holding_trading_days": "12.5",
    "avg_holding_sample_size": 2,
    "profit_factor": "1.85",
    "avg_win_pnl": "1500",
    "avg_loss_pnl": "-810",
    "profit_loss_ratio": "1.85",
    "max_consecutive_losses": 1,
    "by_symbol": [
        {
            "symbol": "600000.SH",
            "trade_count_closed": 1,
            "pnl": "1500",
            "win_rate": "1",
            "win_rate_sample_size": 1,
            "avg_holding_trading_days": "10",
        },
        {
            "symbol": "600001.SH",
            "trade_count_closed": 1,
            "pnl": "-810",
            "win_rate": "0",
            "win_rate_sample_size": 1,
            "avg_holding_trading_days": "15",
        },
    ],
    "final_positions": [],
    "equity_curve_meta": {"downsampled": False, "raw_length": 30},
    "equity_curve": [],
}


class RenderSummaryMarkdownTests(unittest.TestCase):
    """Pure-function rendering of the persisted summary into markdown."""

    def test_happy_path_contains_all_section_headers(self):
        text = render_summary_markdown(_SAMPLE_SUMMARY)

        self.assertIn("## 回测报告 · `btjob-render-1`", text)
        self.assertIn("最终 cycle run：`run-final-cycle-1`", text)
        self.assertIn("数据源：`auto` → `akshare`", text)
        self.assertIn("### 概览", text)
        self.assertIn("### 风险调整", text)
        self.assertIn("### 交易统计", text)
        self.assertIn("### 按标的拆解", text)
        # No anomalies / final positions in the sample.
        self.assertNotIn("### 仍持仓", text)
        self.assertNotIn("### 异常信号", text)

    def test_percent_fields_get_percent_suffix(self):
        text = render_summary_markdown(_SAMPLE_SUMMARY)

        self.assertIn("8.00%", text)  # return_pct
        self.assertIn("120.50%", text)  # annual_return_pct
        self.assertIn("2.53%", text)  # max_drawdown_pct
        self.assertIn("12.87%", text)  # volatility_annual_pct

    def test_null_metrics_render_as_em_dash(self):
        sample = dict(_SAMPLE_SUMMARY)
        sample.update(
            {
                "annual_return_pct": None,
                "sharpe": None,
                "sortino": None,
                "calmar": None,
                "volatility_annual_pct": None,
                "profit_factor": None,
                "profit_loss_ratio": None,
                "avg_win_pnl": None,
                "avg_loss_pnl": None,
            }
        )
        text = render_summary_markdown(sample)

        self.assertIn("年化：—", text)
        self.assertIn("Sharpe：— · Sortino：— · Calmar：—", text)
        self.assertIn("年化波动率：—", text)
        self.assertIn("盈亏因子：—", text)
        self.assertIn("平均盈利 / 亏损：— / —", text)
        self.assertIn("盈亏比 —", text)

    def test_zero_trades_emits_anomaly(self):
        sample = dict(_SAMPLE_SUMMARY)
        sample.update({"trade_count_closed": 0, "by_symbol": []})
        text = render_summary_markdown(sample)

        self.assertIn("### 异常信号", text)
        self.assertIn("零交易", text)

    def test_open_position_emits_anomaly(self):
        sample = dict(_SAMPLE_SUMMARY)
        sample.update({"trade_count_open": 2})
        text = render_summary_markdown(sample)

        self.assertIn("收盘前仍有 2 个标的未平仓", text)

    def test_low_utilization_emits_anomaly(self):
        # 60k cash / 40k market value = 40% invested exposure.
        sample = dict(_SAMPLE_SUMMARY)
        sample.update({"final_cash": "60000", "final_market_value": "40000", "ending_equity": "100000"})
        text = render_summary_markdown(sample)

        self.assertIn("资金利用率 < 50%", text)

    def test_final_all_cash_after_closed_positions_does_not_emit_low_utilization(self):
        sample = dict(_SAMPLE_SUMMARY)
        sample.update(
            {
                "final_cash": "108000",
                "final_market_value": "0",
                "ending_equity": "108000",
                "trade_count_closed": 2,
                "trade_count_open": 0,
            }
        )
        text = render_summary_markdown(sample)

        self.assertNotIn("资金利用率 < 50%", text)

    def test_mock_data_provider_is_called_out(self):
        sample = dict(_SAMPLE_SUMMARY)
        sample.update({"data_provider": "auto", "data_provider_effective": "mock"})
        text = render_summary_markdown(sample)

        self.assertIn("数据源：`auto` → `mock`", text)
        self.assertIn("mock 数据源", text)

    def test_by_symbol_truncates_to_top_5_with_overflow_hint(self):
        sample = dict(_SAMPLE_SUMMARY)
        sample["by_symbol"] = [
            {
                "symbol": f"60000{i}.SH",
                "trade_count_closed": 1,
                "pnl": f"{1000 - i * 100}",
                "win_rate": "1",
                "win_rate_sample_size": 1,
                "avg_holding_trading_days": "10",
            }
            for i in range(8)
        ]
        text = render_summary_markdown(sample)

        # First 5 rendered.
        for i in range(5):
            self.assertIn(f"60000{i}.SH", text)
        # 6th/7th/8th hidden behind overflow note.
        self.assertNotIn("600005.SH", text)
        self.assertNotIn("600006.SH", text)
        self.assertNotIn("600007.SH", text)
        self.assertIn("另有 3 个标的未展示", text)

    def test_final_positions_section_only_when_non_empty(self):
        sample = dict(_SAMPLE_SUMMARY)
        sample["final_positions"] = [
            {
                "symbol": "600522.SH",
                "name": None,
                "quantity": 100,
                "available": 100,
                "cost_price": "30.80",
                "last_price": "40.99",
                "market_value": "4099",
                "weight_pct": "40.99",
            }
        ]
        sample["trade_count_open"] = 1
        text = render_summary_markdown(sample)

        self.assertIn("### 仍持仓", text)
        self.assertIn("`600522.SH`", text)
        self.assertIn("成本 30.80", text)
        self.assertIn("现价 40.99", text)
        self.assertIn("仓位占比 40.99%", text)

    def test_final_positions_weight_renders_missing_when_absent(self):
        """When ``weight_pct`` is null (no last_price / no equity), the cell
        falls back to the standard 「—」 placeholder instead of crashing."""

        sample = dict(_SAMPLE_SUMMARY)
        sample["final_positions"] = [
            {
                "symbol": "600522.SH",
                "name": None,
                "quantity": 100,
                "available": 100,
                "cost_price": "30.80",
                "last_price": None,
                "market_value": None,
                "weight_pct": None,
            }
        ]
        sample["trade_count_open"] = 1
        text = render_summary_markdown(sample)

        self.assertIn("仓位占比 —", text)

    def test_renders_compact_payload(self):
        """Markdown body should stay well below the per-agent budget (4000 chars)
        even with by_symbol top 5 + anomalies + final positions populated."""

        sample = dict(_SAMPLE_SUMMARY)
        sample["by_symbol"] = [
            {
                "symbol": f"60000{i}.SH",
                "trade_count_closed": 1,
                "pnl": f"{1000 - i * 100}",
                "win_rate": "1",
                "win_rate_sample_size": 1,
                "avg_holding_trading_days": "10",
            }
            for i in range(5)
        ]
        text = render_summary_markdown(sample)
        self.assertLess(len(text), 2000)

    def test_non_dict_input_returns_empty_string(self):
        self.assertEqual(render_summary_markdown(None), "")  # type: ignore[arg-type]
        self.assertEqual(render_summary_markdown("not a dict"), "")  # type: ignore[arg-type]


class WarmupInsufficientAnomalyTests(unittest.TestCase):
    """The warmup-insufficient anomaly takes priority over the generic
    "零交易" flag when ``startup_history > bars_total`` and the run
    produced no trades. This is the path that saved 4 wasted diagnostic
    calls in the asst-c392d04c94d2 session (see CLAUDE.md).
    """

    def _base(self) -> dict:
        sample = dict(_SAMPLE_SUMMARY)
        # Drop all closed trades so the "零交易" / warmup branches are live.
        sample.update(
            {
                "trade_count_closed": 0,
                "trade_count_open": 0,
                "by_symbol": [],
            }
        )
        return sample

    def test_no_warmup_anomaly_when_startup_exceeds_report_window(self):
        """The legacy ``warmup_insufficient`` anomaly conflated two
        unrelated numbers: ``bars_total`` (trading days in the user's
        report window) vs ``startup_history`` (bars the strategy needs
        for indicator warmup). The data layer's pre-warmup preload
        feeds the strategy ``startup_history`` bars per cycle regardless
        of report-window length, so a 1-month window with
        ``startup_history = 50`` is fully warmed — the flag was a
        false positive that pushed agents to extend ``--range-start``
        (breaking the user's reporting-window intent). The anomaly is
        gone; zero-trade runs now fall through to the generic "零交易"
        hint instead.
        """
        sample = self._base()
        sample["startup_history"] = 50
        sample["bars_total"] = 19
        from doyoutrade.backtest.summary import _detect_anomalies

        flags = _detect_anomalies(sample)
        joined = "\n".join(flags)
        # Specific warmup hint must NOT fire any more.
        self.assertNotIn("warmup_insufficient", joined)
        # Generic zero-trade hint is the correct guidance: the strategy
        # ran with sufficient warmup, the signal just stayed flat.
        self.assertIn("零交易", joined)

        text = render_summary_markdown(sample)
        self.assertIn("### 异常信号", text)
        self.assertNotIn("warmup_insufficient", text)
        self.assertIn("零交易", text)

    def test_warmup_anomaly_skipped_when_trades_present(self):
        # Same warmup deficit, but at least one closed trade — surface
        # other anomalies instead; warmup-insufficient is no longer
        # actionable (the strategy did trade despite the deficit).
        sample = self._base()
        sample["startup_history"] = 50
        sample["bars_total"] = 19
        sample["trade_count_closed"] = 1
        from doyoutrade.backtest.summary import _detect_anomalies

        flags = _detect_anomalies(sample)
        joined = "\n".join(flags)
        self.assertNotIn("warmup_insufficient", joined)
        self.assertNotIn("零交易", joined)

    def test_warmup_anomaly_skipped_when_open_position_present(self):
        # An open lot still counts as a "tradable signal fired"; the
        # strategy reached the entry-condition branch even if it didn't
        # exit. Suppress warmup flag, keep open-position flag, and —
        # critically — suppress the "零交易" flag too: trade_count_closed=0
        # alone is not "zero trades" when there's still an open position
        # (request1.json turn 4: +35% buy-and-hold misleadingly tagged
        # "零交易：信号始终为 0"). The fix gates the hint on
        # ``closed + open == 0``.
        sample = self._base()
        sample["startup_history"] = 50
        sample["bars_total"] = 19
        sample["trade_count_open"] = 1
        from doyoutrade.backtest.summary import _detect_anomalies

        flags = _detect_anomalies(sample)
        joined = "\n".join(flags)
        self.assertNotIn("warmup_insufficient", joined)
        self.assertIn("收盘前仍有 1 个标的未平仓", joined)
        self.assertNotIn(
            "零交易", joined,
            f"open position should suppress 零交易 hint, got: {joined!r}",
        )

    def test_no_warmup_anomaly_when_startup_unknown(self):
        # Summaries persisted before the field was added (or strategies
        # whose runtime can't resolve startup_history) must fall back to
        # the generic "零交易" flag rather than emit a confidently-wrong
        # warmup hint.
        sample = self._base()
        sample.pop("startup_history", None)
        sample.pop("bars_total", None)
        from doyoutrade.backtest.summary import _detect_anomalies

        flags = _detect_anomalies(sample)
        joined = "\n".join(flags)
        self.assertNotIn("warmup_insufficient", joined)
        self.assertIn("零交易", joined)

    def test_no_warmup_anomaly_when_bars_sufficient(self):
        # bars_total >= startup_history: warmup is satisfied. Zero
        # trades here is a strategy-logic problem (signal stayed flat),
        # not a data-coverage problem. Generic flag applies.
        sample = self._base()
        sample["startup_history"] = 19
        sample["bars_total"] = 30
        from doyoutrade.backtest.summary import _detect_anomalies

        flags = _detect_anomalies(sample)
        joined = "\n".join(flags)
        self.assertNotIn("warmup_insufficient", joined)
        self.assertIn("零交易", joined)

    def test_warmup_fields_present_in_serialized_summary(self):
        """Round-trip: ``startup_history`` and ``bars_total`` survive
        ``compute_summary -> summary_to_json``. Used by the assistant
        tool surface tests below and by frontend consumers."""

        eq = _linear_equity(_ts(2026, 1, 1), 5, Decimal("100000"), Decimal("100000"))
        summary = compute_summary(
            run_id="r-warmup-1",
            range_start_utc=eq[0].t,
            range_end_utc=eq[-1].t,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100000"),
            final_cash=Decimal("100000"),
            final_positions=(),
            equity_history=eq,
            fills=(),
            trading_dates=_flat_calendar(_ts(2026, 1, 1), 5),
            completed_at=eq[-1].t,
            startup_history=50,
            bars_total=19,
        )
        self.assertEqual(summary.startup_history, 50)
        self.assertEqual(summary.bars_total, 19)
        out = summary_to_json(summary)
        self.assertEqual(out["startup_history"], 50)
        self.assertEqual(out["bars_total"], 19)

    def test_warmup_fields_default_to_none_when_omitted(self):
        # Backwards compatibility: callers that don't pass the new args
        # get ``None`` on both fields. Anomaly detection treats this as
        # "no signal" rather than fabricating a flag.
        eq = _linear_equity(_ts(2026, 1, 1), 3, Decimal("100000"), Decimal("100000"))
        summary = compute_summary(
            run_id="r-warmup-2",
            range_start_utc=eq[0].t,
            range_end_utc=eq[-1].t,
            bar_interval="1d",
            starting_equity=Decimal("100000"),
            ending_equity=Decimal("100000"),
            final_cash=Decimal("100000"),
            final_positions=(),
            equity_history=eq,
            fills=(),
            trading_dates=_flat_calendar(_ts(2026, 1, 1), 3),
            completed_at=eq[-1].t,
        )
        self.assertIsNone(summary.startup_history)
        self.assertIsNone(summary.bars_total)
        out = summary_to_json(summary)
        self.assertIsNone(out["startup_history"])
        self.assertIsNone(out["bars_total"])


class ZeroTradeFalsePositiveRegressionTests(unittest.TestCase):
    """request1.json turn 4: a buy-and-hold MACD run on 中天科技 returned
    +35.07% in 21 bars with ``trade_count_closed=0`` (still holding) and
    ``trade_count_open=1``. The pre-fix anomaly detector flagged
    "零交易：信号始终为 0 或入场条件过严" — actively misleading guidance
    given the strategy had clearly entered (and was profitably so). The
    fix gates the hint on total activity (closed + open == 0).
    """

    def _buy_and_hold_summary(self) -> dict:
        # Mirrors the persisted summary shape from the request1.json turn-4
        # report: one open lot at ~100% utilization, no closed trades.
        sample = dict(_SAMPLE_SUMMARY)
        sample.update(
            {
                "trade_count_closed": 0,
                "trade_count_open": 1,
                "starting_equity": "100000",
                "ending_equity": "135073.16",
                "return_pct": "35.07",
                "final_cash": "20",           # ~0% cash; ~100% deployed
                "final_market_value": "135053.24",
                "by_symbol": [],
                "final_positions": [
                    {
                        "symbol": "600522.SH",
                        "name": None,
                        "quantity": 3082,
                        "available": 3082,
                        "cost_price": "32.44",
                        "last_price": "43.82",
                        "market_value": "135053.24",
                        "weight_pct": "99.99",
                    }
                ],
            }
        )
        return sample

    def test_buy_and_hold_does_not_flag_zero_trade(self):
        from doyoutrade.backtest.summary import _detect_anomalies

        flags = _detect_anomalies(self._buy_and_hold_summary())
        joined = "\n".join(flags)
        # The strategy entered and is still in the trade — that is the
        # opposite of "信号始终为 0".
        self.assertNotIn(
            "零交易", joined,
            f"buy-and-hold should not trip 零交易, got: {joined!r}",
        )
        # The genuine signal (still-open position) must still surface.
        self.assertIn("收盘前仍有 1 个标的未平仓", joined)

    def test_buy_and_hold_report_omits_zero_trade(self):
        # End-to-end: the markdown report must agree with the anomaly
        # detector. A regression here would re-introduce the misleading
        # hint into the user-visible report even if the structured list
        # is clean.
        text = render_summary_markdown(self._buy_and_hold_summary())
        self.assertIn("### 异常信号", text)  # other anomalies still surface
        self.assertNotIn("零交易", text)
        self.assertIn("收盘前仍有 1 个标的未平仓", text)

    def test_utilization_flag_suppressed_when_total_activity_zero(self):
        # A truly idle run (closed=0, open=0) sitting in cash already
        # gets the "零交易" hint; double-flagging "资金利用率 < 50%"
        # adds no new information and conflates two distinct root causes
        # (no signal vs. signal-but-undeployed). The fix suppresses the
        # second flag for the all-zero case.
        sample = dict(_SAMPLE_SUMMARY)
        sample.update(
            {
                "trade_count_closed": 0,
                "trade_count_open": 0,
                # 60k cash vs 100k equity → 60% in cash, would normally trip.
                "final_cash": "60000",
                "ending_equity": "100000",
                "by_symbol": [],
            }
        )
        from doyoutrade.backtest.summary import _detect_anomalies

        flags = _detect_anomalies(sample)
        joined = "\n".join(flags)
        self.assertIn("零交易", joined)
        self.assertNotIn(
            "资金利用率 < 50%", joined,
            f"all-zero case should not double-flag utilisation, got: {joined!r}",
        )


if __name__ == "__main__":
    unittest.main()
