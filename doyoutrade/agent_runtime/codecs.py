from __future__ import annotations

import json
from typing import Any, Iterable

from doyoutrade.agent_runtime.types import AgentToolCall, AgentTurnResponse, ToolSpec


def tool_specs_for_provider(provider: str, specs: Iterable[ToolSpec]) -> list[dict[str, Any]]:
    """Translate neutral tool specs to provider-native tool definitions."""

    normalized = provider.strip().lower()
    if normalized in {"openai", "openai_compatible", "lmstudio"}:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description or spec.name,
                    "parameters": spec.parameters,
                },
            }
            for spec in specs
        ]
    if normalized == "anthropic":
        return [
            {
                "name": spec.name,
                "description": spec.description or spec.name,
                "input_schema": spec.parameters,
            }
            for spec in specs
        ]
    raise ValueError(f"unsupported provider for native tools: {provider!r}")


def agent_tool_specs_from_openai_tools(tools: Iterable[dict[str, Any]]) -> list[ToolSpec]:
    """Build neutral tool specs from OpenAI Chat Completions tool definitions."""

    out: list[ToolSpec] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if item.get("type") == "function" else None
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "")
        if not name:
            continue
        params = fn.get("parameters")
        out.append(
            ToolSpec(
                name=name,
                description=str(fn.get("description") or name),
                parameters=(
                    params
                    if isinstance(params, dict)
                    else {"type": "object", "properties": {}}
                ),
            )
        )
    return out


def agent_turn_response_from_model_response(response: Any) -> AgentTurnResponse:
    """Normalize a ``ModelResponse`` into the agent runtime turn shape."""

    raw = response.raw
    return AgentTurnResponse(
        content=response.text or _content_text(raw),
        tool_calls=_tool_calls_from_raw(raw),
        raw=raw,
        request_payload=response.invocation_request_payload,
        response_payload=response.invocation_response_payload,
        usage=_usage_from_raw(raw),
    )


def _content_text(raw: Any) -> str:
    content = getattr(raw, "content", "")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _tool_calls_from_raw(raw: Any) -> list[AgentToolCall]:
    calls = getattr(raw, "tool_calls", None) or []
    out: list[AgentToolCall] = []
    for tc in calls:
        name = getattr(tc, "name", None)
        if name is None and isinstance(tc, dict):
            name = tc.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        tc_id = getattr(tc, "id", None)
        if tc_id is None and isinstance(tc, dict):
            tc_id = tc.get("id")
        out.append(
            AgentToolCall(
                id=str(tc_id or ""),
                name=name,
                arguments=_tool_call_args_from_native(tc),
            )
        )
    return out


def _tool_call_args_from_native(tc: Any) -> dict[str, Any]:
    raw = getattr(tc, "args", None) or (tc.get("args") if isinstance(tc, dict) else None)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return {}
        return _unwrap_nested_tool_call_envelopes(parsed) if isinstance(parsed, dict) else {}
    if isinstance(raw, dict):
        return _unwrap_nested_tool_call_envelopes(raw)
    return {}


def _unwrap_nested_tool_call_envelopes(
    d: dict[str, Any],
    *,
    max_depth: int = 3,
) -> dict[str, Any]:
    cur: dict[str, Any] = dict(d)
    allowed_outer = frozenset({"name", "arguments", "id", "type"})
    for _ in range(max_depth):
        inner = cur.get("arguments")
        if not isinstance(inner, dict):
            break
        nm = cur.get("name")
        if not isinstance(nm, str) or not nm.strip():
            break
        if not set(cur.keys()).issubset(allowed_outer):
            break
        cur = dict(inner)
    return cur


def _usage_from_raw(raw: Any) -> dict[str, int | None]:
    usage = getattr(raw, "usage_metadata", None) or {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    total_tokens = usage.get("total_tokens")
    if (
        total_tokens is None
        and isinstance(input_tokens, int)
        and isinstance(output_tokens, int)
    ):
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens if isinstance(input_tokens, int) else None,
        "output_tokens": output_tokens if isinstance(output_tokens, int) else None,
        "total_tokens": total_tokens if isinstance(total_tokens, int) else None,
    }
