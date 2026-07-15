import json
import tempfile
import unittest
from pathlib import Path

from scripts.metrics_project_optout import (
    END_MARKER,
    START_MARKER,
    claude_transcript_matches,
    codex_transcript_matches,
    inject_opt_out_block,
)


class MetricsProjectOptOutTests(unittest.TestCase):
    def test_inject_opt_out_block_is_idempotent(self) -> None:
        project_root = "/tmp/project"
        original = (
            'SESSION_ID=$(extract \'.session_id\')\n'
            'TOOL_NAME=$(extract \'.tool_name\')\n'
            'CWD=$(extract \'.cwd\')\n'
            '[ -z "$CWD" ] && CWD=$(pwd 2>/dev/null || printf \'\')\n'
            'GIT_BRANCH=""\n'
        )
        first, changed = inject_opt_out_block(original, project_root)
        self.assertTrue(changed)
        self.assertIn(START_MARKER, first)
        self.assertIn(END_MARKER, first)
        self.assertIn(project_root, first)

        second, changed_again = inject_opt_out_block(first, project_root)
        self.assertFalse(changed_again)
        self.assertEqual(first, second)

    def test_codex_transcript_matcher_uses_session_meta_cwd(self) -> None:
        project_root = "/tmp/project"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "codex.jsonl"
            path.write_text(
                json.dumps({"type": "session_meta", "payload": {"cwd": project_root}}) + "\n"
            )
            self.assertTrue(codex_transcript_matches(path, project_root))
            self.assertFalse(codex_transcript_matches(path, "/tmp/other"))

    def test_claude_transcript_matcher_uses_top_level_cwd(self) -> None:
        project_root = "/tmp/project"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "claude.jsonl"
            path.write_text(json.dumps({"cwd": project_root}) + "\n")
            self.assertTrue(claude_transcript_matches(path, project_root))
            self.assertFalse(claude_transcript_matches(path, "/tmp/other"))


if __name__ == "__main__":
    unittest.main()
