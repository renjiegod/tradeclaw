from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from opentelemetry import trace as trace_api

from doyoutrade.money.decimal_helpers import json_default_with_decimals
from doyoutrade.observability.debug_span_export import ATTR_EVENT_PAYLOAD_JSON

debug_note_value: ContextVar[str | None] = ContextVar(
    "debug_note_value",
    default=None,
)

# Code-version context vars: set by the worker at cycle-start so that all
# debug events emitted during the cycle automatically carry the pinned version.
# Reset to None at cycle-end (context manager semantics via worker_code_version_scope).
cycle_code_version: ContextVar[str | None] = ContextVar("cycle_code_version", default=None)
cycle_code_hash: ContextVar[str | None] = ContextVar("cycle_code_hash", default=None)

# Central kill-switch for debug observability persistence/serialization.
# Defaults to True so every path (live ticks, debug sessions, assistant tools)
# behaves exactly as before. Only a non-debug backtest flips this to False to
# skip the expensive trace IO (debug_session_spans / span events / cycle_runs /
# model_invocations) while leaving business logic untouched. This is an
# intentional, *visible* relaxation — the run also records ``debug_enabled`` and
# logs once at start, so it is never a silent swallow of observability.
debug_observability_enabled: ContextVar[bool] = ContextVar(
    "debug_observability_enabled",
    default=True,
)


@contextmanager
def observability_disabled() -> Iterator[None]:
    """Disable debug observability persistence/serialization for the duration.

    Used by the non-debug backtest path to wrap the whole cycle loop. Within this
    scope :func:`emit_debug_event`, ``model_invocations`` recording and
    ``cycle_runs`` persistence are short-circuited.
    """
    token = debug_observability_enabled.set(False)
    try:
        yield
    finally:
        debug_observability_enabled.reset(token)


@contextmanager
def debug_session_scope(recorder: Any = None, *, debug_note: str | None = None) -> Iterator[None]:
    """Context manager for debug session. recorder argument is ignored (kept for API compat)."""
    note_token = debug_note_value.set(debug_note.strip() if isinstance(debug_note, str) and debug_note.strip() else None)
    try:
        yield
    finally:
        debug_note_value.reset(note_token)


@contextmanager
def worker_code_version_scope(code_version: str | None, code_hash: str | None) -> Iterator[None]:
    """Context manager that sets the pinned strategy code version for the duration of a cycle.

    All calls to :func:`emit_debug_event` / :func:`emit_debug_event_sync` within
    this scope will automatically include ``code_version`` + ``code_hash`` in the
    event payload, so the debug UI can correlate every event with the exact compiled
    version — even across nested async calls.
    """
    ver_token = cycle_code_version.set(code_version)
    hash_token = cycle_code_hash.set(code_hash)
    try:
        yield
    finally:
        cycle_code_version.reset(ver_token)
        cycle_code_hash.reset(hash_token)


def current_debug_note() -> str | None:
    return debug_note_value.get()


async def emit_debug_event(event_type: str, payload: dict[str, Any]) -> None:
    """Record an event on the current OpenTelemetry span (debug UI reads these from exported spans)."""
    import asyncio

    await asyncio.sleep(0)  # yield to event loop but never actually waits
    _emit_span_event(event_type, payload)


def emit_debug_event_sync(event_type: str, payload: dict[str, Any]) -> None:
    """Synchronous version of emit_debug_event for use in non-async contexts."""
    _emit_span_event(event_type, payload)


def _emit_span_event(event_type: str, payload: dict[str, Any]) -> None:
    """Internal function to emit span event (shared by both sync and async versions).

    If the current execution context has a pinned strategy code version
    (set via :func:`worker_code_version_scope`), injects ``code_version`` and
    ``code_hash`` into the payload so the debug UI can correlate every event
    with the exact compiled version.
    """
    if not debug_observability_enabled.get():
        return
    span = trace_api.get_current_span()
    if not span.is_recording():
        return
    # Inject pinned code version if set in this cycle's context.
    cv = cycle_code_version.get()
    ch = cycle_code_hash.get()
    if cv is not None or ch is not None:
        payload = dict(payload)
        if cv is not None:
            payload.setdefault("code_version", cv)
        if ch is not None:
            payload.setdefault("code_hash", ch)
    span.add_event(
        event_type,
        {ATTR_EVENT_PAYLOAD_JSON: json.dumps(payload, default=json_default_with_decimals, ensure_ascii=False)},
    )
