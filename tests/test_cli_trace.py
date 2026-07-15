"""Unit tests for doyoutrade.cli._trace — W3C tracecontext propagation.

Validates that ``inject_traceparent_into_env`` and ``cli_trace_scope``
round-trip a parent OTel trace context across the
``execute_bash → doyoutrade-cli`` boundary so debug events emitted from
the CLI subprocess land in the agent's debug session.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider

from doyoutrade.cli._envelope import Meta
from doyoutrade.cli._trace import (
    cli_trace_scope,
    extract_parent_context,
    inject_traceparent_into_env,
)


def _ensure_sdk_provider() -> None:
    """Install an SDK TracerProvider once per process so tracing isn't no-op."""
    provider = trace_api.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        return
    trace_api.set_tracer_provider(TracerProvider())


class InjectAndExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_sdk_provider()

    def test_inject_writes_traceparent_when_span_is_active(self) -> None:
        env: dict[str, str] = {}
        tracer = trace_api.get_tracer("test")
        with tracer.start_as_current_span("parent") as span:
            inject_traceparent_into_env(env)
            self.assertIn("TRACEPARENT", env)
            parent_tid = format(span.get_span_context().trace_id, "032x")
            # traceparent: 00-<trace_id>-<span_id>-<flags>
            self.assertIn(parent_tid, env["TRACEPARENT"])

    def test_inject_is_noop_when_no_span_active(self) -> None:
        env: dict[str, str] = {}
        # Drop into an explicitly empty context.
        from opentelemetry import context as context_api

        token = context_api.attach(context_api.Context())
        try:
            inject_traceparent_into_env(env)
        finally:
            context_api.detach(token)
        # The default provider may still inject "00-0000..." when no span,
        # depending on propagator. We tolerate either: empty env or an
        # injected but all-zeros trace_id (which is "invalid" per W3C).
        if env:
            tp = env.get("TRACEPARENT", "")
            parts = tp.split("-")
            # If injected, it must look like a traceparent — version 00
            # plus four dashes.
            self.assertTrue(tp.startswith("00-"))
            self.assertEqual(len(parts), 4)

    def test_extract_returns_none_when_env_absent(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRACEPARENT", None)
            os.environ.pop("TRACESTATE", None)
            self.assertIsNone(extract_parent_context())

    def test_round_trip_preserves_trace_id(self) -> None:
        env: dict[str, str] = {}
        tracer = trace_api.get_tracer("test")
        with tracer.start_as_current_span("parent") as parent:
            inject_traceparent_into_env(env)
            parent_tid = format(parent.get_span_context().trace_id, "032x")

        with patch.dict(os.environ, env, clear=False):
            ctx = extract_parent_context()
            self.assertIsNotNone(ctx)
            # Activate the extracted context and read the trace id back.
            from opentelemetry import context as context_api

            token = context_api.attach(ctx)
            try:
                current = trace_api.get_current_span()
                child_tid = format(current.get_span_context().trace_id, "032x")
            finally:
                context_api.detach(token)

        self.assertEqual(child_tid, parent_tid)


class CliTraceScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_sdk_provider()

    def test_scope_inherits_parent_when_env_set(self) -> None:
        tracer = trace_api.get_tracer("test")
        env: dict[str, str] = {}
        with tracer.start_as_current_span("agent") as parent:
            parent_tid = format(parent.get_span_context().trace_id, "032x")
            inject_traceparent_into_env(env)

        meta = Meta(agent_id="asst-x", session_id="s-y", debug_session_id="s-y")
        with patch.dict(os.environ, env, clear=False):
            with cli_trace_scope("test_tool", meta):
                inner = trace_api.get_current_span()
                inner_tid = format(inner.get_span_context().trace_id, "032x")
                self.assertEqual(inner_tid, parent_tid)
                self.assertTrue(inner.is_recording())

    def test_scope_does_not_break_when_env_absent(self) -> None:
        # Standalone CLI run: TRACEPARENT not set. The scope must still
        # produce a usable span so tools' emit_debug_event() doesn't
        # crash; we don't assert recording status because that depends
        # on the configured tracer provider.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRACEPARENT", None)
            os.environ.pop("TRACESTATE", None)
            meta = Meta()
            with cli_trace_scope("test_tool", meta):
                span = trace_api.get_current_span()
                # is_recording is False on default no-op spans; either
                # way, calling set_attribute must not raise.
                span.set_attribute("noop_ok", True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
