"""Tests for built-in assistant system-prompt templates."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from doyoutrade.assistant.prompt_templates import (
    get_prompt_template_text,
    list_prompt_templates,
)


class AssistantPromptTemplateTests(unittest.TestCase):
    def test_list_prompt_templates_is_stable_and_complete(self) -> None:
        items = list_prompt_templates()

        self.assertEqual(
            [item["template_id"] for item in items],
            [
                "main-agent",
                "swing-trader",
                "event-driven",
                "research-copilot",
                "signal-card-composer",
            ],
        )
        for item in items:
            self.assertIn("name", item)
            self.assertIn("description", item)
            self.assertIn("system_prompt", item)
            self.assertTrue(item["system_prompt"].strip())

    def test_list_prompt_templates_contains_swing_trader_template_text(self) -> None:
        items = {item["template_id"]: item for item in list_prompt_templates()}
        item = items["swing-trader"]

        self.assertEqual(item["name"], "Swing Trader")
        self.assertIn("keep position sizing and stop logic conservative", item["system_prompt"])

    def test_get_prompt_template_text_renders_jinja_template(self) -> None:
        with patch(
            "doyoutrade.assistant.prompt_templates._render_template",
            return_value="Hello Preview",
        ) as mock_render:
            text = get_prompt_template_text("swing-trader")

        self.assertEqual(text, "Hello Preview")
        mock_render.assert_called_once()

    def test_list_prompt_templates_returns_rendered_preview_content(self) -> None:
        with patch(
            "doyoutrade.assistant.prompt_templates.get_prompt_template_text",
            side_effect=lambda template_id: f"preview::{template_id}",
        ):
            items = list_prompt_templates()

        self.assertEqual(
            [item["system_prompt"] for item in items],
            [
                "preview::main-agent",
                "preview::swing-trader",
                "preview::event-driven",
                "preview::research-copilot",
                "preview::signal-card-composer",
            ],
        )

    def test_main_agent_prompt_prefers_schema_contract_before_help(self) -> None:
        text = get_prompt_template_text("main-agent")

        self.assertIn("Contract First", text)
        self.assertIn("doyoutrade-cli schema <command>", text)
        self.assertIn("data.cli_contract", text)

    def test_main_agent_prompt_separates_execution_layer_from_skill_docs(self) -> None:
        text = get_prompt_template_text("main-agent")

        self.assertIn("## 执行分层", text)
        self.assertIn("执行层（真正动手）", text)
        self.assertIn("说明层（帮助你理解怎么做）", text)
        self.assertIn("skill 名称用于**选择一份说明**", text)
        self.assertIn("先问自己：这是要**执行一个动作**，还是先**补一份方法说明**", text)
        self.assertIn("优先在 CLI / in-process tool 中找入口", text)

    def test_main_agent_prompt_forces_load_skill_before_writing_sdk_code(self) -> None:
        # 2026-05-28: real-chat validation surfaced the agent skipping
        # `load_skill strategy-definition-authoring` when the user framed
        # "write a minimal strategy and sdk validate" as a lightweight test
        # task, then hallucinating MarketDataProvider / ctx.indicators.is_hammer
        # / on_bar(self) -> None against the actual SDK shape. The hard rule
        # below pins the load_skill prerequisite to "outputting SDK code" not
        # "persisting a definition", so the agent cannot wriggle out by
        # claiming the task is "just validation". This test makes sure the
        # rule (and its anti-hallucination examples) cannot silently vanish
        # in future prompt rewrites.
        text = get_prompt_template_text("main-agent")

        # The hard rule itself.
        self.assertIn("策略 SDK 表面禁止凭训练数据猜", text)
        # The trigger predicate — should mention the syntactic markers that
        # bind the rule to "writing SDK code" rather than to user intent words.
        self.assertIn("from doyoutrade.strategy_sdk import", text)
        self.assertIn("class X(Strategy):", text)
        self.assertIn("populate_indicators", text)
        # The non-exemption examples (these phrases must NOT be loopholes).
        self.assertIn("sdk validate 一下就行", text)
        self.assertIn("在内存里 validate", text)
        # Known anti-hallucinations the rule lists explicitly.
        self.assertIn("MarketDataProvider", text)
        self.assertIn("ctx.indicators.is_hammer", text)
        self.assertIn("on_bar(self, df, ctx) -> Signal", text)

    def test_main_agent_prompt_explains_skill_persistence_across_compaction(self) -> None:
        # 2026-05-28 follow-up to test_main_agent_prompt_forces_load_skill_before_writing_sdk_code:
        # PR-C makes load_skill content survive context compaction by re-injecting
        # it as a <system-reminder> on the next turn. Without this guidance the
        # agent re-invokes load_skill defensively every time it loses the
        # original tool_result to a compaction boundary (the 6-call regression
        # captured during PR-B real-chat validation). The text below must pin
        # the rule + the two legitimate "re-load" exceptions so future prompt
        # rewrites can't silently drop them.
        text = get_prompt_template_text("main-agent")

        # The new bullet header.
        self.assertIn("已加载 skill 跨 compaction 自动驻留", text)
        # Names the persistence table so an operator reading the prompt can
        # cross-reference the implementation.
        self.assertIn("assistant_loaded_skills", text)
        # Re-injection mechanism is named explicitly so the agent connects the
        # rule to the system-reminder it sees post-compaction.
        self.assertIn("<system-reminder>", text)
        # The two legitimate re-load scenarios are pinned to prevent erosion
        # into a vague "use your judgment".
        self.assertIn("本会话从来没加载过该 skill", text)
        # Drift case — match on substring without depending on exact line break.
        self.assertRegex(text, r"SKILL\.md\s+在磁盘上被改过")

    def test_main_agent_prompt_mentions_cli_agent_management_and_validation(self) -> None:
        text = get_prompt_template_text("main-agent")

        self.assertIn("doyoutrade-cli assistant agent list|get|create|update|clone|delete", text)
        self.assertIn("doyoutrade-cli assistant run", text)
        self.assertIn("--add-skill", text)
        self.assertIn("--remove-skill", text)
        self.assertIn("--tool-config", text)
        self.assertIn("--compaction-mode", text)

    def test_main_agent_prompt_size_stays_bounded(self) -> None:
        text = get_prompt_template_text("main-agent")

        # Keep the main-agent prompt compact enough that first-turn requests do
        # not start from a massive prompt budget before any user context lands.
        # Bound raised 32000 -> 33000 (2026-07) to admit the render_panel
        # in-process visualization tool: it must appear in the always-loaded
        # in-process tool list (otherwise the model never discovers it and the
        # capability is dead); the block-schema detail lives in the on-demand
        # doyoutrade-data skill, so only a concise trigger/summary is in-prompt.
        self.assertLess(len(text), 33000)
