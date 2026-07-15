from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:  # Prefer real LangChain message classes when installed.
    from langchain_core.messages import HumanMessage, SystemMessage
except Exception:  # pragma: no cover - dependency fallback for stripped test envs
    from doyoutrade.test_messages import HumanMessage, SystemMessage


@dataclass(frozen=True)
class FullCompactionPlan:
    compacted_rows: list[dict[str, Any]]
    tail_rows: list[dict[str, Any]]
    boundary_metadata: dict[str, Any] | None
    latest_boundary_row: dict[str, Any] | None = None


def _context_compaction_metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    payload = metadata.get("context_compaction")
    return dict(payload) if isinstance(payload, dict) else {}


def _is_summary_boundary(row: dict[str, Any]) -> bool:
    payload = _context_compaction_metadata(row)
    return payload.get("kind") == "summary_boundary"


def _boundary_source_message_count(row: dict[str, Any] | None) -> int:
    if not isinstance(row, dict):
        return 0
    payload = _context_compaction_metadata(row)
    try:
        return max(0, int(payload.get("source_message_count") or 0))
    except (TypeError, ValueError):
        return 0


def _tail_start_index(
    history_rows: list[dict[str, Any]],
    *,
    preserve_recent_messages: int,
    preserve_recent_tool_pairs: int,
) -> int:
    if not history_rows:
        return 0

    start = max(0, len(history_rows) - max(0, int(preserve_recent_messages)))
    if preserve_recent_tool_pairs <= 0:
        return start

    preserved_pairs = 0
    index = len(history_rows) - 1
    while index >= 0 and preserved_pairs < preserve_recent_tool_pairs:
        row = history_rows[index]
        if row.get("role") != "tool":
            index -= 1
            continue
        preserved_pairs += 1
        start = min(start, index)
        if index > 0:
            start = min(start, index - 1)
        index -= 1
    return max(0, start)


def build_summary_boundary_metadata(
    *, compacted_until_message_id: str, source_message_count: int
) -> dict[str, Any]:
    return {
        "context_compaction": {
            "kind": "summary_boundary",
            "strategy": "full",
            "compacted_until_message_id": compacted_until_message_id,
            "source_message_count": int(source_message_count),
        }
    }


def history_rows_after_latest_boundary(history_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_boundary_index = -1
    for index, row in enumerate(history_rows):
        if _is_summary_boundary(row):
            latest_boundary_index = index
    if latest_boundary_index < 0:
        return list(history_rows)
    return list(history_rows[latest_boundary_index:])


def _format_rows_for_summary(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        role = str(row.get("role") or "unknown")
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines).strip()


def build_summary_generation_messages(
    *,
    compacted_rows: list[dict[str, Any]],
    latest_boundary_row: dict[str, Any] | None,
) -> list[Any]:
    existing_summary = ""
    if latest_boundary_row is not None:
        existing_summary = str(latest_boundary_row.get("content") or "").strip()
    transcript = _format_rows_for_summary(compacted_rows)
    instructions = (
        "You summarize earlier assistant conversation context for reuse in later model turns. "
        "Write a concise factual summary that preserves user goals, decisions, constraints, "
        "open questions, and concrete outputs. Do not invent tool results or new instructions."
    )
    prompt_parts = []
    if existing_summary:
        prompt_parts.append("Existing summary:\n" + existing_summary)
    if transcript:
        prompt_parts.append("New conversation to fold in:\n" + transcript)
    prompt_parts.append("Return only the updated summary text.")
    return [
        SystemMessage(content=instructions),
        HumanMessage(content="\n\n".join(prompt_parts)),
    ]


async def generate_compaction_summary(
    *,
    adapter: Any,
    summary_messages: list[Any],
) -> str:
    response = await adapter.agent_turn(summary_messages, tools=None)
    return str(getattr(response, "content", "") or "").strip()


def build_full_compaction_plan(
    *,
    history_rows: list[dict[str, Any]],
    preserve_recent_messages: int,
    preserve_recent_tool_pairs: int,
) -> FullCompactionPlan:
    latest_boundary_row: dict[str, Any] | None = None
    boundary_index = -1
    for index, row in enumerate(history_rows):
        if _is_summary_boundary(row):
            latest_boundary_row = row
            boundary_index = index

    candidate_rows = history_rows[boundary_index + 1 :]
    if len(candidate_rows) <= max(0, int(preserve_recent_messages)):
        return FullCompactionPlan(
            compacted_rows=[],
            tail_rows=list(candidate_rows),
            boundary_metadata=None,
            latest_boundary_row=latest_boundary_row,
        )

    tail_start = _tail_start_index(
        candidate_rows,
        preserve_recent_messages=preserve_recent_messages,
        preserve_recent_tool_pairs=preserve_recent_tool_pairs,
    )
    compacted_rows = list(candidate_rows[:tail_start])
    tail_rows = list(candidate_rows[tail_start:])
    if not compacted_rows:
        return FullCompactionPlan(
            compacted_rows=[],
            tail_rows=tail_rows,
            boundary_metadata=None,
            latest_boundary_row=latest_boundary_row,
        )

    source_message_count = _boundary_source_message_count(latest_boundary_row) + len(compacted_rows)
    boundary_metadata = build_summary_boundary_metadata(
        compacted_until_message_id=str(compacted_rows[-1]["message_id"]),
        source_message_count=source_message_count,
    )
    return FullCompactionPlan(
        compacted_rows=compacted_rows,
        tail_rows=tail_rows,
        boundary_metadata=boundary_metadata,
        latest_boundary_row=latest_boundary_row,
    )
