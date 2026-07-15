"""Tests for ``LoadSkillTool`` — the response must expose the skill's
absolute base directory so the agent can resolve `references/...` and
`scripts/...` paths via the `read_file` tool.
"""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from doyoutrade.tools import LoadSkillTool


class TestLoadSkillTool(unittest.IsolatedAsyncioTestCase):
    def _make_skill(
        self,
        *,
        name: str = "strategy-authoring",
        skill_path: str = "strategy-authoring",
        skill_dir: Path = Path("/abs/.doyoutrade/skills/strategy-authoring"),
        body: str = "# Strategy Authoring\nSee `references/error-codes.md`.",
    ) -> MagicMock:
        skill = MagicMock(enabled=True)
        skill.name = name
        skill.skill_path = skill_path
        skill.skill_dir = skill_dir
        skill.body = body
        return skill

    async def test_returns_base_directory_and_body(self):
        skill = self._make_skill()
        with patch("doyoutrade.tools.load_skills", return_value=[skill]):
            result = await LoadSkillTool().execute(skill_name="strategy-authoring")

        text = result.text if hasattr(result, "text") else str(result)
        self.assertIn("Loaded skill 'strategy-authoring' from strategy-authoring.", text)
        self.assertIn(
            "Base directory: /abs/.doyoutrade/skills/strategy-authoring", text
        )
        self.assertIn("read_file", text)
        self.assertIn("--- SKILL.md ---", text)
        self.assertIn("See `references/error-codes.md`.", text)
        self.assertFalse(getattr(result, "is_error", False))

    async def test_matches_by_skill_path_too(self):
        skill = self._make_skill(name="display-name", skill_path="nested/dir")
        with patch("doyoutrade.tools.load_skills", return_value=[skill]):
            result = await LoadSkillTool().execute(skill_name="nested/dir")

        text = result.text if hasattr(result, "text") else str(result)
        self.assertIn("Loaded skill 'display-name' from nested/dir.", text)
        self.assertIn("Base directory:", text)

    async def test_returns_error_when_not_found(self):
        with patch("doyoutrade.tools.load_skills", return_value=[]):
            result = await LoadSkillTool().execute(skill_name="ghost")

        text = result.text if hasattr(result, "text") else str(result)
        self.assertTrue(getattr(result, "is_error", False))
        self.assertIn("[error:skill_not_found]", text)
        self.assertIn("ghost", text)


if __name__ == "__main__":
    unittest.main()
