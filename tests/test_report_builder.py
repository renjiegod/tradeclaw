"""Tests for the deterministic report builder + report templates.

Covers sorting (score None / descending), action bucket counting, price
formatting, zh/en labels, summary_only, empty items, and rendering smoke for
both ``report/markdown.j2`` and ``report/brief.j2``.
"""

import unittest
from datetime import date, datetime
from decimal import Decimal

from doyoutrade.assistant.reporting import ReportItem, ReportRequest, render_report
from doyoutrade.assistant.reporting.builder import build_context


def _items():
    return [
        ReportItem(symbol="000001.SZ", name="平安银行", action="watch", score=55.0,
                   price=12.34, change_pct=-1.2),
        ReportItem(symbol="600519.SH", name="贵州茅台", action="buy", score=80.0,
                   price=Decimal("1680.5"), change_pct=2.35, trend="MA20 上方",
                   core_conclusion="强势上行", key_indicators={"RSI14": "62.1"},
                   battle_plan={"entry": "1650", "stop_loss": "1600", "target": "1800"},
                   logic="白酒龙头", risks="估值偏高", news=["新闻甲", "新闻乙"]),
        ReportItem(symbol="300750.SZ", name="宁德时代", action="sell", score=None),
    ]


class BuildContextTests(unittest.TestCase):
    def test_sorted_by_score_desc_none_last(self):
        ctx = build_context(ReportRequest(items=_items()))
        symbols = [i["symbol"] for i in ctx["items"]]
        self.assertEqual(symbols, ["600519.SH", "000001.SZ", "300750.SZ"])

    def test_none_scores_keep_stable_order(self):
        items = [
            ReportItem(symbol="A", score=None),
            ReportItem(symbol="B", score=10.0),
            ReportItem(symbol="C", score=None),
        ]
        ctx = build_context(ReportRequest(items=items))
        self.assertEqual([i["symbol"] for i in ctx["items"]], ["B", "A", "C"])

    def test_bucket_counts(self):
        ctx = build_context(ReportRequest(items=_items()))
        self.assertEqual(ctx["counts"]["buy"], 1)
        self.assertEqual(ctx["counts"]["watch"], 1)
        self.assertEqual(ctx["counts"]["sell"], 1)
        self.assertEqual(ctx["counts"]["total"], 3)

    def test_price_formatting(self):
        ctx = build_context(ReportRequest(items=_items()))
        by_symbol = {i["symbol"]: i for i in ctx["items"]}
        self.assertEqual(by_symbol["600519.SH"]["price_display"], "1,680.50")
        self.assertEqual(by_symbol["000001.SZ"]["price_display"], "12.34")
        self.assertEqual(by_symbol["300750.SZ"]["price_display"], "—")
        self.assertEqual(by_symbol["600519.SH"]["change_pct_display"], "+2.35%")
        self.assertEqual(by_symbol["000001.SZ"]["change_pct_display"], "-1.20%")
        self.assertEqual(by_symbol["300750.SZ"]["change_pct_display"], "—")

    def test_invalid_price_type_raises(self):
        with self.assertRaises(ValueError):
            build_context(
                ReportRequest(items=[ReportItem(symbol="A", price="12.3")])
            )

    def test_zh_labels(self):
        ctx = build_context(ReportRequest(items=_items(), language="zh"))
        self.assertEqual(ctx["labels"]["buy"], "买入")
        by_symbol = {i["symbol"]: i for i in ctx["items"]}
        self.assertEqual(by_symbol["600519.SH"]["action_label"], "买入")
        self.assertIn("买入", ctx["summary_line"])

    def test_en_labels(self):
        ctx = build_context(ReportRequest(items=_items(), language="en"))
        self.assertEqual(ctx["labels"]["buy"], "buy")
        by_symbol = {i["symbol"]: i for i in ctx["items"]}
        self.assertEqual(by_symbol["600519.SH"]["action_label"], "buy")
        self.assertIn("1 buy / 1 watch / 1 sell", ctx["summary_line"])

    def test_unsupported_language_raises(self):
        with self.assertRaises(ValueError):
            build_context(ReportRequest(items=[], language="fr"))

    def test_non_report_item_raises(self):
        with self.assertRaises(ValueError):
            build_context(ReportRequest(items=[{"symbol": "A"}]))

    def test_has_plan_flag(self):
        ctx = build_context(ReportRequest(items=_items()))
        by_symbol = {i["symbol"]: i for i in ctx["items"]}
        self.assertTrue(by_symbol["600519.SH"]["has_plan"])
        self.assertFalse(by_symbol["000001.SZ"]["has_plan"])

    def test_as_of_date_and_datetime(self):
        ctx = build_context(ReportRequest(items=[], as_of=date(2026, 7, 13)))
        self.assertEqual(ctx["as_of"], "2026-07-13")
        ctx = build_context(
            ReportRequest(items=[], as_of=datetime(2026, 7, 13, 15, 30))
        )
        self.assertEqual(ctx["as_of"], "2026-07-13 15:30")
        ctx = build_context(ReportRequest(items=[]))
        self.assertEqual(ctx["as_of"], "")


class RenderReportTests(unittest.TestCase):
    def test_markdown_render_smoke(self):
        md = render_report(
            ReportRequest(items=_items(), title="每日研报", as_of=date(2026, 7, 13))
        )
        self.assertIn("# 每日研报", md)
        self.assertIn("2026-07-13", md)
        self.assertIn("600519.SH", md)
        self.assertIn("贵州茅台", md)
        self.assertIn("强势上行", md)          # core conclusion
        self.assertIn("RSI14: 62.1", md)       # key indicator
        self.assertIn("止损", md)              # battle plan label localized
        self.assertIn("新闻甲", md)            # news
        self.assertIn("1,680.50", md)          # formatted price
        # summary bar counts
        self.assertIn("1 买入 / 1 观察 / 1 卖出", md)

    def test_markdown_summary_only_omits_sections(self):
        md = render_report(
            ReportRequest(items=_items(), title="每日研报", summary_only=True)
        )
        self.assertIn("1 买入 / 1 观察 / 1 卖出", md)
        self.assertNotIn("600519.SH", md)

    def test_markdown_empty_items(self):
        md = render_report(ReportRequest(items=[], title="空报告"))
        self.assertIn("# 空报告", md)
        self.assertIn("本期无入选标的", md)

    def test_brief_render_smoke(self):
        md = render_report(
            ReportRequest(items=_items(), title="简报", as_of=date(2026, 7, 13)),
            template="report/brief.j2",
        )
        self.assertIn("简报", md)
        self.assertIn("600519.SH", md)
        self.assertIn("强势上行", md)
        self.assertIn("1 买入 / 1 观察 / 1 卖出", md)

    def test_brief_render_en(self):
        md = render_report(
            ReportRequest(items=_items(), title="Brief", language="en"),
            template="report/brief.j2",
        )
        self.assertIn("1 buy / 1 watch / 1 sell", md)
        self.assertIn("[buy/80.0]", md)

    def test_brief_empty_items(self):
        md = render_report(
            ReportRequest(items=[], title="Brief", language="en"),
            template="report/brief.j2",
        )
        self.assertIn("no symbols selected", md)


if __name__ == "__main__":
    unittest.main()
