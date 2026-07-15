from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider


_provider: TracerProvider | None = None


def configure_tracing(service_name: str = "doyoutrade", tracing_enabled: bool = True):
    global _provider

    if not tracing_enabled:
        return trace.get_tracer_provider()

    if _provider is None:
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        trace.set_tracer_provider(provider)
        _provider = provider

    return trace.get_tracer_provider()


def get_tracer(name: str):
    return trace.get_tracer(name)
