"""``doyoutrade.assistant.review_analytics`` — deterministic review pre-processing.

Covers the three public callables introduced for the daily_review Agent
upgrade:

* :func:`build_review_metrics` — aggregates the gathered statement into a
  structured metrics payload (money as decimal strings, ratios already ×100,
  ``None`` when a metric cannot be derived rather than a fabricated 0).
* :func:`build_rule_diagnostics` — runs deterministic rules over the metrics
  to emit ``{code, severity, title, detail, recommendation}`` findings.
* :func:`build_fallback_journal` — composes a minimal Python-only 复盘 used
  when the LLM turn fails / returns empty.

Properties asserted:
  - Money is decimal-string-normalised (no float noise, no spurious trailing
    zeros) regardless of how the caller passed it (Decimal / float / int /
    str). This is the same contract as the rest of the statement pipeline.
  - Missing inputs → ``None`` (not 0). Rules must skip when the input is
    ``None``; they must NOT silently treat ``None`` as "0% concentration".
  - Rule severity ranking (critical < warn < info) and that each rule fires
    on its own threshold without coupling.
  - The fallback journal always starts with ``# <asof> 复盘`` (KB index
    contract) and surfaces the reason banner + at least the five standard
    sections, even when metrics are mostly ``None``.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from doyoutrade.assistant.review_analytics import (
    build_fallback_journal,
    build_review_metrics,
    build_rule_diagnostics,
    parse_trailing_review_json,
)


def _stmt(
    *,
    cash: str | None = "1000",
    equity: str | None = "5000",
    total_market_value: str | None = "0",
    positions: list[dict] | None = None,
    asset: dict | None = None,
    trades: list[dict] | None = None,
    errors: list[dict] | None = None,
) -> dict:
    return {
        "asof": "2026-06-17",
        "source": "broker",
        "account": {
            "account": {"cash": cash, "equity": equity},
            "total_market_value": total_market_value,
            "positions": positions or [],
        },
        "asset": asset,
        "trades": trades or [],
        "trade_count": len(trades or []),
        "errors": errors or [],
    }


class BuildReviewMetricsTests(unittest.TestCase):
    def test_empty_statement_yields_none_not_zero(self):
        m = build_review_metrics(_stmt(cash=None, equity=None, total_market_value=None))
        # Ratios that depend on a missing denominator must be None, not 0.0.
        # Returning 0.0 here would be a silent bug (AGENTS.md §错误可见性).
        self.assertIsNone(m["cash_ratio_pct"])
        self.assertIsNone(m["holding_pnl_pct"])
        self.assertIsNone(m["concentration_top1_pct"])
        self.assertIsNone(m["profit_loss_ratio_pct"])
        self.assertIsNone(m["fee_to_turnover_pct"])
        self.assertEqual(m["trade_count"], 0)
        self.assertEqual(m["position_count"], 0)
        self.assertEqual(m["top_positions"], [])
        # Money fields stay as decimal strings.
        self.assertEqual(m["buy_amount"], "0")
        self.assertEqual(m["sell_amount"], "0")
        self.assertEqual(m["net_amount"], "0")
        self.assertEqual(m["fee_total"], "0")
        # asof + lookback passthrough.
        self.assertEqual(m["asof"], "2026-06-17")
        self.assertEqual(m["lookback_days"], 1)

    def test_trade_side_classification(self):
        trades = [
            {"side": "buy", "amount": "10000", "commission": "5"},
            {"side": "买入", "amount": "5000", "commission": "5"},
            {"side": "sell", "amount": "8000", "commission": "4"},
            {"side": "S", "amount": "2000", "commission": "3"},
            {"side": "unknown", "amount": "999", "commission": "1"},
        ]
        m = build_review_metrics(_stmt(trades=trades, total_market_value="0"))
        self.assertEqual(m["trade_count"], 5)
        self.assertEqual(m["buy_count"], 2)
        self.assertEqual(m["sell_count"], 2)
        self.assertEqual(m["other_side_count"], 1)
        # buy_amount = 10000 + 5000; sell_amount = 8000 + 2000.
        self.assertEqual(m["buy_amount"], "15000")
        self.assertEqual(m["sell_amount"], "10000")
        # net = sell - buy = -5000 (net buy / capital outflow).
        self.assertEqual(m["net_amount"], "-5000")
        # fee_total = 5+5+4+3+1 = 18.
        self.assertEqual(m["fee_total"], "18")
        # turnover = 25000 → fee_to_turnover_pct = 18/25000 * 100 = 0.072.
        self.assertAlmostEqual(m["fee_to_turnover_pct"], 0.072, places=4)

    def test_concentration_and_cash_ratio(self):
        positions = [
            {"symbol": "600519.SH", "name": "贵州茅台", "market_value": "60000"},
            {"symbol": "000858.SZ", "name": "五粮液", "market_value": "30000"},
            {"symbol": "002714.SZ", "name": "牧原股份", "market_value": "10000"},
        ]
        asset = {
            "total_asset": "100000",
            "market_value": "100000",
            "cash": "10000",
            "frozen_cash": "0",
            "available_cash": "10000",
            "profit_loss": "1234.5",
            "profit_loss_ratio": 0.012345,  # broker fraction; ×100 in metric.
        }
        m = build_review_metrics(
            _stmt(
                cash="10000",
                equity="110000",
                total_market_value="100000",
                positions=positions,
                asset=asset,
            )
        )
        # concentration_top1 = 60000/100000 = 60%.
        self.assertEqual(m["concentration_top1_pct"], 60.0)
        # concentration_top3 = 100% (all three sum to 100000).
        self.assertAlmostEqual(m["concentration_top3_pct"], 100.0, places=2)
        self.assertEqual(len(m["top_positions"]), 3)
        self.assertEqual(m["top_positions"][0]["symbol"], "600519.SH")
        self.assertEqual(m["top_positions"][0]["weight_pct"], 60.0)
        # cash_ratio = 10000/100000 * 100 = 10%.
        self.assertAlmostEqual(m["cash_ratio_pct"], 10.0, places=4)
        # holding_pnl_pct = 1234.5 / 100000 * 100 = 1.2345.
        self.assertAlmostEqual(m["holding_pnl_pct"], 1.2345, places=4)
        # profit_loss_ratio_pct = 0.012345 * 100 = 1.2345.
        self.assertAlmostEqual(m["profit_loss_ratio_pct"], 1.2345, places=4)
        # Money fields are decimal strings.
        self.assertEqual(m["total_asset"], "100000")
        self.assertEqual(m["profit_loss"], "1234.5")

    def test_derive_total_mv_when_statement_missing(self):
        # Statement has positions with MV but total_market_value is missing/0.
        # The metrics layer re-derives from positions so concentration is still
        # meaningful — but the derived value goes into total_market_value too.
        positions = [
            {"symbol": "A", "name": "A", "market_value": "7000"},
            {"symbol": "B", "name": "B", "market_value": "3000"},
        ]
        m = build_review_metrics(
            _stmt(total_market_value=None, positions=positions)
        )
        self.assertEqual(m["total_market_value"], "10000")
        self.assertEqual(m["concentration_top1_pct"], 70.0)

    def test_errors_propagation(self):
        stmt_errors = [{"stage": "trades", "error_type": "X", "message": "m1"}]
        kb_errors = [{"stage": "index", "error_type": "Y", "message": "m2"}]
        m = build_review_metrics(
            _stmt(errors=stmt_errors),
            knowledge={"errors": kb_errors},
        )
        self.assertEqual(len(m["errors"]), 2)
        stages = {e["stage"] for e in m["errors"]}
        self.assertEqual(stages, {"trades", "index"})

    def test_decimal_string_inputs_normalised(self):
        # Callers may pass Decimal / int / float directly (e.g. tests). Output
        # must still be the canonical decimal-string contract (no float noise).
        positions = [
            {
                "symbol": "X",
                "name": "X",
                "market_value": Decimal("333.30"),  # Decimal input
            }
        ]
        m = build_review_metrics(_stmt(positions=positions, total_market_value="333.3"))
        self.assertEqual(m["total_market_value"], "333.3")
        self.assertEqual(m["concentration_top1_pct"], 100.0)


class BuildRuleDiagnosticsTests(unittest.TestCase):
    def test_no_findings_on_empty_metrics(self):
        m = build_review_metrics(_stmt(cash=None, equity=None, total_market_value=None))
        self.assertEqual(build_rule_diagnostics(m), [])

    def test_concentration_critical_over_warn(self):
        positions = [
            {"symbol": "600519.SH", "name": "贵州茅台", "market_value": "70000"},
            {"symbol": "X", "name": "X", "market_value": "30000"},
        ]
        asset = {"total_asset": "100000"}
        m = build_review_metrics(
            _stmt(positions=positions, asset=asset, total_market_value="100000")
        )
        findings = build_rule_diagnostics(m)
        codes = [f["code"] for f in findings]
        # Critical wins over warn for the same rule family.
        self.assertIn("concentration_top1_critical", codes)
        self.assertNotIn("concentration_top1_high", codes)
        critical = next(f for f in findings if f["code"] == "concentration_top1_critical")
        self.assertEqual(critical["severity"], "critical")
        self.assertIn("贵州茅台", critical["detail"])
        self.assertTrue(critical["recommendation"])

    def test_concentration_warn_band(self):
        positions = [
            {"symbol": "A", "name": "A", "market_value": "45000"},
            {"symbol": "B", "name": "B", "market_value": "55000"},
        ]
        asset = {"total_asset": "100000"}
        m = build_review_metrics(
            _stmt(positions=positions, asset=asset, total_market_value="100000")
        )
        codes = [f["code"] for f in build_rule_diagnostics(m)]
        self.assertIn("concentration_top1_high", codes)
        self.assertNotIn("concentration_top1_critical", codes)

    def test_cash_ratio_near_fully_invested(self):
        positions = [{"symbol": "A", "name": "A", "market_value": "95000"}]
        asset = {"total_asset": "100000"}
        m = build_review_metrics(
            _stmt(
                cash="5000",
                positions=positions,
                asset=asset,
                total_market_value="95000",
            )
        )
        codes = [f["code"] for f in build_rule_diagnostics(m)]
        self.assertIn("near_fully_invested", codes)

    def test_cash_ratio_near_flat_no_action(self):
        asset = {"total_asset": "100000"}
        m = build_review_metrics(
            _stmt(cash="95000", equity="95000", asset=asset, total_market_value="0")
        )
        # 95% cash, 0 trades, 0 positions → near_flat_no_action (info).
        codes = [f["code"] for f in build_rule_diagnostics(m)]
        self.assertIn("near_flat_no_action", codes)

    def test_fee_rate_warn_vs_critical(self):
        # fee_total=300 on turnover=100000 → 0.3% → critical (>0.2).
        trades = [
            {"side": "buy", "amount": "50000", "commission": "150"},
            {"side": "sell", "amount": "50000", "commission": "150"},
        ]
        m = build_review_metrics(_stmt(trades=trades, total_market_value="0"))
        codes = [f["code"] for f in build_rule_diagnostics(m)]
        self.assertIn("fee_rate_critical", codes)
        self.assertNotIn("fee_rate_high", codes)

    def test_fee_rate_warn_band(self):
        # fee_total=15 on turnover=10000 → 0.15% → warn (0.1 ≤ x < 0.2).
        trades = [
            {"side": "buy", "amount": "5000", "commission": "7.5"},
            {"side": "sell", "amount": "5000", "commission": "7.5"},
        ]
        m = build_review_metrics(_stmt(trades=trades, total_market_value="0"))
        codes = [f["code"] for f in build_rule_diagnostics(m)]
        self.assertIn("fee_rate_high", codes)
        self.assertNotIn("fee_rate_critical", codes)

    def test_high_trade_count_fires(self):
        trades = [
            {"side": "buy", "amount": "100", "commission": "5"}
            for _ in range(25)
        ]
        m = build_review_metrics(_stmt(trades=trades, total_market_value="0"))
        codes = [f["code"] for f in build_rule_diagnostics(m)]
        self.assertIn("high_trade_count", codes)

    def test_position_fragmented(self):
        positions = [
            {"symbol": f"S{i}", "name": f"N{i}", "market_value": "1000"}
            for i in range(12)
        ]
        m = build_review_metrics(_stmt(positions=positions, total_market_value="12000"))
        codes = [f["code"] for f in build_rule_diagnostics(m)]
        self.assertIn("position_fragmented", codes)

    def test_big_session_loss_and_win(self):
        # loss = -6000 / 100000 = -6% → big_session_loss
        asset_loss = {"total_asset": "100000", "profit_loss": "-6000"}
        m_loss = build_review_metrics(
            _stmt(asset=asset_loss, total_market_value="0")
        )
        codes_loss = [f["code"] for f in build_rule_diagnostics(m_loss)]
        self.assertIn("big_session_loss", codes_loss)
        self.assertNotIn("big_session_win", codes_loss)

        asset_win = {"total_asset": "100000", "profit_loss": "6000"}
        m_win = build_review_metrics(
            _stmt(asset=asset_win, total_market_value="0")
        )
        codes_win = [f["code"] for f in build_rule_diagnostics(m_win)]
        self.assertIn("big_session_win", codes_win)
        self.assertNotIn("big_session_loss", codes_win)

    def test_data_partially_unavailable_when_errors(self):
        m = build_review_metrics(
            _stmt(errors=[{"stage": "trades", "error_type": "X", "message": "m"}])
        )
        codes = [f["code"] for f in build_rule_diagnostics(m)]
        self.assertIn("data_partially_unavailable", codes)

    def test_severity_sorting_critical_first(self):
        # Construct metrics that fire multiple rules across severities.
        positions = [
            {"symbol": "X", "name": "X", "market_value": "95000"},  # near_fully_invested + concentration_critical
        ]
        asset = {
            "total_asset": "100000",
            "profit_loss": "-8000",  # big_session_loss (-8%)
        }
        trades = [
            {"side": "buy", "amount": "50000", "commission": "200"},  # fee_rate_critical
            {"side": "sell", "amount": "50000", "commission": "200"},
        ] + [
            {"side": "buy", "amount": "100", "commission": "5"}
            for _ in range(25)  # high_trade_count
        ]
        m = build_review_metrics(
            _stmt(
                cash="5000",
                positions=positions,
                asset=asset,
                trades=trades,
                total_market_value="95000",
            )
        )
        findings = build_rule_diagnostics(m)
        sev = [f["severity"] for f in findings]
        # All criticals first, then warns, then infos.
        self.assertEqual(
            sev,
            sorted(sev, key=lambda s: {"critical": 0, "warn": 1, "info": 2}[s]),
        )
        self.assertEqual(sev[0], "critical")


class BuildFallbackJournalTests(unittest.TestCase):
    def test_starts_with_title_and_banner(self):
        m = build_review_metrics(_stmt())
        d = build_rule_diagnostics(m)
        text = build_fallback_journal("2026-06-17", m, d, reason="compose_failed")
        # KB index title contract — must start with exactly this line.
        self.assertTrue(text.startswith("# 2026-06-17 复盘\n"))
        # Reason banner is visible (never silent — AGENTS.md §错误可见性).
        self.assertIn("reason=compose_failed", text)
        self.assertIn("兜底", text)

    def test_has_five_standard_sections(self):
        m = build_review_metrics(_stmt())
        d = build_rule_diagnostics(m)
        text = build_fallback_journal("2026-06-17", m, d, reason="empty_reply")
        for section in (
            "## 账户概览",
            "## 今日成交",
            "## 持仓盘点",
            "## 盈亏归因与复盘",
            "## 风险提示与明日计划",
        ):
            self.assertIn(section, text)

    def test_renders_findings_when_present(self):
        positions = [
            {"symbol": "600519.SH", "name": "贵州茅台", "market_value": "70000"},
            {"symbol": "X", "name": "X", "market_value": "30000"},
        ]
        asset = {"total_asset": "100000"}
        m = build_review_metrics(
            _stmt(positions=positions, asset=asset, total_market_value="100000")
        )
        d = build_rule_diagnostics(m)
        self.assertTrue(any(f["severity"] == "critical" for f in d))
        text = build_fallback_journal("2026-06-17", m, d, reason="compose_failed")
        self.assertIn("贵州茅台", text)
        self.assertIn("单票仓位严重集中", text)

    def test_missing_metrics_render_as_dash_not_zero(self):
        m = build_review_metrics(_stmt(cash=None, equity=None, total_market_value=None))
        d = build_rule_diagnostics(m)
        text = build_fallback_journal("2026-06-17", m, d, reason="compose_failed")
        # Missing money fields render as "—" so the reader distinguishes
        # "missing" from "zero". A bare 0 here would conflate the two.
        self.assertIn("—", text)

    def test_trailing_json_block_has_fallback_source(self):
        m = build_review_metrics(_stmt())
        d = build_rule_diagnostics(m)
        text = build_fallback_journal("2026-06-17", m, d, reason="compose_failed")
        # The trailing JSON block uses the same contract as the LLM is asked
        # to emit, so downstream parsers treat both shapes uniformly.
        self.assertIn('```json', text)
        self.assertIn('"source": "fallback"', text)
        self.assertIn('"ai_status": "fallback"', text)


class ParseTrailingReviewJsonTests(unittest.TestCase):
    def test_none_on_empty_or_no_block(self):
        self.assertIsNone(parse_trailing_review_json(""))
        self.assertIsNone(parse_trailing_review_json("# title\n\nplain prose only"))

    def test_parses_last_block_when_multiple_present(self):
        # An LLM may illustrate a JSON shape mid-prose; the parser must take
        # the LAST block (the trailing one the framing asks for), not the
        # first.
        text = (
            "# 2026-06-17 复盘\n\n"
            "例如 ```json\n{\"example\": true}\n```\n是在正文中举例。\n\n"
            "## 风险提示\n\n正文结束。\n\n"
            "```json\n"
            "{\n"
            '  "source": "llm",\n'
            '  "ai_status": "ok",\n'
            '  "summary": "今日小赢",\n'
            '  "diagnosis": ["单票仓位过重"],\n'
            '  "recommendations": ["明日减仓"],\n'
            '  "cautions": ["本复盘只读"]\n'
            "}\n"
            "```"
        )
        parsed = parse_trailing_review_json(text)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["source"], "llm")
        self.assertEqual(parsed["summary"], "今日小赢")
        self.assertEqual(parsed["diagnosis"], ["单票仓位过重"])

    def test_returns_none_on_malformed_json(self):
        # Defensive: a malformed block must NOT raise; it yields None so the
        # journal body is still usable on its own.
        text = "# x\n\n```json\n{not valid json, missing quotes}\n```"
        self.assertIsNone(parse_trailing_review_json(text))

    def test_returns_none_when_block_parses_to_non_dict(self):
        # A JSON array is technically valid JSON but not the contract shape.
        text = "# x\n\n```json\n[1, 2, 3]\n```"
        self.assertIsNone(parse_trailing_review_json(text))


if __name__ == "__main__":
    unittest.main()
