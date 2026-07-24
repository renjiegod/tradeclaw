"""Tests for the flat-root skills loader (no public/custom split)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from doyoutrade.skills.loader import default_skills_root, find_project_root, load_skills


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


class DefaultSkillsRootEnvOverrideTest(unittest.TestCase):
    def test_env_override_wins(self):
        with TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"DOYOUTRADE_SKILLS_PATH": td}):
                self.assertEqual(default_skills_root(), Path(td))

    def test_env_override_expands_user(self):
        with mock.patch.dict(os.environ, {"DOYOUTRADE_SKILLS_PATH": "~/some-skills-dir"}):
            self.assertEqual(default_skills_root(), Path("~/some-skills-dir").expanduser())

    def test_no_env_falls_back_to_project_root(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOYOUTRADE_SKILLS_PATH", None)
            self.assertEqual(default_skills_root(), find_project_root() / ".doyoutrade" / "skills")


if __name__ == "__main__":
    unittest.main()
