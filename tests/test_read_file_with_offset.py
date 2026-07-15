# tests/test_read_file_with_offset.py
"""Tests for ReadFileTool offset/limit support (text files, byte-based)."""
import os
import tempfile
import unittest
from pathlib import Path


class ReadFileToolOffsetLimitTests(unittest.TestCase):
    def setUp(self):
        # Create a temp file with known content
        self.temp_dir = tempfile.mkdtemp()
        self.temp_file = Path(self.temp_dir) / "test.txt"
        self.content = "a" * 1000  # 1000 'a' characters
        self.temp_file.write_text(self.content, encoding="utf-8")

    def tearDown(self):
        os.remove(self.temp_file)
        os.rmdir(self.temp_dir)

    def _read(self, path, offset=0, limit=50000):
        """Helper to call ReadFileTool and return the result dict."""
        from doyoutrade.tools.file_tools import ReadFileTool
        tool = ReadFileTool()
        return tool.execute(file_path=path, offset=offset, limit=limit)

    def test_read_file_with_offset_and_limit(self):
        """ReadFileTool should support offset and limit parameters."""
        # Call with offset=100, limit=200 - should return 200 'a' starting at position 100
        resp = self._read(str(self.temp_file), offset=100, limit=200)

        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["char_count"], 200)

    def test_read_file_with_offset_only(self):
        """ReadFileTool should support offset without explicit limit."""
        # Call with offset=500 - should return 500 'a' (1000 - 500 = 500 remaining)
        resp = self._read(str(self.temp_file), offset=500)

        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["char_count"], 500)

    def test_read_file_with_limit_only(self):
        """ReadFileTool should support limit without offset (reads from start)."""
        # Call with limit=50 - should return first 50 'a'
        resp = self._read(str(self.temp_file), limit=50)

        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["char_count"], 50)

    def test_read_file_offset_exceeds_file_size(self):
        """ReadFileTool should return empty text when offset exceeds file size."""
        resp = self._read(str(self.temp_file), offset=2000, limit=100)

        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["char_count"], 0)

    def test_read_file_limit_exceeds_remaining(self):
        """ReadFileTool should return only remaining content when limit exceeds remaining."""
        # File has 1000 chars, offset=900, limit=200 should return 100 chars
        resp = self._read(str(self.temp_file), offset=900, limit=200)

        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["char_count"], 100)


if __name__ == "__main__":
    unittest.main()
