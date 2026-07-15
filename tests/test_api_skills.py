"""End-to-end tests for /skills CRUD endpoints."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from doyoutrade.api.skills import build_skills_router


SKILL_BODY = """---
name: alpha
description: an alpha skill
---

# Alpha

Hello.
"""


def _make_app(root: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(build_skills_router(lambda: root), prefix="")
    return app


class SkillsApiReadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "alpha").mkdir()
        (self.root / "alpha" / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
        (self.root / "alpha" / "references").mkdir()
        (self.root / "alpha" / "references" / "note.md").write_text("# note\n", encoding="utf-8")
        self.client = TestClient(_make_app(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def test_list_skills(self):
        r = self.client.get("/skills")
        self.assertEqual(r.status_code, 200)
        items = r.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["folder_name"], "alpha")
        self.assertEqual(items[0]["frontmatter"]["name"], "alpha")
        self.assertTrue(items[0]["enabled"])

    def test_get_skill_detail(self):
        r = self.client.get("/skills/alpha")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["folder_name"], "alpha")
        names = {node["name"] for node in data["tree"]}
        self.assertIn("SKILL.md", names)
        self.assertIn("references", names)

    def test_get_file_text(self):
        r = self.client.get("/skills/alpha/files", params={"path": "SKILL.md"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["encoding"], "utf-8")
        self.assertIn("Hello.", data["content"])
        self.assertIn("mtime", data)
        self.assertEqual(data["mime"], "text/markdown")

    def test_get_file_path_escape_rejected(self):
        r = self.client.get("/skills/alpha/files", params={"path": "../outside"})
        self.assertEqual(r.status_code, 400)

    def test_get_unknown_skill_404(self):
        r = self.client.get("/skills/does-not-exist")
        self.assertEqual(r.status_code, 404)


class SkillsApiFileWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "alpha").mkdir()
        (self.root / "alpha" / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
        self.client = TestClient(_make_app(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def _mtime(self, p: Path) -> str:
        import datetime as _dt
        return _dt.datetime.fromtimestamp(p.stat().st_mtime, tz=_dt.timezone.utc).isoformat()

    def test_put_file_updates_content(self):
        skill_md = self.root / "alpha" / "SKILL.md"
        r = self.client.put(
            "/skills/alpha/files",
            params={"path": "SKILL.md"},
            json={"content": "---\nname: alpha\ndescription: x\n---\n\n# Hi\n", "encoding": "utf-8"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("# Hi", skill_md.read_text(encoding="utf-8"))

    def test_put_mtime_conflict_returns_409(self):
        skill_md = self.root / "alpha" / "SKILL.md"
        stale = "2000-01-01T00:00:00+00:00"
        r = self.client.put(
            "/skills/alpha/files",
            params={"path": "SKILL.md"},
            json={"content": "x", "encoding": "utf-8", "if_unmodified_since": stale},
        )
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertIn("mtime", body["detail"])

    def test_post_create_file(self):
        r = self.client.post(
            "/skills/alpha/files",
            json={"path": "references/note.md", "kind": "file", "content": "# note\n"},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue((self.root / "alpha" / "references" / "note.md").is_file())

    def test_post_create_dir(self):
        r = self.client.post(
            "/skills/alpha/files",
            json={"path": "scripts", "kind": "dir"},
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue((self.root / "alpha" / "scripts").is_dir())

    def test_post_create_conflict(self):
        r = self.client.post(
            "/skills/alpha/files",
            json={"path": "SKILL.md", "kind": "file", "content": "x"},
        )
        self.assertEqual(r.status_code, 409)

    def test_rename_file(self):
        (self.root / "alpha" / "old.md").write_text("x", encoding="utf-8")
        r = self.client.post(
            "/skills/alpha/files/rename",
            json={"from": "old.md", "to": "new.md"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((self.root / "alpha" / "new.md").is_file())

    def test_delete_file(self):
        target = self.root / "alpha" / "tmp.md"
        target.write_text("x", encoding="utf-8")
        r = self.client.delete("/skills/alpha/files", params={"path": "tmp.md"})
        self.assertEqual(r.status_code, 204)
        self.assertFalse(target.exists())

    def test_delete_skill_md_rejected(self):
        r = self.client.delete("/skills/alpha/files", params={"path": "SKILL.md"})
        self.assertEqual(r.status_code, 400)

    def test_rename_skill_md_rejected(self):
        r = self.client.post(
            "/skills/alpha/files/rename",
            json={"from": "SKILL.md", "to": "OTHER.md"},
        )
        self.assertEqual(r.status_code, 400)


class SkillsApiSkillLevelTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "alpha").mkdir()
        (self.root / "alpha" / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
        self.client = TestClient(_make_app(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_skill(self):
        r = self.client.post(
            "/skills",
            json={"folder_name": "beta", "name": "beta", "description": "b"},
        )
        self.assertEqual(r.status_code, 201, r.text)
        skill_md = self.root / "beta" / "SKILL.md"
        self.assertTrue(skill_md.is_file())
        body = skill_md.read_text(encoding="utf-8")
        self.assertIn("name: beta", body)
        self.assertIn("description: b", body)

    def test_create_skill_invalid_name(self):
        r = self.client.post(
            "/skills",
            json={"folder_name": "../bad", "name": "n", "description": "d"},
        )
        self.assertEqual(r.status_code, 400)

    def test_create_skill_conflict(self):
        r = self.client.post(
            "/skills",
            json={"folder_name": "alpha", "name": "n", "description": "d"},
        )
        self.assertEqual(r.status_code, 409)

    def test_rename_skill_folder(self):
        r = self.client.post(
            "/skills/alpha/rename",
            json={"new_folder_name": "alpha2"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((self.root / "alpha2" / "SKILL.md").is_file())
        self.assertFalse((self.root / "alpha").exists())
        self.assertEqual(r.json()["skill_id"], "alpha2")

    def test_delete_skill(self):
        r = self.client.delete("/skills/alpha")
        self.assertEqual(r.status_code, 204)
        self.assertFalse((self.root / "alpha").exists())

    def test_update_frontmatter(self):
        r = self.client.put(
            "/skills/alpha/frontmatter",
            json={"name": "alpha", "description": "updated"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = (self.root / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("description: updated", body)
        # body section preserved
        self.assertIn("# Alpha", body)

    def test_update_frontmatter_rename_updates_disabled(self):
        (self.root / "skills_state.yaml").write_text("disabled:\n  - alpha\n", encoding="utf-8")
        r = self.client.put(
            "/skills/alpha/frontmatter",
            json={"name": "alpha-v2", "description": "x"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        state = (self.root / "skills_state.yaml").read_text(encoding="utf-8")
        self.assertIn("alpha-v2", state)
        self.assertNotIn("- alpha\n", state)


class SkillsApiEnableDisableTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "alpha").mkdir()
        (self.root / "alpha" / "SKILL.md").write_text(SKILL_BODY, encoding="utf-8")
        self.client = TestClient(_make_app(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def test_disable_then_enable(self):
        r = self.client.post("/skills/alpha/disable")
        self.assertEqual(r.status_code, 200, r.text)
        state = (self.root / "skills_state.yaml").read_text(encoding="utf-8")
        self.assertIn("alpha", state)

        r = self.client.post("/skills/alpha/enable")
        self.assertEqual(r.status_code, 200, r.text)
        state = (self.root / "skills_state.yaml").read_text(encoding="utf-8")
        self.assertNotIn("- alpha\n", state)

    def test_disable_unknown_skill_404(self):
        r = self.client.post("/skills/does-not-exist/disable")
        self.assertEqual(r.status_code, 404)

    def test_cache_invalidated_on_write(self):
        with patch("doyoutrade.assistant.slash_commands.invalidate_skill_commands_cache") as mocked:
            r = self.client.post(
                "/skills",
                json={"folder_name": "beta", "name": "beta", "description": "b"},
            )
            self.assertEqual(r.status_code, 201, r.text)
            self.assertTrue(mocked.called)


if __name__ == "__main__":
    unittest.main()
