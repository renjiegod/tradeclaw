"""Run an ``OperationHandler`` and translate the result into an envelope.

The CLI deliberately re-uses each tool's full input contract — same
``_enforce_kwargs_contract`` / ``_apply_schema_coercion`` /
``_apply_identifier_guards`` chain that the assistant uses in-process —
by calling ``tool.execute(**kwargs)`` directly. That keeps the contract
single-sourced: the same ``error_code`` tokens, repair hints, and
allowed-key lists land in the envelope's ``error`` block.

Phase 0+1 scope: read the session env vars and surface them in
``envelope.meta``; do not yet attempt to set up OpenTelemetry tracing or
propagate trace context from the parent process. Tools that emit debug
events via ``emit_debug_event`` no-op silently when no recording span is
present, which matches the standalone-CLI use case. A future phase will
hook trace context propagation so events surface back into the agent's
debug session.
"""

from __future__ import annotations

import inspect
import os
from typing import Any

from doyoutrade.cli._envelope import (
    EXIT_FAILURE,
    EXIT_OK,
    Meta,
    error_envelope,
    exit_code_for_error,
    extract_unknown_arguments_fields,
    parse_tool_result,
    success_envelope,
)
from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._trace import cli_trace_scope
from doyoutrade.tools import OperationHandler, ToolResult, adapt_sync_dict_to_tool_result


def read_session_meta() -> Meta:
    """Pull session context out of environment variables.

    ``execute_bash`` is responsible for setting these when it spawns a
    subprocess; running the CLI from a plain shell yields an empty
    ``Meta`` (and the envelope omits the ``meta`` key entirely).
    """

    return Meta(
        agent_id=os.environ.get("DOYOUTRADE_AGENT_ID") or None,
        session_id=os.environ.get("DOYOUTRADE_SESSION_ID") or None,
        debug_session_id=os.environ.get("DOYOUTRADE_DEBUG_SESSION_ID") or None,
        run_id=os.environ.get("DOYOUTRADE_RUN_ID") or None,
    )


def _autofill_session_kwargs(tool: OperationHandler, kwargs: dict[str, Any], meta: Meta) -> None:
    """Mirror the auto-fill that ``OperationRegistry.execute`` performs.

    Tools that flagged ``requires_calling_agent_id`` / ``requires_calling_session_id``
    expect those values to be present without the model having to recite
    them. The registry injects them from the calling session; the CLI does
    the same from ``Meta`` so tools called via the CLI behave identically
    to in-process invocations.
    """

    if getattr(tool, "requires_session_id", False) and meta.session_id and not kwargs.get("session_id"):
        kwargs["session_id"] = meta.session_id
    if (
        getattr(tool, "requires_calling_agent_id", False)
        and meta.agent_id
        and not kwargs.get("agent_id")
    ):
        kwargs["agent_id"] = meta.agent_id
    if (
        getattr(tool, "requires_calling_session_id", False)
        and meta.session_id
        and not kwargs.get("target_session_id")
    ):
        kwargs["target_session_id"] = meta.session_id


async def invoke_tool(
    tool: OperationHandler,
    kwargs: dict[str, Any],
    *,
    meta: Meta | None = None,
) -> tuple[dict[str, Any], int]:
    """Execute a tool and return ``(envelope_dict, exit_code)``.

    Caller is responsible for serializing/printing the envelope; this
    helper keeps the formatting concern out of business logic so tests
    can assert on the envelope dict directly. Exceptions from
    ``tool.execute`` are caught and rendered as an ``internal_error``
    envelope so the CLI never prints a bare Python traceback.
    """

    if meta is None:
        meta = read_session_meta()
    call_args = dict(kwargs)
    _autofill_session_kwargs(tool, call_args, meta)

    # cli_trace_scope re-attaches the CLI invocation to the agent's
    # OTel trace when TRACEPARENT is set by execute_bash. Without this,
    # tool calls inside the CLI emit operation_*.* events into the
    # void — the parent debug session never sees them.
    try:
        with cli_trace_scope(tool.name, meta):
            raw = tool.execute(**call_args)
            if inspect.isawaitable(raw):
                raw = await raw
    except TypeError as exc:
        # Most often: the model (or developer) passed an unexpected keyword.
        # Surface it as a validation error so the CLI exits with code 2.
        envelope = error_envelope(
            error_code="validation_error",
            error_type="TypeError",
            message=str(exc) or "tool rejected kwargs",
            meta=meta,
        )
        return envelope, exit_code_for_error("validation_error")
    except Exception as exc:
        envelope = error_envelope(
            error_code="internal_error",
            error_type=type(exc).__name__,
            message=str(exc) or f"{type(exc).__name__} (no message)",
            meta=meta,
        )
        return envelope, EXIT_FAILURE

    if isinstance(raw, dict):
        raw = adapt_sync_dict_to_tool_result(raw)
    if isinstance(raw, ToolResult):
        text = raw.text
        is_error = raw.is_error
    else:
        text = raw if isinstance(raw, str) else str(raw)
        is_error = bool(getattr(raw, "is_error", False))

    data_block, summary, error_info = parse_tool_result(text, is_error=is_error)

    if is_error:
        code = (error_info or {}).get("error_code", "tool_error")
        message = (error_info or {}).get("message", summary or "tool error")
        extra = _carry_extra_error_fields(data_block) or {}
        # Tools that render their contract error via ``format_unknown_args``
        # don't attach a JSON data block, so reverse-extract the structured
        # bits from the prose so the envelope is as actionable as the
        # in-process error dict.
        if code == "unknown_arguments":
            for key, value in extract_unknown_arguments_fields(message).items():
                extra.setdefault(key, value)
        envelope = error_envelope(
            error_code=code,
            error_type=(error_info or {}).get("error_type"),
            message=message,
            hint=(error_info or {}).get("hint"),
            repair_hints=(data_block or {}).get("repair_hints") if isinstance(data_block, dict) else None,
            extra=extra or None,
            meta=meta,
        )
        return envelope, exit_code_for_error(code)

    envelope = success_envelope(data_block, summary, meta=meta)
    return envelope, EXIT_OK


