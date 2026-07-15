"""OpenTelemetry span and debug-event instrumentation helpers for data providers."""

from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from typing import Any

from opentelemetry.trace import Status, StatusCode

from doyoutrade.debug import emit_debug_event
from doyoutrade.observability import get_tracer
from doyoutrade.observability.worker_span_context import mark_worker_ancestor_spans_error


@contextmanager
def data_span(provider: str, method: str):
    """Sync context manager: enters a data-provider method span and emits a debug event on exit.

    The span is a child of whatever OTEL context is active when entered.
    The event is only emitted if the span is recording.
    """
    tracer = get_tracer("doyoutrade.data")
    span_name = f"data.{provider}.{method}"
    event_name = f"data_provider.{method}"
    start = time.perf_counter()
    with tracer.start_as_current_span(span_name) as span:
        span.set_attribute("data.provider", provider)
        span.set_attribute("data.method", method)
        try:
            yield
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            mark_worker_ancestor_spans_error(str(exc))
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            span.set_attribute("data.duration_ms", duration_ms)
            _fire_event(event_name, {
                "provider": provider,
                "method": method,
                "duration_ms": round(duration_ms, 2),
            })


def _fire_event(event_name: str, payload: dict[str, Any]) -> None:
    """Fire emit_debug_event as a fire-and-forget task from a sync context."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        # No running event loop (e.g., during import); skip event.
        pass
