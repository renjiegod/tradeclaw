from __future__ import annotations

import json
from typing import Any

from doyoutrade.money.decimal_helpers import json_sanitize
from doyoutrade.models.reasoning_tags import strip_reasoning_tags


def build_assistant_session_export(
    *,
    session: dict[str, Any] | None,
    agent: dict[str, Any] | None,
    messages: list[dict[str, Any]],
    events: list[dict[str, Any]],
    traces: dict[str, Any] | None,
    trace_details: list[dict[str, Any]],
    fmt: str,
    include_traces: bool,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    sanitized_session = json_sanitize(session)
    sanitized_agent = json_sanitize(agent)
    sanitized_messages = json_sanitize(list(messages or []))
    sanitized_events = json_sanitize(list(events or []))
    sanitized_raw_details = json_sanitize(list(trace_details or [])) if include_traces else []
    normalized_traces = _normalize_traces(json_sanitize(traces)) if include_traces else {"items": [], "total": 0}
    spans = _collect_spans(sanitized_raw_details)
    model_invocations = _collect_model_invocations(sanitized_raw_details)
    normalized_details = _summarize_trace_details(sanitized_raw_details)
    ids = _collect_ids(
        session=sanitized_session,
        events=sanitized_events,
        traces=normalized_traces,
        trace_details=sanitized_raw_details,
        spans=spans,
        model_invocations=model_invocations,
    )
    payload: dict[str, Any] = {
        "format": fmt,
        "session": sanitized_session,
        "agent": sanitized_agent,
        "messages": sanitized_messages,
        "events": sanitized_events,
        "traces": normalized_traces,
        "trace_details": normalized_details,
        "spans": spans,
        "model_invocations": model_invocations,
        "counts": {
            "messages": len(sanitized_messages),
            "events": len(sanitized_events),
            "traces": int(normalized_traces.get("total") or len(normalized_traces.get("items") or [])),
            "trace_details": len(normalized_details),
            "spans": len(spans),
            "model_invocations": len(model_invocations),
        },
        "ids": ids,
        "warnings": json_sanitize(list(warnings or [])),
    }
    if fmt == "markdown":
        payload["export_text"] = _render_markdown(payload)
    return payload


def _normalize_traces(traces: dict[str, Any] | None) -> dict[str, Any]:
    if not traces:
        return {"items": [], "total": 0}
    items = list(traces.get("items") or [])
    normalized = dict(traces)
    normalized["items"] = items
    normalized["total"] = int(traces.get("total") or len(items))
    return normalized


def _collect_spans(trace_details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for detail in trace_details:
        for span in detail.get("spans") or []:
            spans.append(span)
    return spans


def _collect_model_invocations(trace_details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    invocations: list[dict[str, Any]] = []
    for detail in trace_details:
        for invocation in detail.get("model_invocations") or []:
            invocations.append(invocation)
    return invocations


def _summarize_trace_details(trace_details: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for detail in trace_details:
        spans = list(detail.get("spans") or [])
        invocations = list(detail.get("model_invocations") or [])
        summary = {
            key: value
            for key, value in detail.items()
            if key not in {"spans", "model_invocations"}
        }
        summary["span_count"] = len(spans)
        summary["model_invocation_count"] = len(invocations)
        summaries.append(summary)
    return summaries


def _collect_ids(
    *,
    session: dict[str, Any] | None,
    events: list[dict[str, Any]],
    traces: dict[str, Any],
    trace_details: list[dict[str, Any]],
    spans: list[dict[str, Any]],
    model_invocations: list[dict[str, Any]],
) -> dict[str, Any]:
    run_ids: list[str] = []
    trace_ids: list[str] = []

    def add_run_id(value: Any) -> None:
        if value is not None and str(value) not in run_ids:
            run_ids.append(str(value))

    def add_trace_id(value: Any) -> None:
        if value is not None and str(value) not in trace_ids:
            trace_ids.append(str(value))

    for event in events or []:
        payload = event.get("payload") or {}
        if isinstance(payload, dict):
            add_run_id(payload.get("run_id"))
            add_trace_id(payload.get("trace_id"))

    for item in traces.get("items") or []:
        add_trace_id(item.get("trace_id"))

    for detail in trace_details:
        add_trace_id(detail.get("trace_id"))

    for span in spans:
        add_trace_id(span.get("trace_id"))
        attributes = span.get("attributes") or {}
        if isinstance(attributes, dict):
            add_run_id(attributes.get("doyoutrade.run_id") or attributes.get("run_id"))

    for invocation in model_invocations:
        add_run_id(invocation.get("run_id"))
        add_trace_id(invocation.get("trace_id"))

    return {
        "session_id": (session or {}).get("session_id"),
        "agent_id": (session or {}).get("agent_id"),
        "latest_attempt_id": (session or {}).get("last_attempt_id"),
        "run_ids": run_ids,
        "trace_ids": trace_ids,
    }


def _render_markdown(payload: dict[str, Any]) -> str:
    session = payload.get("session") or {}
    agent = payload.get("agent")
    lines = ["# Assistant Session Export", ""]
    lines.extend(_render_session(session))
    lines.extend(_render_agent(agent, session))
    lines.extend(_render_messages(payload.get("messages") or []))
    lines.extend(_render_events(payload.get("events") or []))
    lines.extend(_render_trace_summary(payload.get("traces") or {}, payload.get("trace_details") or []))
    lines.extend(_render_json_block("Spans", payload.get("spans") or []))
    lines.extend(_render_json_block("Model Invocations", payload.get("model_invocations") or []))
    warnings = payload.get("warnings") or []
    if warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_session(session: dict[str, Any]) -> list[str]:
    lines = ["## Session", ""]
    for key in (
        "session_id",
        "agent_id",
        "title",
        "status",
        "created_at",
        "updated_at",
        "last_attempt_id",
    ):
        value = session.get(key)
        if value is not None:
            lines.append(f"- {key}: `{value}`")
    config = session.get("config") or {}
    if isinstance(config, dict) and config.get("system_prompt_snapshot"):
        lines.extend(["", "### Session System Prompt Snapshot", "", "```text", str(config["system_prompt_snapshot"]), "```"])
    lines.append("")
    return lines


def _render_agent(agent: dict[str, Any] | None, session: dict[str, Any]) -> list[str]:
    lines = ["## Agent", ""]
    if agent is None:
        lines.extend(["No agent metadata available.", ""])
        return lines
    for key in ("id", "name", "status", "model_route_name", "max_turns"):
        value = agent.get(key)
        if value is not None:
            lines.append(f"- {key}: `{value}`")
    tool_names = agent.get("tool_names") or [cfg.get("name") for cfg in agent.get("tool_configs") or [] if cfg.get("name")]
    if tool_names:
        lines.append(f"- tools: `{', '.join(str(name) for name in tool_names)}`")
    if agent.get("skill_names"):
        lines.append(f"- skills: `{', '.join(str(name) for name in agent['skill_names'])}`")
    prompt = agent.get("resolved_system_prompt") or agent.get("system_prompt") or (session.get("config") or {}).get(
        "system_prompt_snapshot"
    )
    if prompt:
        lines.extend(["", "### Effective System Prompt", "", "```text", str(prompt), "```"])
    lines.append("")
    return lines


def _render_messages(messages: list[dict[str, Any]]) -> list[str]:
    lines = ["## Conversation", ""]
    if not messages:
        lines.extend(["No messages.", ""])
        return lines
    for idx, message in enumerate(messages, start=1):
        role = message.get("role", "message")
        created_at = message.get("created_at", "")
        attempt_id = message.get("linked_attempt_id")
        suffix = f" ({created_at})" if created_at else ""
        lines.append(f"### {idx}. {str(role).title()}{suffix}")
        if attempt_id:
            lines.append(f"- linked_attempt_id: `{attempt_id}`")
        if message.get("content"):
            # Defensive: older rows (or adapters that don't separate
            # reasoning_content) may still carry inline <think>...</think>
            # markup baked into the persisted text — split it back out so it
            # renders as a Thinking section instead of raw tags.
            visible_text, inline_thinking = strip_reasoning_tags(str(message["content"]))
            lines.extend(["", visible_text])
            if inline_thinking:
                lines.extend(["", "#### Thinking (inline)", "", inline_thinking])
        blocks = (message.get("metadata") or {}).get("content_blocks") or []
        if blocks:
            lines.extend(["", "#### Content Blocks"])
            lines.extend(_render_content_blocks(blocks))
        lines.append("")
    return lines


def _render_content_blocks(blocks: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type == "thinking":
            lines.extend(["", "#### Thinking", "", str(block.get("content") or "")])
        elif block_type == "tool_call":
            name = block.get("name") or "tool"
            lines.extend(["", f"#### Tool Call: `{name}`"])
            for key in ("tool_call_id", "status", "is_error"):
                if key in block:
                    lines.append(f"- {key}: `{block.get(key)}`")
            if "arguments" in block:
                lines.extend(["", "Arguments:", "", "```json", _to_json(block["arguments"]), "```"])
            if block.get("result_preview") is not None:
                lines.extend(["", "Result Preview:", "", "```text", _preview(block.get("result_preview")), "```"])
        elif block.get("content"):
            visible_text, inline_thinking = strip_reasoning_tags(str(block["content"]))
            lines.extend(["", visible_text])
            if inline_thinking:
                lines.extend(["", "#### Thinking (inline)", "", inline_thinking])
    return lines


def _render_events(events: list[dict[str, Any]]) -> list[str]:
    lines = ["## Events Timeline", ""]
    if not events:
        lines.extend(["No events.", ""])
        return lines
    for event in events:
        lines.append(
            f"- `{event.get('created_at', '')}` `{event.get('event_type', '')}` "
            f"`{event.get('event_id', '')}`"
        )
        payload = event.get("payload")
        if payload:
            lines.extend(["", "```json", _to_json(payload), "```", ""])
    return lines


def _render_trace_summary(traces: dict[str, Any], trace_details: list[dict[str, Any]]) -> list[str]:
    lines = ["## Trace Summary", ""]
    items = traces.get("items") or []
    if not items and not trace_details:
        lines.extend(["No trace data included.", ""])
        return lines
    for item in items:
        parts = [
            f"trace_id=`{item.get('trace_id')}`",
            f"span_name=`{item.get('span_name') or item.get('name')}`",
            f"status=`{item.get('status')}`",
        ]
        if item.get("duration_ms") is not None:
            parts.append(f"duration_ms=`{item.get('duration_ms')}`")
        lines.append("- " + " ".join(parts))
    lines.extend(["", "## Trace Details", ""])
    for detail in trace_details:
        lines.extend([f"### Trace `{detail.get('trace_id')}`", "", "```json", _to_json(detail), "```", ""])
    return lines


def _render_json_block(title: str, value: Any) -> list[str]:
    return [f"## {title}", "", "```json", _to_json(value), "```", ""]


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _preview(value: Any, *, limit: int = 4000) -> str:
    text = value if isinstance(value, str) else _to_json(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"
