"""Model-visible injection of structured attachments during history replay.

The persisted user ``content`` is the user's own text only; the absolute path
must be re-injected from ``metadata.attachments`` when rebuilding the model
conversation. Critically, the reconstructed *last* user turn must match the
live ``fallback_user_text`` exactly, or the tail-dedup in
``_conversation_messages_from_rows`` would append a duplicate final message.
"""

import unittest
import uuid

from doyoutrade.assistant import attachments as A
from doyoutrade.assistant.service import _conversation_messages_from_rows


class AttachmentHistoryInjectionTests(unittest.TestCase):
    def setUp(self):
        A.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        self._file_id = uuid.uuid4().hex + ".pdf"
        self._path = A.UPLOADS_DIR / self._file_id
        self._path.write_bytes(b"data")

    def tearDown(self):
        if self._path.exists():
            self._path.unlink()

    def _att(self):
        return {"file_id": self._file_id, "filename": "流水.pdf"}

    def test_last_turn_injects_path_without_duplicate(self):
        rows = [
            {"role": "user", "content": "分析这个文件", "metadata": {"attachments": [self._att()]}},
        ]
        fallback = A.compose_model_user_text("分析这个文件", [self._att()])
        msgs = _conversation_messages_from_rows(rows, fallback)

        # No duplicate final message: the single user row IS the last turn.
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].content, fallback)
        # The absolute path is injected for the model to read_file.
        self.assertIn(f"path: {self._path.resolve()}", msgs[0].content)
        # ...but the persisted content itself never carried the path.
        self.assertNotIn("path:", rows[0]["content"])

    def test_earlier_turn_attachment_path_survives_replay(self):
        rows = [
            {"role": "user", "content": "看看这个", "metadata": {"attachments": [self._att()]}},
            {"role": "assistant", "content": "好的，已读取。", "metadata": {}},
            {"role": "user", "content": "继续分析", "metadata": {}},
        ]
        msgs = _conversation_messages_from_rows(rows, "继续分析")

        self.assertEqual(len(msgs), 3)
        # Earlier user turn still exposes the path on replay.
        self.assertIn(f"path: {self._path.resolve()}", msgs[0].content)
        self.assertEqual(msgs[1].content, "好的，已读取。")
        # Latest turn has no attachment -> plain text, matches fallback (no dup).
        self.assertEqual(msgs[2].content, "继续分析")

    def test_user_turn_without_attachments_is_plain(self):
        rows = [{"role": "user", "content": "你好", "metadata": {}}]
        msgs = _conversation_messages_from_rows(rows, "你好")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].content, "你好")


if __name__ == "__main__":
    unittest.main()
