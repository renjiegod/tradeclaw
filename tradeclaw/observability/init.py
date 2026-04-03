from __future__ import annotations

from typing import TextIO
from weakref import WeakSet

from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor

from tradeclaw.observability.logging import configure_logging, reset_logging
from tradeclaw.observability.tracing import configure_tracing


_logging_instrumentor = LoggingInstrumentor()
_logging_instrumented = False
_instrumented_apps: WeakSet = WeakSet()


def initialize_observability(
    *,
    service_name: str = "tradeclaw",
    log_level: str = "INFO",
    stream: TextIO | None = None,
    app=None,
    tracing_enabled: bool = True,
    console_enabled: bool = True,
):
    global _logging_instrumented

    provider = configure_tracing(service_name=service_name, tracing_enabled=tracing_enabled)
    configure_logging(log_level=log_level, stream=stream, console_enabled=console_enabled)

    if tracing_enabled and not _logging_instrumented:
        _logging_instrumentor.instrument(set_logging_format=False)
        _logging_instrumented = True

    if tracing_enabled and app is not None and app not in _instrumented_apps:
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        _instrumented_apps.add(app)

    return provider


def reset_observability() -> None:
    global _logging_instrumented

    if _logging_instrumented:
        _logging_instrumentor.uninstrument()
        _logging_instrumented = False
    _instrumented_apps.clear()
    reset_logging()
