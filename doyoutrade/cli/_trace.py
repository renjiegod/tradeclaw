"""W3C tracecontext propagation for CLI subprocess invocations.

Closes the gap noted in Phase 0+1: the CLI subprocess emits
``operation_<name>.*`` debug events via ``emit_debug_event``, but
without a parent span those events were dropped silently. This module
re-attaches every CLI tool invocation to the agent's OpenTelemetry
trace by:

1. Reading ``TRACEPARENT`` / ``TRACESTATE`` env vars that
   ``execute_bash`` injects via ``opentelemetry.propagate.inject``.
2. Starting a child span ``cli.<tool_name>`` under the extracted parent
   context.
3. Activating ``debug_span_export_for_session(debug_session_id, "cli")``
   so the CLI span (and its events) are tagged for export into the
   ``debug_session_spans`` table the agent's debug UI reads from.

Standalone runs (no ``TRACEPARENT`` in env) still get a tracer span,
but the span has no parent and the export scope is skipped — so the
emitted events go nowhere harmful and the CLI exits cleanly even when
the OTel provider is the default no-op one.
"""

from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
from typing import Iterator

from opentelemetry import propagate, trace as trace_api

from doyoutrade.cli._envelope import Meta


_W3C_TRACEPARENT_ENV = "TRACEPARENT"
_W3C_TRACESTATE_ENV = "TRACESTATE"


def inject_traceparent_into_env(env: dict[str, str]) -> None:
    """Inject the current process's W3C tracecontext into ``env``.

    Called by ``execute_bash`` right before spawning the CLI subprocess.
    ``opentelemetry.propagate.inject`` writes to a carrier dict using
    the configured propagator (defaults include the W3C ``tracecontext``
    propagator that produces the ``traceparent`` / ``tracestate``
    keys). Empty carriers mean there is no current span — that's OK,
    standalone CLI runs handle absence gracefully.
    """

    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    if "traceparent" in carrier:
        env[_W3C_TRACEPARENT_ENV] = carrier["traceparent"]
    if "tracestate" in carrier:
        env[_W3C_TRACESTATE_ENV] = carrier["tracestate"]


def extract_parent_context():
    """Pull the parent context out of ``TRACEPARENT`` / ``TRACESTATE`` env.

    Returns ``None`` when neither var is set so callers can branch on
    "no parent" and skip span creation in standalone mode.
    """

    carrier: dict[str, str] = {}
    traceparent = os.environ.get(_W3C_TRACEPARENT_ENV)
    if traceparent:
        carrier["traceparent"] = traceparent
    tracestate = os.environ.get(_W3C_TRACESTATE_ENV)
    if tracestate:
        carrier["tracestate"] = tracestate
    if not carrier:
        return None
    return propagate.extract(carrier)


@contextmanager
def cli_trace_scope(tool_name: str, meta: Meta) -> Iterator[None]:
    """Wrap a CLI tool invocation in a child span linked to the agent's trace.

    The span is named ``cli.<tool_name>`` and carries the same
    ``doyoutrade.*`` attributes the agent's chat-flow spans use, so the
    debug UI can group CLI calls with their parent without special
    casing. When ``TRACEPARENT`` is absent the span still gets created
    (so local emitters don't silently no-op against a default span),
    but the debug-export scope is skipped because there is no session
    to anchor on.
    """

    parent_ctx = extract_parent_context()
    tracer = trace_api.get_tracer("doyoutrade.cli")

    if meta.debug_session_id:
        # Lazy import: bootstrap may not yet have run for commands that
        # don't touch the platform service (schema / stock lookup), so
        # the OTel provider is still the default no-op. The processor
        # registration is idempotent inside bootstrap, but the import
        # is cheap so we always do it and let the processor decide.
        from doyoutrade.observability.debug_span_export import debug_span_export_for_session

        export_cm = debug_span_export_for_session(meta.debug_session_id, span_source="cli")
    else:
        export_cm = nullcontext()

    span_kwargs: dict[str, object] = {}
    if parent_ctx is not None:
        span_kwargs["context"] = parent_ctx

    with export_cm:
        with tracer.start_as_current_span(f"cli.{tool_name}", **span_kwargs) as span:
            if meta.agent_id:
                span.set_attribute("doyoutrade.agent_id", meta.agent_id)
            if meta.session_id:
                span.set_attribute("doyoutrade.session_id", meta.session_id)
            if meta.run_id:
                span.set_attribute("doyoutrade.run_id", meta.run_id)
            if meta.debug_session_id:
                span.set_attribute("doyoutrade.debug_session_id", meta.debug_session_id)
            span.set_attribute("doyoutrade.cli.tool_name", tool_name)
            yield


__all__ = [
    "cli_trace_scope",
    "extract_parent_context",
    "inject_traceparent_into_env",
]
