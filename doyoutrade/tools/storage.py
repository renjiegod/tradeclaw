# doyoutrade/tools/storage.py
from __future__ import annotations

import json
import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PREVIEW_SIZE = 2000
TOOL_RESULTS_SUBDIR = "tool-results"


class ToolResultStorage:
    def __init__(self, session_id: str, base_dir: Path | None = None):
        if base_dir is None:
            home = Path.home()
            base_dir = home / ".doyoutrade" / "sessions" / session_id / TOOL_RESULTS_SUBDIR
        self._base_dir = base_dir
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _filepath(self, tool_use_id: str) -> Path:
        return self._base_dir / f"{tool_use_id}.json"

    async def persist(self, tool_use_id: str, content: str) -> tuple[str, str]:
        """Persist content to disk. Returns (filepath, preview)."""
        preview = content[:PREVIEW_SIZE]
        filepath = self._filepath(tool_use_id)
        record = {
            "tool_use_id": tool_use_id,
            "original_size": len(content),
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "content": content,
        }
        try:
            with filepath.open("w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False)
        except Exception as exc:
            logger.warning("Failed to persist tool result to %s: %s", filepath, exc)
            raise
        return str(filepath), preview

    def build_preview_message(self, tool_use_id: str, original_size: int, preview: str, filepath: str) -> str:
        size_str = self._format_size(original_size)
        return (
            f"{preview}\n"
            f"---\n"
            f"<persisted-output>\n"
            f"Output too large ({size_str}). Full output saved to: {filepath}\n"
            f"Use Read tool with path, offset, and limit parameters to access full content.\n"
            f"---\n"
        )

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size}B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f}KB"
        else:
            return f"{size / (1024 * 1024):.1f}MB"

    async def read(self, tool_use_id: str, offset: int = 0, limit: int = 50000) -> dict[str, Any] | None:
        """Read persisted content with offset/limit. Returns dict or None if not found."""
        filepath = self._filepath(tool_use_id)
        if not filepath.exists():
            return None
        try:
            with filepath.open(encoding="utf-8") as f:
                record = json.load(f)
            content = record.get("content", "")
            return {
                "tool_use_id": tool_use_id,
                "original_size": record["original_size"],
                "content": content[offset : offset + limit],
                "offset": offset,
                "limit": limit,
            }
        except Exception as exc:
            logger.warning("Failed to read tool result from %s: %s", filepath, exc)
            return None
