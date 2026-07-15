from __future__ import annotations

import json
import math
from typing import Any


def _safe_json_dumps(value: Any) -> str:
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


def _stringify_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            else:
                parts.append(_safe_json_dumps(item))
        return "\n".join(parts)
    if isinstance(value, dict):
        return _safe_json_dumps(value)
    return str(value)


def _estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 3))


def estimate_message_tokens(message: Any) -> int:
    total = 8
    total += _estimate_text_tokens(_stringify_content(getattr(message, "content", "")))

    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id:
        total += 6 + _estimate_text_tokens(str(tool_call_id))

    for tool_call in getattr(message, "tool_calls", None) or []:
        total += 12
        name = getattr(tool_call, "name", None)
        if name is None and isinstance(tool_call, dict):
            name = tool_call.get("name") or tool_call.get("function", {}).get("name")
        if name:
            total += _estimate_text_tokens(str(name))

        args = getattr(tool_call, "args", None)
        if args is None and isinstance(tool_call, dict):
            args = tool_call.get("args")
            if args is None:
                args = tool_call.get("function", {}).get("arguments")
        total += _estimate_text_tokens(_stringify_content(args))

    return total


def estimate_messages_tokens(messages: list[Any]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)
