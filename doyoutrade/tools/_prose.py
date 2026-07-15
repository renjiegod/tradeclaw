"""Reusable prose-formatting helpers for ``ToolResult.text``.

Every assistant tool needs to render the same flavour of error / success
prose for the model. Centralising these helpers keeps ``[error:<code>]``
prefixes stable across tools (skill docs reference these codes as
contract tokens) and prevents each tool from inventing its own format.

Pair these helpers with :class:`doyoutrade.tools.ToolResult`:

    return ToolResult(
        text=format_error_text("missing_name", "name is required",
                               "pass a non-empty task name"),
        data={"status": "error", "error_code": "missing_name", ...},
        is_error=True,
    )
"""

from __future__ import annotations

import json as _json
from typing import Any, Iterable, Mapping


def format_error_text(
    error_code: str,
    message: str,
    hint: str | None = None,
) -> str:
    """Render the model-facing prose for an error result.

    The ``[error:<code>]`` prefix is the stable contract token that skill
    docs reference and that ``_tool_result_is_error`` falls back to when
    a tool returns a plain string. Append a separate ``Hint:`` line when
    we have a concrete recovery suggestion.
    """

    parts = [f"[error:{error_code}] {message}"]
    if hint:
        parts.append(f"Hint: {hint}")
    return "\n".join(parts)


def format_unknown_args(
    unknown: list[str],
    allowed: Iterable[str],
    suggested: Mapping[str, str] | None = None,
) -> str:
    """Render the prose for the ``unknown_arguments`` contract failure.

    The full list of allowed top-level keys is included so the model can
    self-correct without re-reading the schema; the ``suggested_path``
    map (when present) tells it the canonical nested location.
    """

    base = (
        f"Unknown arguments: {', '.join(unknown)}. "
        f"Allowed top-level keys: {', '.join(sorted(allowed))}."
    )
    if suggested:
        hints = "; ".join(f"{k} -> {v}" for k, v in suggested.items())
        base += f" Suggested rename: {hints}."
    return f"[error:unknown_arguments] {base}"


def append_json_payload(text: str, payload: Any) -> str:
    """Append ``payload`` as a fenced JSON block under ``text``.

    Info-retrieval tools (``get_*``, ``inspect_*``, debug-view fetches)
    used to ship their structured response on a side channel for the UI
    while leaving the model with a thin prose summary. With the single-
    channel design the model sees the same string the UI does, so dense
    payloads belong inside the result text. Wrapping them in a fenced
    JSON block keeps the prose header scan-able while preserving every
    field for the model to parse and the UI's
    ``renderToolResultPayload`` to render via ``JsonCodeBlock``.

    Use sparingly: confirmation-style tools (``create_*`` / ``update_*``
    / ``delete_*``) don't need this — their existing one-line prose is
    enough.
    """

    body = _json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return f"{text}\n\n```json\n{body}\n```"


__all__ = ("format_error_text", "format_unknown_args", "append_json_payload")
