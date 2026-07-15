"""Tests for Jinja2-backed prompt templates."""

from __future__ import annotations

import unittest

from doyoutrade.prompts import (
    REVIEW_SYSTEM,
    REVIEW_USER,
    SIGNAL_SYSTEM,
    SIGNAL_USER,
    render_prompt,
)


class RenderPromptTests(unittest.TestCase):
    def test_signal_system_contains_core_rules(self) -> None:
        text = render_prompt(SIGNAL_SYSTEM)
        self.assertIn("量化信号生成器", text)
        self.assertIn("proposals", text)
        self.assertIn("universe", text)
        self.assertIn("strategy_preferences", text)
        self.assertIn("信号阶段", text)
        self.assertIn("long", text)
        self.assertIn("short", text)

    def test_signal_system_skills_tool_guidance_section(self) -> None:
        text = render_prompt(SIGNAL_SYSTEM, skills_tool_guidance=True)
        self.assertIn("技能（system-reminder）", text)
        self.assertIn("invoke_skill", text)

    def test_format_skills_listing_for_reminder_packaged_skill(self) -> None:
        from doyoutrade.skills import format_skills_listing_for_reminder

        text = format_skills_listing_for_reminder()
        self.assertIn("daily-range-swing-trade", text)
        self.assertTrue(text.startswith("- ") or "\n- " in text)

    def test_signal_user_markdown_includes_core_fields(self) -> None:
        payload = {
            "cycle_time": "2026-01-01T08:00:00+08:00",
            "max_proposals": 3,
            "universe": ["A"],
            "watch_symbols": [],
            "strategy_preferences": "",
            "debug_note": "",
            "extensions": {},
            "enrichment": {},
        }
        raw = render_prompt(SIGNAL_USER, payload=payload, skills_reminder_listing="")
        self.assertIn("2026-01-01T08:00:00+08:00", raw)
        self.assertIn("3", raw)
        self.assertIn("`A`", raw)
        self.assertIn("不包含", raw)

    def test_signal_user_skills_reminder_in_template(self) -> None:
        payload = {
            "cycle_time": "2026-01-01T08:00:00+08:00",
            "max_proposals": 1,
            "universe": ["X"],
            "watch_symbols": [],
            "strategy_preferences": "",
            "debug_note": "",
            "extensions": {},
            "enrichment": {},
        }
        raw = render_prompt(
            SIGNAL_USER,
            payload=payload,
            skills_reminder_listing="- example-signal: test",
        )
        self.assertTrue(raw.startswith("<system-reminder>"))
        self.assertIn("invoke_skill", raw)
        self.assertIn("- example-signal: test", raw)
        self.assertIn("## 周期与时间", raw)

    def test_review_system_contains_schema(self) -> None:
        text = render_prompt(REVIEW_SYSTEM, notional_cap=9_999_999_999.0, equity_fraction_percent=100)
        self.assertIn("交易复核执行器", text)
        self.assertIn("reviews", text)
        self.assertIn("symbol_scope", text)

    def test_review_system_unlimited_notional_cap_branch(self) -> None:
        text = render_prompt(REVIEW_SYSTEM, notional_cap=None, equity_fraction_percent=100)
        self.assertIn("未配置", text)
        self.assertIn("T = 权益 × f", text)

    def test_review_user_markdown_includes_payload_fields(self) -> None:
        payload = {
            "market_prices": {"600000.SH": 10.5},
            "account": {"cash": 1000.0, "equity": 50000.0},
            "review_sizing": {"notional_cap": 9_999_999_999.0, "equity_fraction": 1.0},
            "positions": [{"symbol": "600000.SH", "quantity": 100, "cost_price": 9.8}],
            "proposals": [
                {
                    "proposal_index": 0,
                    "symbol": "600000.SH",
                    "side": "long",
                    "strategy_tag": "trend",
                    "rationale": "test",
                }
            ],
        }
        raw = render_prompt(REVIEW_USER, payload=payload)
        self.assertIn("600000.SH", raw)
        self.assertIn("1000.0", raw)
        self.assertIn("50000.0", raw)
        self.assertIn("数量 100", raw)
        self.assertIn("提案 #0", raw)
        self.assertIn("test", raw)

    def test_review_user_unlimited_cap_markdown(self) -> None:
        payload = {
            "market_prices": {},
            "account": {"cash": 1000.0, "equity": 50000.0},
            "review_sizing": {"notional_cap": None, "equity_fraction": 1.0},
            "positions": [],
            "proposals": [],
        }
        raw = render_prompt(REVIEW_USER, payload=payload)
        self.assertIn("无上限", raw)
        self.assertIn("T = 权益 × f", raw)


if __name__ == "__main__":
    unittest.main()
