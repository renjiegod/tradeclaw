"""Sandbox enforcement tests for the sandboxed file primitives.

Tests cover all error_code tokens documented in file_tools.py:
  read_file:    no sandbox (can read any path); file_not_found, invalid_path
  write_file:   path_outside_workspace
  edit_file:    old_string_not_unique, replace_all, old_string_not_found
  list_files:   no sandbox (can list any directory)

The write/edit tools use the module-level ``_sandbox`` registry.
Each test registers the work_dir as a sandbox root in setUp and
unregisters it in tearDown.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from doyoutrade.persistence.strategy_storage import StrategyStorage
from doyoutrade.tools import _sandbox
from doyoutrade.tools.file_tools import (
    EditFileTool,
    ListFilesTool,
    ReadFileTool,
    WriteFileTool,
)


class FileToolsSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.storage = StrategyStorage(self.tmp / "strategies")
        self.work_dir = self.storage.open_draft("sd-1", "sess-1", base_version=None)

        # Register the work_dir as an active sandbox root so write/edit tools accept it.
        _sandbox.register_sandbox(self.work_dir)

    def tearDown(self) -> None:
        _sandbox.unregister_sandbox(self.work_dir)
        shutil.rmtree(self.tmp)

    # ------------------------------------------------------------------
    # ReadFileTool — unrestricted (no sandbox)
    # ------------------------------------------------------------------

    def test_read_returns_content_with_line_numbers(self) -> None:
        (self.work_dir / "strategy.py").write_text("a = 1\nb = 2\n")
        tool = ReadFileTool()
        result = tool.execute(file_path=str(self.work_dir / "strategy.py"))
        self.assertEqual(result["status"], "ok")
        self.assertIn("1\ta = 1", result["content"])
        self.assertIn("2\tb = 2", result["content"])

    def test_read_outside_sandbox_succeeds(self) -> None:
        """read_file is unrestricted — it must work on paths outside any sandbox."""
        outside = self.tmp / "secret.py"
        outside.write_text("secret_value = 99\n")
        tool = ReadFileTool()
        result = tool.execute(file_path=str(outside))
        # Must succeed — no sandbox enforcement on read
        self.assertEqual(result["status"], "ok")
        self.assertIn("secret_value", result["content"])

    def test_read_rejects_relative_path(self) -> None:
        """read_file rejects relative paths with invalid_path."""
        tool = ReadFileTool()
        result = tool.execute(file_path="relative/path.py")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "invalid_path")

    def test_read_multiformat_py_has_line_numbers(self) -> None:
        """Python files return 1-indexed line-numbered content."""
        (self.work_dir / "strategy.py").write_text("x = 1\ny = 2\nz = 3\n")
        tool = ReadFileTool()
        result = tool.execute(file_path=str(self.work_dir / "strategy.py"))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["format"], "plain")
        self.assertIn("1\tx = 1", result["content"])
        self.assertIn("2\ty = 2", result["content"])
        self.assertIn("3\tz = 3", result["content"])

    def test_read_nonexistent_returns_file_not_found(self) -> None:
        tool = ReadFileTool()
        result = tool.execute(file_path=str(self.work_dir / "does_not_exist.py"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "file_not_found")

    # ------------------------------------------------------------------
    # WriteFileTool — sandbox-enforced
    # ------------------------------------------------------------------

    def test_write_creates_nested_file(self) -> None:
        tool = WriteFileTool()
        result = tool.execute(
            file_path=str(self.work_dir / "helpers" / "ma.py"),
            content="def sma(x, n): return x.rolling(n).mean()\n",
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue((self.work_dir / "helpers" / "ma.py").exists())

    def test_write_rejects_escape(self) -> None:
        # Attempt path traversal: resolve of work_dir/../../../escape.py
        # will land outside the registered sandbox root.
        escape_path = str(self.work_dir / ".." / ".." / ".." / "escape.py")
        tool = WriteFileTool()
        result = tool.execute(
            file_path=escape_path,
            content="boom",
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "path_outside_workspace")

    # ------------------------------------------------------------------
    # EditFileTool — sandbox-enforced
    # ------------------------------------------------------------------

    def test_edit_replaces_unique_substring(self) -> None:
        (self.work_dir / "strategy.py").write_text("a = 1\nb = 2\n")
        tool = EditFileTool()
        result = tool.execute(
            file_path=str(self.work_dir / "strategy.py"),
            old_string="b = 2",
            new_string="b = 99",
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("b = 99", (self.work_dir / "strategy.py").read_text())

    def test_edit_non_unique_old_string_rejected(self) -> None:
        (self.work_dir / "strategy.py").write_text("x = 1\nx = 1\n")
        tool = EditFileTool()
        result = tool.execute(
            file_path=str(self.work_dir / "strategy.py"),
            old_string="x = 1",
            new_string="x = 2",
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "old_string_not_unique")

    def test_edit_replace_all(self) -> None:
        (self.work_dir / "strategy.py").write_text("x = 1\nx = 1\n")
        tool = EditFileTool()
        result = tool.execute(
            file_path=str(self.work_dir / "strategy.py"),
            old_string="x = 1",
            new_string="x = 2",
            replace_all=True,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            (self.work_dir / "strategy.py").read_text(), "x = 2\nx = 2\n"
        )

    def test_edit_missing_old_string(self) -> None:
        (self.work_dir / "strategy.py").write_text("a = 1\n")
        tool = EditFileTool()
        result = tool.execute(
            file_path=str(self.work_dir / "strategy.py"),
            old_string="nope",
            new_string="ok",
        )
        self.assertEqual(result["error_code"], "old_string_not_found")

    # ------------------------------------------------------------------
    # ListFilesTool — unrestricted (no sandbox)
    # ------------------------------------------------------------------

    def test_list_returns_relative_tree(self) -> None:
        (self.work_dir / "helpers").mkdir(exist_ok=True)
        (self.work_dir / "helpers" / "ma.py").write_text("X = 1\n")
        tool = ListFilesTool()
        result = tool.execute(directory=str(self.work_dir))
        self.assertEqual(result["status"], "ok")
        paths = set(result["files"])
        self.assertIn("strategy.py", paths)
        self.assertIn("helpers/ma.py", paths)

    def test_list_outside_sandbox_succeeds(self) -> None:
        """list_files is unrestricted — it can list directories outside any sandbox."""
        # Create a directory outside the registered sandbox
        outside_dir = self.tmp / "outside"
        outside_dir.mkdir()
        (outside_dir / "foo.txt").write_text("hello\n")
        tool = ListFilesTool()
        result = tool.execute(directory=str(outside_dir))
        self.assertEqual(result["status"], "ok")
        self.assertIn("foo.txt", result["files"])

    def test_list_nonexistent_returns_file_not_found(self) -> None:
        tool = ListFilesTool()
        result = tool.execute(directory=str(self.tmp / "does_not_exist"))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "file_not_found")


class KnowledgeSandboxTests(unittest.TestCase):
    """The private knowledge base ``~/.doyoutrade/knowledge`` is a permanent
    write sandbox so ``write_file`` / ``edit_file`` accept it.  Behavioural
    write-gating lives in the prompt + skill, not here.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        # Point DOYOUTRADE_HOME at a temp dir so the real ~/.doyoutrade is untouched.
        self._env = mock.patch.dict(os.environ, {"DOYOUTRADE_HOME": str(self.tmp)})
        self._env.start()
        self.kb_root = _sandbox.register_knowledge_sandbox()

    def tearDown(self) -> None:
        _sandbox.unregister_sandbox(self.kb_root)
        self._env.stop()
        shutil.rmtree(self.tmp)

    def test_knowledge_root_honours_doyoutrade_home(self) -> None:
        self.assertEqual(self.kb_root, self.tmp / "knowledge")
        self.assertTrue(self.kb_root.is_dir())  # created on register

    def test_write_inside_knowledge_succeeds(self) -> None:
        target = self.kb_root / "journal" / "2026" / "2026-05-30.md"
        result = WriteFileTool().execute(
            file_path=str(target),
            content="# 2026-05-30 复盘\n",
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue(target.exists())

    def test_edit_inside_knowledge_succeeds(self) -> None:
        roles = self.kb_root / "symbols" / "roles.md"
        roles.parent.mkdir(parents=True, exist_ok=True)
        roles.write_text("600519.SH 龙头\n")
        result = EditFileTool().execute(
            file_path=str(roles),
            old_string="龙头",
            new_string="龙头 / 已建仓",
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("已建仓", roles.read_text())

    def test_write_outside_knowledge_rejected(self) -> None:
        """A path under DOYOUTRADE_HOME but outside knowledge/ must be rejected."""
        outside = self.tmp / "sessions" / "leak.md"
        result = WriteFileTool().execute(file_path=str(outside), content="boom")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "path_outside_workspace")

    def test_idempotent_registration(self) -> None:
        before = _sandbox.knowledge_root()
        again = _sandbox.register_knowledge_sandbox()
        self.assertEqual(again, before)


if __name__ == "__main__":
    unittest.main()
