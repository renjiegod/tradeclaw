"""Register active ``worker.run_cycle`` / ``worker.phase.*`` spans for error propagation.

OpenTelemetry does not bubble ERROR status to parent spans. Nested code (strategies,
model recording, data providers) calls :func:`mark_worker_ancestor_spans_error` so the
debug trace tree shows phase and cycle as failed when any child span fails."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator

from opentelemetry.trace import Span, Status, StatusCode

_cycle_span_var: ContextVar[Span | None] = ContextVar("doyoutrade_worker_cycle_span", default=None)
_phase_span_var: ContextVar[Span | None] = ContextVar("doyoutrade_worker_phase_span", default=None)


def mark_worker_ancestor_spans_error(description: str) -> None:
    """Set OTel ERROR on the innermost registered worker phase span and on ``worker.run_cycle``."""
    msg = (description or "error")[:12000]
    st = Status(StatusCode.ERROR, msg)
    for span in (_phase_span_var.get(), _cycle_span_var.get()):
        if span is not None:
            span.set_status(st)


@contextmanager
def worker_run_cycle_span(tracer: Any) -> Iterator[Span]:
    """Enter ``worker.run_cycle`` and expose it for ancestor error propagation."""
    with tracer.start_as_current_span("worker.run_cycle") as span:
        tok: Token[Span | None] = _cycle_span_var.set(span)
        try:
            yield span
        finally:
            _cycle_span_var.reset(tok)


@contextmanager
def worker_phase_span(tracer: Any, name: str, **span_kwargs: Any) -> Iterator[Span]:
    """Enter a ``worker.phase.*`` span and expose it for ancestor error propagation."""
    with tracer.start_as_current_span(name, **span_kwargs) as span:
        tok: Token[Span | None] = _phase_span_var.set(span)
        try:
            yield span
        finally:
            _phase_span_var.reset(tok)
