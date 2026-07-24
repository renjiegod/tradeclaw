"""Pin the front-of-skill checklist for doyoutrade-data.

2026-07-24: agents that skip the long SKILL.md body still need a short,
high-signal checklist for the most common CLI footguns (--symbol vs
--symbols, 60m interval, render_panel for K-line, truncated CSV stats).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


_SKILL = (
    Path(__file__).resolve().parents[1]
    / ".doyoutrade"
    / "skills"
    / "doyoutrade-data"
    / "SKILL.md"
)


class DoyoutradeDataSkillChecklistTests(unittest.TestCase):
    def test_skill_file_exists(self) -> None:
        self.assertTrue(_SKILL.is_file(), f"missing {_SKILL}")

    def test_checklist_appears_before_commands_section(self) -> None:
        text = _SKILL.read_text(encoding="utf-8")
        checklist_idx = text.index("## Quick checklist")
        commands_idx = text.index("## Commands")
        self.assertLess(
            checklist_idx,
            commands_idx,
            "Quick checklist must sit above ## Commands",
        )

    def test_checklist_covers_common_footguns(self) -> None:
        text = _SKILL.read_text(encoding="utf-8")
        # Only assert against the checklist block so later sections can
        # still document the same topics at length.
        block = text[text.index("## Quick checklist") : text.index("## Commands")]

        self.assertIn("--symbol", block)  # forbidden / wrong flag callout
        self.assertIn("--symbols", block)
        self.assertIn("60m", block)
        self.assertIn("render_panel", block)
        self.assertIn("schema", block)
        self.assertIn("stock lookup", block)
        # Truncated artifact reads must not be invented into stats.
        self.assertTrue(
            re.search(r"truncat|截断|省略", block, flags=re.IGNORECASE),
            "checklist must warn against inventing stats from truncated reads",
        )
