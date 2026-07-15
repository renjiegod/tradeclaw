from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from doyoutrade.tools import (
    OperationHandler,
    OperationRegistry,
    ToolResult,
    storage as storage_module,
)


class _BigToolResultTool(OperationHandler):
    """ToolResult whose text overflows the budget (e.g. an info-retrieval
    tool that inlines a JSON code block of its full payload)."""

    name = "big_toolresult_tool"
    description = "Returns a ToolResult with a deliberately oversized text."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, payload_chars: int = 5000) -> None:
        self._size = payload_chars

    async def execute(self, **_: Any) -> ToolResult:
        # Simulates "prose header + dense JSON code block" shape.
        text = "Result header.\n\n```json\n" + ("x" * self._size) + "\n```"
        return ToolResult(text=text)


class _BigTextTool(OperationHandler):
    """Returns a legacy bare-``str`` payload that overflows the budget."""

    name = "big_text_tool"
    description = "Returns a legacy str payload that overflows the budget."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    async def execute(self, **_: Any) -> str:
        return "x" * 5000


class _BypassTruncationTool(OperationHandler):
    """Opt-out tool whose oversized payload must reach the model intact."""

    name = "bypass_truncation_tool"
    description = "Returns an oversized payload that must skip disk-spill."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    bypass_result_truncation = True

    async def execute(self, **_: Any) -> ToolResult:
        return ToolResult(text="y" * 5000)


class RegistrySizeGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Force ToolResultStorage to write under a temp dir to keep the
        # test hermetic and to make assertions on the resulting file simple.
        self._tmp = Path(tempfile.mkdtemp(prefix="tool-result-storage-"))
        self._orig_base_dir = storage_module.ToolResultStorage.__init__

        tmp_root = self._tmp

        def _patched_init(inst, session_id: str, base_dir: Path | None = None) -> None:
            base = base_dir or (tmp_root / session_id / "tool-results")
            self._orig_base_dir(inst, session_id, base)

        storage_module.ToolResultStorage.__init__ = _patched_init  # type: ignore[assignment]

    def tearDown(self) -> None:
        storage_module.ToolResultStorage.__init__ = self._orig_base_dir  # type: ignore[assignment]
        shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_oversized_toolresult_text_spills_to_disk(self) -> None:
        registry = OperationRegistry(
            [_BigToolResultTool(payload_chars=5000)],
            tool_result_max_chars=500,
        )
        result = await registry.execute("big_toolresult_tool", {}, session_id="sess-toolresult")
        text = str(result)
        self.assertIn("<persisted-output>", text)
        self.assertIn("Output too large", text)
        # The model-facing preview must point at the persisted file so the
        # model knows where to look.
        self.assertIn("Use Read tool", text)
        # And the persisted file should actually exist under the tmp dir.
        persisted_files = list(self._tmp.rglob("*.json"))
        self.assertEqual(len(persisted_files), 1)

    async def test_legacy_str_tool_still_spills_text_to_disk(self) -> None:
        registry = OperationRegistry(
            [_BigTextTool()],
            tool_result_max_chars=200,
        )
        result = await registry.execute("big_text_tool", {}, session_id="sess-text")
        text = str(result)
        self.assertIn("<persisted-output>", text)
        self.assertIn("Output too large", text)

    async def test_bypass_truncation_tool_skips_disk_spill(self) -> None:
        registry = OperationRegistry(
            [_BypassTruncationTool()],
            tool_result_max_chars=500,
        )
        result = await registry.execute(
            "bypass_truncation_tool", {}, session_id="sess-bypass"
        )
        text = str(result)
        # Full payload must reach the model intact — no preview, no
        # persisted-output marker, no on-disk artifact.
        self.assertEqual(text, "y" * 5000)
        self.assertNotIn("<persisted-output>", text)
        persisted_files = list(self._tmp.rglob("*.json"))
        self.assertEqual(persisted_files, [])

    async def test_small_toolresult_passes_through_untouched(self) -> None:
        registry = OperationRegistry(
            [_BigToolResultTool(payload_chars=10)],
            tool_result_max_chars=10_000,
        )
        result = await registry.execute("big_toolresult_tool", {}, session_id="sess-small")
        text = str(result)
        self.assertNotIn("<persisted-output>", text)
        # And no file should be persisted.
        persisted_files = list(self._tmp.rglob("*.json"))
        self.assertEqual(persisted_files, [])


if __name__ == "__main__":
    unittest.main()
