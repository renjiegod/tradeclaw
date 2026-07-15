"""Wrap a :class:`~doyoutrade.models.base.ModelAdapter` to persist per-call diagnostics."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from opentelemetry import trace

from doyoutrade.debug.context import debug_observability_enabled
from doyoutrade.money.decimal_helpers import json_default_with_decimals
from doyoutrade.models.base import ModelAdapter, ModelRequest, ModelResponse
from doyoutrade.observability.tracing import get_tracer
from doyoutrade.observability.worker_span_context import mark_worker_ancestor_spans_error
from doyoutrade.models.invoke_errors import model_invocation_failure_response_payload
from doyoutrade.models.invocation_context import model_invocation_call_kind, model_invocation_context
from doyoutrade.models.providers import (
    serialized_chat_invocation_request,
    serialized_model_invocation_request,
)
from doyoutrade.models.providers._common import redact_image_blocks

_LOG = logging.getLogger("doyoutrade.models.recording")

ModelInvocationRecorder = Callable[[dict[str, Any]], None]


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, default=json_default_with_decimals)
        return value
    except (TypeError, ValueError):
        return str(value)


def _serialize_lc_message(msg: Any) -> dict[str, Any]:
    """Serialize a LangChain AIMessage or PseudoAIMessage for recording."""
    if msg is None:
        return {}
    out: dict[str, Any] = {"class": type(msg).__name__}
    content = getattr(msg, "content", None)
    out["content"] = _json_safe(content)

    # Handle tool_calls for both LangChain AIMessage (list of ToolCall objects)
    # and PseudoAIMessage (list of PseudoToolCall dicts)
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls is not None:
        serialized_tcs: list[dict[str, Any]] = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                serialized_tcs.append(tc)
            else:
                # LangChain ToolCall or PseudoToolCall
                tc_dict: dict[str, Any] = {
                    "name": getattr(tc, "name", ""),
                    "args": getattr(tc, "args", ""),
                }
                tc_id = getattr(tc, "id", None)
                if tc_id is not None:
                    tc_dict["id"] = tc_id
                serialized_tcs.append(tc_dict)
        out["tool_calls"] = serialized_tcs

    rm = getattr(msg, "response_metadata", None) or {}
    if isinstance(rm, dict) and rm:
        out["response_metadata"] = _json_safe(dict(rm))
    um = getattr(msg, "usage_metadata", None) or {}
    if isinstance(um, dict) and um:
        out["usage_metadata"] = _json_safe(dict(um))
    return out


def _extract_token_usage(raw: Any) -> tuple[int | None, int | None, int | None, int | None, int | None]:
    if raw is None:
        return None, None, None, None, None
    um = getattr(raw, "usage_metadata", None) or {}
    if not isinstance(um, dict):
        return None, None, None, None, None
    inp = um.get("input_tokens")
    if inp is None:
        inp = um.get("prompt_tokens")
    out = um.get("output_tokens")
    if out is None:
        out = um.get("completion_tokens")
    tot = um.get("total_tokens")
    if tot is None and isinstance(inp, int) and isinstance(out, int):
        tot = inp + out

    # Cache tokens: Anthropic uses cache_read_input_tokens / cache_creation_input_tokens
    cache_read = um.get("cache_read_input_tokens")
    cache_write = um.get("cache_creation_input_tokens")
    # OpenAI-compatible: prompt_tokens_details.cached_tokens
    if cache_read is None:
        cached = um.get("prompt_tokens_details", {})
        if isinstance(cached, dict):
            cache_read = cached.get("cached_tokens")

    return (
        inp if isinstance(inp, int) else None,
        out if isinstance(out, int) else None,
        tot if isinstance(tot, int) else None,
        cache_read if isinstance(cache_read, int) else None,
        cache_write if isinstance(cache_write, int) else None,
    )


def _extract_model_id(raw: Any) -> str | None:
    """Extract the resolved model id from a provider response (response.raw.model)."""
    if raw is None:
        return None
    model = getattr(raw, "model", None)
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _extract_first_token_ms(raw: Any) -> int | None:
    if raw is None:
        return None
    rm = getattr(raw, "response_metadata", None) or {}
    if not isinstance(rm, dict):
        return None
    for key in (
        "time_to_first_token_ms",
        "ttft_ms",
        "first_token_ms",
    ):
        v = rm.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _clean_context_value(value: Any) -> Any | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped not in ("", "-") else None
    return value


def _span_event_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in attrs.items() if value is not None}


class RecordingModelAdapter(ModelAdapter):
    """Delegates to ``inner`` and emits structured records via ``recorder`` (sync or thread)."""

    def __init__(
        self,
        inner: ModelAdapter,
        *,
        provider: str,
        provider_kind: str,
        model: str,
        recorder: ModelInvocationRecorder | None,
    ):
        self._inner = inner
        self._provider = provider
        self._provider_kind = provider_kind
        self._model = model
        self._recorder = recorder

    def generate(self, request: ModelRequest) -> ModelResponse:
        if self._recorder is None or not debug_observability_enabled.get():
            return self._inner.generate(request)

        ctx = model_invocation_context.get() or {}
        kind = model_invocation_call_kind.get() or "unknown"
        task_id = ctx.get("task_id")
        run_id = ctx.get("run_id")
        trace_id = ctx.get("trace_id")
        route_name = ctx.get("model_route_name")
        provider_key = ctx.get("provider_key")
        task_id = _clean_context_value(task_id)
        run_id = _clean_context_value(run_id)
        trace_id = _clean_context_value(trace_id)
        mrn = _clean_context_value(route_name)
        pkey = _clean_context_value(provider_key) or _clean_context_value(self._provider)

        # Defensive image redaction: whichever path produced this body, image
        # base64 must never reach model_invocations (providers pre-redact, but
        # this entry point is the last line of defence).
        request_payload = dict(
            redact_image_blocks(serialized_model_invocation_request(self._inner, request))
        )

        span = self._start_model_span(kind)
        span.add_event("call_started", {
            "model": self._model,
            "provider": self._provider,
            "call_kind": kind,
        })

        t0 = time.perf_counter()
        try:
            response = self._inner.generate(request)
        except Exception as exc:
            total_ms = int((time.perf_counter() - t0) * 1000)
            span.add_event("error", {
                "error.type": type(exc).__name__,
                "error.message": str(exc),
            })
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            span.end()
            mark_worker_ancestor_spans_error(str(exc))
            payload = {
                "model_id": self._model,
                "provider_kind": self._provider_kind,
                "model": self._model,
                "model_route_name": mrn,
                "provider_key": pkey,
                "task_id": task_id,
                "run_id": run_id,
                "trace_id": trace_id,
                "call_kind": kind,
                "first_token_latency_ms": None,
                "total_latency_ms": total_ms,
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "ok": False,
                "error_message": str(exc),
                "request_payload": request_payload,
                "response_payload": model_invocation_failure_response_payload(exc, adapter=self),
                "span_id": span.get_span_context().span_id,
            }
            self._fire(payload)
            raise

        total_ms = int((time.perf_counter() - t0) * 1000)
        inp, out, tot, cache_read, cache_write = _extract_token_usage(response.raw)
        ttft = _extract_first_token_ms(response.raw)
        model_id = _extract_model_id(response.raw)
        span.add_event("call_ended", {
            "ok": True,
            "duration_ms": total_ms,
        })
        span.add_event(
            "token_usage",
            _span_event_attrs(
                {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "total_tokens": tot,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                }
            ),
        )
        span.set_status(trace.Status(trace.StatusCode.OK))
        span.end()
        if response.invocation_request_payload is not None:
            # Providers pre-redact image blocks; re-apply here so no wire-body
            # path can leak base64 into model_invocations.
            request_payload = dict(redact_image_blocks(response.invocation_request_payload))
        response_payload: dict[str, Any] | None
        if response.invocation_response_payload is not None:
            response_payload = dict(response.invocation_response_payload)
        else:
            response_payload = {"message": _serialize_lc_message(response.raw)}
        payload = {
            "model_id": model_id if model_id else self._model,
            "provider_kind": self._provider_kind,
            "model": self._model,
            "model_route_name": mrn,
            "provider_key": pkey,
            "task_id": task_id,
            "run_id": run_id,
            "trace_id": trace_id,
            "call_kind": kind,
            "first_token_latency_ms": ttft,
            "total_latency_ms": total_ms,
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": tot,
            "cache_read_tokens": cache_read if isinstance(cache_read, int) else None,
            "cache_write_tokens": cache_write if isinstance(cache_write, int) else None,
            "ok": True,
            "error_message": "",
            "request_payload": request_payload,
            "response_payload": response_payload,
            "span_id": span.get_span_context().span_id,
        }
        self._fire(payload)
        return response

    async def chat_ainvoke(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        inner = self._inner
        inner_ainvoke = getattr(inner, "chat_ainvoke", None)
        if inner_ainvoke is None:
            raise TypeError(
                "inner adapter must implement chat_ainvoke() for native tool turns "
                "(AnthropicAdapter / OpenAICompatibleAdapter).",
            )
        if self._recorder is None or not debug_observability_enabled.get():
            return await inner_ainvoke(messages, tools=tools)

        ctx = model_invocation_context.get() or {}
        kind = model_invocation_call_kind.get() or "unknown"
        task_id = ctx.get("task_id")
        run_id = ctx.get("run_id")
        trace_id = ctx.get("trace_id")
        route_name = ctx.get("model_route_name")
        provider_key = ctx.get("provider_key")
        task_id = _clean_context_value(task_id)
        run_id = _clean_context_value(run_id)
        trace_id = _clean_context_value(trace_id)
        mrn = _clean_context_value(route_name)
        pkey = _clean_context_value(provider_key) or _clean_context_value(self._provider)

        request_payload = dict(
            redact_image_blocks(serialized_chat_invocation_request(inner, messages, tools))
        )

        span = self._start_model_span(kind)
        span.add_event("call_started", {
            "model": self._model,
            "provider": self._provider,
            "call_kind": kind,
        })

        t0 = time.perf_counter()
        try:
            response = await inner_ainvoke(messages, tools=tools)
        except Exception as exc:
            total_ms = int((time.perf_counter() - t0) * 1000)
            span.add_event("error", {
                "error.type": type(exc).__name__,
                "error.message": str(exc),
            })
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            span.end()
            mark_worker_ancestor_spans_error(str(exc))
            payload = {
                "model_id": self._model,
                "provider_kind": self._provider_kind,
                "model": self._model,
                "model_route_name": mrn,
                "provider_key": pkey,
                "task_id": task_id,
                "run_id": run_id,
                "trace_id": trace_id,
                "call_kind": kind,
                "first_token_latency_ms": None,
                "total_latency_ms": total_ms,
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "ok": False,
                "error_message": str(exc),
                "request_payload": request_payload,
                "response_payload": model_invocation_failure_response_payload(exc, adapter=self),
                "span_id": span.get_span_context().span_id,
            }
            self._fire(payload)
            raise

        total_ms = int((time.perf_counter() - t0) * 1000)
        inp, out, tot, cache_read, cache_write = _extract_token_usage(response.raw)
        ttft = _extract_first_token_ms(response.raw)
        model_id = _extract_model_id(response.raw)
        span.add_event("call_ended", {
            "ok": True,
            "duration_ms": total_ms,
        })
        span.add_event(
            "token_usage",
            _span_event_attrs(
                {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "total_tokens": tot,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                }
            ),
        )
        span.set_status(trace.Status(trace.StatusCode.OK))
        span.end()
        if response.invocation_request_payload is not None:
            # Providers pre-redact image blocks; re-apply here so no wire-body
            # path can leak base64 into model_invocations.
            request_payload = dict(redact_image_blocks(response.invocation_request_payload))
        response_payload: dict[str, Any] | None
        if response.invocation_response_payload is not None:
            response_payload = dict(response.invocation_response_payload)
        else:
            response_payload = {"message": _serialize_lc_message(response.raw)}
        payload = {
            "model_id": model_id if model_id else self._model,
            "provider_kind": self._provider_kind,
            "model": self._model,
            "model_route_name": mrn,
            "provider_key": pkey,
            "task_id": task_id,
            "run_id": run_id,
            "trace_id": trace_id,
            "call_kind": kind,
            "first_token_latency_ms": ttft,
            "total_latency_ms": total_ms,
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": tot,
            "cache_read_tokens": cache_read if isinstance(cache_read, int) else None,
            "cache_write_tokens": cache_write if isinstance(cache_write, int) else None,
            "ok": True,
            "error_message": "",
            "request_payload": request_payload,
            "response_payload": response_payload,
            "span_id": span.get_span_context().span_id,
        }
        self._fire(payload)
        return response

    async def agent_turn(
        self,
        messages: list[Any],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_text_delta: Callable[[str], Any] | None = None,
        on_thinking_delta: Callable[[str], Any] | None = None,
    ):
        from doyoutrade.agent_runtime import (
            AgentTurnResponse,
            agent_turn_response_from_model_response,
        )

        inner_agent_turn = getattr(self._inner, "agent_turn", None)
        if inner_agent_turn is None:
            response = await self.chat_ainvoke(messages, tools=tools)
            turn = agent_turn_response_from_model_response(response)
            if on_text_delta is not None and turn.content:
                maybe_awaitable = on_text_delta(turn.content)
                if hasattr(maybe_awaitable, "__await__"):
                    await maybe_awaitable
            return turn

        if self._recorder is None or not debug_observability_enabled.get():
            return await inner_agent_turn(
                messages,
                tools=tools,
                on_text_delta=on_text_delta,
                on_thinking_delta=on_thinking_delta,
            )

        ctx = model_invocation_context.get() or {}
        kind = model_invocation_call_kind.get() or "unknown"
        task_id = ctx.get("task_id")
        run_id = ctx.get("run_id")
        trace_id = ctx.get("trace_id")
        route_name = ctx.get("model_route_name")
        provider_key = ctx.get("provider_key")
        task_id = _clean_context_value(task_id)
        run_id = _clean_context_value(run_id)
        trace_id = _clean_context_value(trace_id)
        mrn = _clean_context_value(route_name)
        pkey = _clean_context_value(provider_key) or _clean_context_value(self._provider)

        request_payload = dict(
            redact_image_blocks(serialized_chat_invocation_request(self._inner, messages, tools))
        )
        span = self._start_model_span(kind)
        span.add_event("call_started", {
            "model": self._model,
            "provider": self._provider,
            "call_kind": kind,
            "stream": True,
        })

        t0 = time.perf_counter()
        first_token_ms: int | None = None

        async def _record_first_token_and_forward(callback: Callable[[str], Any] | None, delta: str) -> None:
            nonlocal first_token_ms
            if first_token_ms is None and delta:
                first_token_ms = int((time.perf_counter() - t0) * 1000)
            if callback is not None:
                maybe_awaitable = callback(delta)
                if hasattr(maybe_awaitable, "__await__"):
                    await maybe_awaitable

        async def _on_text_delta(delta: str) -> None:
            await _record_first_token_and_forward(on_text_delta, delta)

        async def _on_thinking_delta(delta: str) -> None:
            await _record_first_token_and_forward(on_thinking_delta, delta)

        should_stream = on_text_delta is not None or on_thinking_delta is not None
        try:
            turn = await inner_agent_turn(
                messages,
                tools=tools,
                on_text_delta=_on_text_delta if should_stream else None,
                on_thinking_delta=_on_thinking_delta if should_stream else None,
            )
            if not isinstance(turn, AgentTurnResponse):
                turn = agent_turn_response_from_model_response(turn)
        except Exception as exc:
            total_ms = int((time.perf_counter() - t0) * 1000)
            span.add_event("error", {
                "error.type": type(exc).__name__,
                "error.message": str(exc),
            })
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            span.end()
            mark_worker_ancestor_spans_error(str(exc))
            self._fire(
                {
                    "model_id": self._model,
                    "provider": self._provider,
                    "provider_kind": self._provider_kind,
                    "model": self._model,
                    "model_route_name": mrn,
                    "provider_key": pkey,
                    "task_id": task_id,
                    "run_id": run_id,
                    "trace_id": trace_id,
                    "call_kind": kind,
                    "first_token_latency_ms": None,
                    "total_latency_ms": total_ms,
                    "input_tokens": None,
                    "output_tokens": None,
                    "total_tokens": None,
                    "cache_read_tokens": None,
                    "cache_write_tokens": None,
                    "ok": False,
                    "error_message": str(exc),
                    "request_payload": request_payload,
                    "response_payload": model_invocation_failure_response_payload(exc, adapter=self),
                    "span_id": span.get_span_context().span_id,
                }
            )
            raise

        total_ms = int((time.perf_counter() - t0) * 1000)
        inp, out, tot, cache_read, cache_write = _extract_token_usage(turn.raw)
        if inp is None:
            inp = turn.usage.get("input_tokens")
        if out is None:
            out = turn.usage.get("output_tokens")
        if tot is None:
            tot = turn.usage.get("total_tokens")
        ttft = _extract_first_token_ms(turn.raw)
        if ttft is None:
            ttft = first_token_ms
        model_id = _extract_model_id(turn.raw)
        span.add_event("call_ended", {"ok": True, "duration_ms": total_ms})
        span.add_event(
            "token_usage",
            _span_event_attrs(
                {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "total_tokens": tot,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                }
            ),
        )
        span.set_status(trace.Status(trace.StatusCode.OK))
        span.end()
        if turn.request_payload is not None:
            request_payload = dict(redact_image_blocks(turn.request_payload))
        if turn.response_payload is not None:
            response_payload = dict(turn.response_payload)
        else:
            response_payload = {"message": _serialize_lc_message(turn.raw)}
        self._fire(
            {
                "model_id": model_id if model_id else self._model,
                "provider_kind": self._provider_kind,
                "model": self._model,
                "model_route_name": mrn,
                "provider_key": pkey,
                "task_id": task_id,
                "run_id": run_id,
                "trace_id": trace_id,
                "call_kind": kind,
                "first_token_latency_ms": ttft,
                "total_latency_ms": total_ms,
                "input_tokens": inp if isinstance(inp, int) else None,
                "output_tokens": out if isinstance(out, int) else None,
                "total_tokens": tot if isinstance(tot, int) else None,
                "cache_read_tokens": cache_read if isinstance(cache_read, int) else None,
                "cache_write_tokens": cache_write if isinstance(cache_write, int) else None,
                "ok": True,
                "error_message": "",
                "request_payload": request_payload,
                "response_payload": response_payload,
                "span_id": span.get_span_context().span_id,
            }
        )
        return turn

    def _fire(self, payload: dict[str, Any]) -> None:
        assert self._recorder is not None
        try:
            self._recorder(payload)
        except Exception:
            _LOG.exception("model invocation recorder failed")

    def _start_model_span(self, kind: str) -> trace.Span:
        tracer = get_tracer("doyoutrade.models.recording")
        span_name = f"model.{self._provider}.{self._model}"
        span = tracer.start_span(
            span_name,
            attributes={
                "provider": self._provider,
                "model": self._model,
                "call_kind": kind,
            },
        )
        return span
