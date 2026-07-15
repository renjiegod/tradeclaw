# tests/test_tool_result_storage.py
import asyncio
import unittest
from pathlib import Path
from doyoutrade.tools.storage import ToolResultStorage


class ToolResultStorageTests(unittest.TestCase):
    def test_persist_returns_preview_and_filepath(self):
        storage = ToolResultStorage(session_id="test-session-abc")
        tool_use_id = "get_task_123"
        content = "x" * 60000  # 60K chars, over 50K threshold

        filepath, preview = asyncio.run(storage.persist(tool_use_id, content))

        self.assertTrue(filepath.endswith(f"test-session-abc/tool-results/{tool_use_id}.json"))
        self.assertEqual(preview, "x" * 2000)  # first 2000 chars
        self.assertTrue(Path(filepath).exists())

    def test_persist_and_read_roundtrip(self):
        storage = ToolResultStorage(session_id="test-session-abc")
        content = "Hello world" * 100
        tool_use_id = "list_tasks_456"

        asyncio.run(storage.persist(tool_use_id, content))

        result = asyncio.run(storage.read(tool_use_id, offset=0, limit=50))

        self.assertIsNotNone(result)
        self.assertEqual(result["content"], content[:50])
        self.assertEqual(result["original_size"], len(content))

    def test_read_nonexistent_returns_none(self):
        storage = ToolResultStorage(session_id="test-session-abc")
        result = asyncio.run(storage.read("nonexistent_tool_xyz"))
        self.assertIsNone(result)

    def test_build_preview_message(self):
        storage = ToolResultStorage(session_id="test-session-abc")
        msg = storage.build_preview_message(
            "get_task_123",
            85401,  # 85401 / 1024 = 83.4KB (correct for binary division)
            "x" * 2000,
            "/home/user/.doyoutrade/sessions/test-session-abc/tool-results/get_task_123.json",
        )
        self.assertIn("x" * 2000, msg)
        self.assertIn("83.4KB", msg)
        self.assertIn("<persisted-output>", msg)
        self.assertIn("Read tool", msg)


if __name__ == "__main__":
    unittest.main()