async def invoke_tool_api(
    tool_name: str,
    kwargs: dict[str, Any],
    *,
    meta: Meta | None = None,
    timeout_seconds: float | None = None,
) -> tuple[dict[str, Any], int]:
    """Execute an assistant tool on the API server and adapt it to a CLI envelope."""

    if meta is None:
        meta = read_session_meta()
    invoke_kwargs: dict[str, Any] = {
        "json": {"args": dict(kwargs or {})},
        "meta": meta,
        "not_found_error_code": "tool_not_found",
    }
    if timeout_seconds is not None:
        invoke_kwargs["timeout_seconds"] = timeout_seconds
    envelope, exit_code = await invoke_api(
        "POST",
        f"/assistant/tools/{tool_name}/execute",
        **invoke_kwargs,
    )
    if not envelope.get("ok"):
        return envelope, exit_code

    body = envelope.get("data")
    if not isinstance(body, dict):
        return error_envelope(
            error_code="api_response_invalid",
            error_type="InvalidToolResponse",
            message=f"API response for tool {tool_name} did not contain an object body",
            meta=meta,
        ), exit_code_for_error("api_response_invalid")

    text = body.get("text")
    if not isinstance(text, str):
        return error_envelope(
            error_code="api_response_invalid",
            error_type="InvalidToolResponse",
            message=f"API response for tool {tool_name} did not contain text",
            meta=meta,
        ), exit_code_for_error("api_response_invalid")
    is_error = bool(body.get("is_error", False))

    data_block, summary, error_info = parse_tool_result(text, is_error=is_error)
    if is_error:
        code = (error_info or {}).get("error_code", "tool_error")
        message = (error_info or {}).get("message", summary or "tool error")
        extra = _carry_extra_error_fields(data_block) or {}
        if code == "unknown_arguments":
            for key, value in extract_unknown_arguments_fields(message).items():
                extra.setdefault(key, value)
        adapted = error_envelope(
            error_code=code,
            error_type=(error_info or {}).get("error_type"),
            message=message,
            hint=(error_info or {}).get("hint"),
            repair_hints=(data_block or {}).get("repair_hints") if isinstance(data_block, dict) else None,
            extra=extra or None,
            meta=meta,
        )
        return adapted, exit_code_for_error(code)

    return success_envelope(data_block, summary, meta=meta), EXIT_OK


def _carry_extra_error_fields(data_block: dict[str, Any] | None) -> dict[str, Any] | None:
    """Forward useful error-shape fields the tool put in the fenced JSON.

    Tools like ``CreateTaskTool`` ship ``unknown``, ``allowed_top_level``,
    ``suggested_path``, ``candidates``, etc. inside the fenced data block.
    We forward the well-known ones so the CLI's error envelope is as
    actionable as the tool's in-process error dict.
    """

    if not isinstance(data_block, dict):
        return None
    carry_keys = (
        "unknown",
        "allowed_top_level",
        "suggested_path",
        "candidates",
        "field",
        "expected_kind",
        "actual_kind",
        "missing",
    )
    out: dict[str, Any] = {}
    for key in carry_keys:
        if key in data_block:
            out[key] = data_block[key]
    return out or None


__all__ = ["invoke_tool", "invoke_tool_api", "read_session_meta"]
