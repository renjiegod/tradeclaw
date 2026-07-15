"""Tests for the flat-root skills loader (no public/custom split)."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from doyoutrade.skills.loader import load_skills


SKILL_BODY = """---
name: alpha
description: an alpha skill
---

# Alpha
"""

BETA_BODY = """---
name: beta
description: a beta skill
---

# Beta
"""


class FlatSkillsLoaderTest(unittest.TestCase):
    def test_load_skills_from_flat_root(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "alpha").mkdir()
            (root / "alpha" / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
            (root / "beta").mkdir()
            (root / "beta" / "SKILL.md").write_text(BETA_BODY, encoding="utf-8")

            skills = load_skills(root)

            self.assertEqual([s.name for s in skills], ["alpha", "beta"])
            self.assertFalse(hasattr(skills[0], "category"))

    def test_skips_state_yaml_and_dot_dirs(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "skills_state.yaml").write_text("disabled: []", encoding="utf-8")
            (root / ".hidden").mkdir()
            (root / ".hidden" / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
            (root / "alpha").mkdir()
            (root / "alpha" / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")

            skills = load_skills(root)

            self.assertEqual([s.name for s in skills], ["alpha"])


if __name__ == "__main__":
    unittest.main()
