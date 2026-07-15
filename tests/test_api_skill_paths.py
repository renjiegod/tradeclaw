"""Path-sandbox helpers for the /skills API."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from doyoutrade.api._skill_paths import (
    SkillPathError,
    resolve_skill_root,
    resolve_inside,
    detect_mime,
)


class SkillPathSandboxTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "alpha").mkdir()
        (self.root / "alpha" / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: x\n---\n# alpha\n", encoding="utf-8"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolve_skill_root_ok(self):
        self.assertEqual(
            resolve_skill_root(self.root, "alpha"),
            (self.root / "alpha").resolve(),
        )

    def test_resolve_skill_root_escape_rejected(self):
        with self.assertRaises(SkillPathError):
            resolve_skill_root(self.root, "../etc")
        with self.assertRaises(SkillPathError):
            resolve_skill_root(self.root, "/etc")
        with self.assertRaises(SkillPathError):
            resolve_skill_root(self.root, "")

    def test_resolve_inside_ok(self):
        skill_root = (self.root / "alpha").resolve()
        self.assertEqual(
            resolve_inside(skill_root, "SKILL.md"),
            (skill_root / "SKILL.md").resolve(),
        )

    def test_resolve_inside_rejects_traversal(self):
        skill_root = (self.root / "alpha").resolve()
        for bad in ("..", "../beta", "/abs", "", "a\x00b"):
            with self.assertRaises(SkillPathError):
                resolve_inside(skill_root, bad)

    def test_resolve_inside_rejects_symlink_escape(self):
        outside = self.root / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        link = self.root / "alpha" / "evil"
        os.symlink(outside, link)
        skill_root = (self.root / "alpha").resolve()
        with self.assertRaises(SkillPathError):
            resolve_inside(skill_root, "evil")

    def test_detect_mime(self):
        self.assertEqual(detect_mime(Path("a.md")), "text/markdown")
        self.assertTrue(detect_mime(Path("a.py")).startswith("text/"))
        self.assertEqual(detect_mime(Path("a.png")), "image/png")


if __name__ == "__main__":
    unittest.main()
