# tests/test_strategy_storage.py
import shutil
import tempfile
import unittest
from pathlib import Path

from doyoutrade.persistence.strategy_storage import (
    StrategyStorage,
    SandboxViolation,
)


class StrategyStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.storage = StrategyStorage(self.tmp / "strategies")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def test_open_draft_from_scratch_writes_scaffold(self) -> None:
        work_dir = self.storage.open_draft("sd-123", "sess-a", base_version=None)
        self.assertTrue((work_dir / "strategy.py").exists())
        body = (work_dir / "strategy.py").read_text()
        self.assertIn("class Strategy", body)

    def test_open_draft_copies_base_version(self) -> None:
        first = self.storage.open_draft("sd-123", "sess-a", base_version=None)
        (first / "strategy.py").write_text("class Strategy:\n    pass\n# v1")
        version_label, _ = self.storage.finalize_draft("sd-123", "sess-a")

        next_dir = self.storage.open_draft("sd-123", "sess-b", base_version=version_label)
        self.assertIn("# v1", (next_dir / "strategy.py").read_text())

    def test_finalize_promotes_atomically_and_returns_version(self) -> None:
        work = self.storage.open_draft("sd-x", "sess-1", base_version=None)
        (work / "strategy.py").write_text("class Strategy:\n    pass\n")
        version_label, code_hash = self.storage.finalize_draft("sd-x", "sess-1")
        self.assertTrue(version_label.startswith("v0001-"))
        self.assertEqual(len(code_hash), 16)
        promoted = self.storage.version_dir("sd-x", version_label)
        self.assertTrue((promoted / "strategy.py").exists())
        self.assertFalse(self.storage.draft_dir("sd-x", "sess-1").exists())

    def test_finalize_versions_increment(self) -> None:
        for i in range(3):
            self.storage.open_draft("sd-y", f"sess-{i}", base_version=None)
            self.storage.finalize_draft("sd-y", f"sess-{i}")
        versions = sorted(p.name for p in (self.storage.definition_dir("sd-y") / "versions").iterdir())
        self.assertEqual([v[:5] for v in versions], ["v0001", "v0002", "v0003"])

    def test_cancel_removes_draft(self) -> None:
        self.storage.open_draft("sd-z", "sess-c", base_version=None)
        self.storage.cancel_draft("sd-z", "sess-c")
        self.assertFalse(self.storage.draft_dir("sd-z", "sess-c").exists())

    def test_resolve_sandbox_rejects_escape(self) -> None:
        work = self.storage.open_draft("sd-q", "sess-q", base_version=None)
        with self.assertRaises(SandboxViolation):
            self.storage.resolve_in_sandbox(work, "../../../etc/passwd")
        with self.assertRaises(SandboxViolation):
            self.storage.resolve_in_sandbox(work, str(self.tmp / "outside.py"))

    def test_resolve_sandbox_accepts_nested_relative(self) -> None:
        work = self.storage.open_draft("sd-q", "sess-q", base_version=None)
        (work / "helpers").mkdir()
        (work / "helpers" / "ma.py").write_text("X = 1\n")
        resolved = self.storage.resolve_in_sandbox(work, "helpers/ma.py")
        self.assertEqual(resolved.read_text(), "X = 1\n")

    def test_resolve_sandbox_rejects_symlink_escape(self) -> None:
        work = self.storage.open_draft("sd-q", "sess-q", base_version=None)
        outside = self.tmp / "secret.py"
        outside.write_text("secret = 1\n")
        (work / "link.py").symlink_to(outside)
        with self.assertRaises(SandboxViolation):
            self.storage.resolve_in_sandbox(work, "link.py")

    def test_compute_hash_stable_across_runs(self) -> None:
        work = self.storage.open_draft("sd-h", "sess-h", base_version=None)
        (work / "strategy.py").write_text("class Strategy:\n    pass\n")
        (work / "helpers.py").write_text("PI = 3.14\n")
        h1 = self.storage.compute_hash(work)
        h2 = self.storage.compute_hash(work)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)

    def test_finalize_with_no_files_rejected(self) -> None:
        self.storage.open_draft("sd-e", "sess-e", base_version=None)
        # Remove the scaffold so it's empty
        (self.storage.draft_dir("sd-e", "sess-e") / "strategy.py").unlink()
        from doyoutrade.persistence.strategy_storage import EmptyDraft
        with self.assertRaises(EmptyDraft):
            self.storage.finalize_draft("sd-e", "sess-e")

    def test_list_files_excludes_generated_bytecode(self) -> None:
        # CPython writes __pycache__/<mod>.pyc into the version dir whenever the
        # strategy is imported; it must never surface as a "source file" (binary
        # bytecode decoded as UTF-8 is the 乱码 seen in the source viewer).
        work = self.storage.open_draft("sd-pyc", "sess-pyc", base_version=None)
        (work / "helpers.py").write_text("PI = 3.14\n")
        cache = work / "__pycache__"
        cache.mkdir()
        (cache / "strategy.cpython-312.pyc").write_bytes(b"\x00\x01\x02\xff\xfe")
        (work / ".DS_Store").write_bytes(b"\x00\x00")

        listed = self.storage.list_files(work)
        self.assertIn("strategy.py", listed)
        self.assertIn("helpers.py", listed)
        self.assertFalse(
            any("__pycache__" in p or p.endswith(".pyc") or p.endswith(".DS_Store") for p in listed),
            f"generated artifacts leaked into listing: {listed}",
        )

    def test_compute_hash_ignores_generated_bytecode(self) -> None:
        work = self.storage.open_draft("sd-pych", "sess-pych", base_version=None)
        (work / "strategy.py").write_text("class Strategy:\n    pass\n")
        before = self.storage.compute_hash(work)
        cache = work / "__pycache__"
        cache.mkdir()
        (cache / "strategy.cpython-312.pyc").write_bytes(b"\x00\x01\x02\xff\xfe")
        after = self.storage.compute_hash(work)
        self.assertEqual(before, after)

    def test_open_draft_raises_on_duplicate_session(self) -> None:
        from doyoutrade.persistence.strategy_storage import StrategyStorageError

        self.storage.open_draft("sd-dup", "sess-x", base_version=None)
        with self.assertRaises(StrategyStorageError):
            self.storage.open_draft("sd-dup", "sess-x", base_version=None)


if __name__ == "__main__":
    unittest.main()
