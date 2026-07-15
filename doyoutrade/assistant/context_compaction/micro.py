from __future__ import annotations

import json
from typing import Any

# Tool names whose ``ToolMessage`` payload must reach the model intact.
# Mirrors ``OperationHandler.bypass_result_truncation`` on the registry side —
# kept as a small static set here because ``micro_compact_messages`` runs
# without a registry handle. Tools added to this set must also set the
# class flag so the registry's disk-spill is bypassed too.
NO_COMPACT_TOOL_NAMES: frozenset[str] = frozenset(
    {"load_skill", "run_strategy_backtest"}
)


def _stringify_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                default=lambda item: str(item),
            )
        except TypeError:
            return str(value)


def _clone_tool_message(message: Any, content: str) -> Any:
    model_copy = getattr(message, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"content": content}, deep=True)

    model_dump = getattr(message, "model_dump", None)
    if callable(model_dump):
        data = model_dump()
        data["content"] = content
        return type(message)(**data)

    clone_kwargs = {
        "content": content,
        "tool_call_id": getattr(message, "tool_call_id"),
    }
    for field_name in (
        "companion_user_text",
        "artifact",
        "status",
        "additional_kwargs",
        "response_metadata",
        "name",
        "id",
    ):
        field_value = getattr(message, field_name, None)
        if field_value is not None:
            clone_kwargs[field_name] = field_value
    return type(message)(**clone_kwargs)


def _compact_tool_content(content: str, *, tool_result_max_chars: int) -> str:
    if len(content) <= tool_result_max_chars:
        return content
    if tool_result_max_chars <= 0:
        return ""

    full_marker_template = (
        "\n... [truncated tool result: omitted {omitted} chars, original size {original} chars]"
    )
    min_preview_chars = 1
    omitted = len(content) - min_preview_chars
    full_marker = full_marker_template.format(omitted=omitted, original=len(content))
    if tool_result_max_chars > len(full_marker):
        preview_budget = max(min_preview_chars, tool_result_max_chars - len(full_marker))
        preview = content[:preview_budget].rstrip() or content[:min_preview_chars]
        omitted = len(content) - len(preview)
        full_marker = full_marker_template.format(omitted=omitted, original=len(content))
        candidate = f"{preview}{full_marker}"
        if len(candidate) <= tool_result_max_chars:
            return candidate

    if tool_result_max_chars == 1:
        return content[:1]

    compact_marker = "..."
    preview_budget = max(min_preview_chars, tool_result_max_chars - len(compact_marker))
    preview = content[:preview_budget].rstrip() or content[:min_preview_chars]
    compact_suffix = compact_marker[: max(0, tool_result_max_chars - len(preview))]
    return f"{preview}{compact_suffix}"[:tool_result_max_chars]


def micro_compact_messages(messages: list[Any], *, tool_result_max_chars: int) -> list[Any]:
    compacted: list[Any] = []
    for message in messages:
        if getattr(message, "type", None) != "tool":
            compacted.append(message)
            continue
        # Skip compaction entirely when the tool opts out (e.g. load_skill
        # must surface the full SKILL.md body to the model).
        if getattr(message, "name", None) in NO_COMPACT_TOOL_NAMES:
            compacted.append(message)
            continue
        raw_content = getattr(message, "content", "")
        if isinstance(raw_content, str) and len(raw_content) <= tool_result_max_chars:
            compacted.append(_clone_tool_message(message, raw_content))
            continue
        if not isinstance(raw_content, str):
            try:
                if len(raw_content) <= tool_result_max_chars:  # type: ignore[arg-type]
                    compacted.append(_clone_tool_message(message, raw_content))
                    continue
            except TypeError:
                pass
        content = _stringify_content(raw_content)
        if len(content) <= tool_result_max_chars:
            compacted.append(_clone_tool_message(message, raw_content))
            continue
        compacted_content = _compact_tool_content(content, tool_result_max_chars=tool_result_max_chars)
        compacted.append(_clone_tool_message(message, compacted_content))
    return compacted
