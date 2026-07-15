"""OpenTelemetry span export into ``debug_session_spans`` (local DB) when a session is active."""

from __future__ import annotations

import asyncio

from doyoutrade.diagnostics import runtime_diag
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterator, Mapping, Optional, Sequence

from opentelemetry import trace as trace_api
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Span as SpanAPI
from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)

ATTR_SESSION_ID = "doyoutrade.session_id"
# Legacy attribute from older exporters / in-flight spans
_ATTR_SESSION_ID_LEGACY = "doyoutrade.debug_session_id"
ATTR_SPAN_SOURCE = "doyoutrade.span_source"
ATTR_SPAN_TYPE = "doyoutrade.span_type"
ATTR_EVENT_PAYLOAD_JSON = "doyoutrade.event.payload_json"

_DEFAULT_SPAN_TYPE = "internal"
_DEFAULT_SPAN_SOURCE = "debug"

SpanPersistSink = Callable[[dict[str, Any]], None]

_persist_sink: SpanPersistSink | None = None
_processors_installed = False

# Serialized writes for sqlite+aiosqlite: OTel calls the sink synchronously on span end;
# scheduling many concurrent append_span tasks can deadlock with other DB users (e.g. mark_finished).
_persist_queue: asyncio.Queue | None = None
_persist_worker_task: asyncio.Task[None] | None = None
_PERSIST_SENTINEL = object()


def debug_span_queue_sink(row: dict[str, Any]) -> None:
    """Enqueue a span row for the background worker (non-blocking)."""
    q = _persist_queue
    if q is None:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    q.put_nowait(row)


async def start_debug_span_persist_worker(append_span: Callable[..., Awaitable[Any]]) -> None:
    """Start a single consumer that persists queued span rows via ``append_span``."""
    global _persist_queue, _persist_worker_task
    if _persist_worker_task is not None and not _persist_worker_task.done():
        return

    async def _worker() -> None:
        assert _persist_queue is not None
        while True:
            row = await _persist_queue.get()
            try:
                if row is _PERSIST_SENTINEL:
                    break
                await append_span(**row)
            except Exception:
                logger.exception("failed to persist exported debug span")
            finally:
                _persist_queue.task_done()

    _persist_queue = asyncio.Queue()
    _persist_worker_task = asyncio.create_task(_worker())


async def drain_debug_span_persist_queue() -> None:
    """Wait until all rows currently in the persist queue have been written."""
    global _persist_queue, _persist_worker_task
    q = _persist_queue
    if q is None:
        runtime_diag("drain_debug_span_persist_queue: no queue (sink disabled)")
        return
    t = _persist_worker_task
    if t is None or t.done():
        runtime_diag("drain_debug_span_persist_queue: stale queue without active worker")
        _persist_queue = None
        _persist_worker_task = None
        return
    runtime_diag("drain_debug_span_persist_queue: awaiting q.join()")
    await q.join()
    runtime_diag("drain_debug_span_persist_queue: join complete")


async def stop_debug_span_persist_worker() -> None:
    """Drain pending rows, stop the worker, and release queue state."""
    global _persist_queue, _persist_worker_task
    q = _persist_queue
    t = _persist_worker_task
    if q is None and t is None:
        return
    try:
        if q is not None and t is not None and not t.done():
            await q.join()
            await q.put(_PERSIST_SENTINEL)
            await t
    finally:
        _persist_queue = None
        _persist_worker_task = None


@dataclass(frozen=True)
class _DebugExportContext:
    session_id: str
    span_source: str


_debug_export_ctx: ContextVar[_DebugExportContext | None] = ContextVar(
    "doyoutrade_debug_export_ctx",
    default=None,
)


def register_span_persist_sink(sink: SpanPersistSink | None) -> None:
    """Register the sink that persists exported span rows (or clear with None)."""
    global _persist_sink
    _persist_sink = sink


def ensure_debug_span_export_processors() -> None:
    """Attach enricher + DB exporter to the SDK TracerProvider (idempotent)."""
    global _processors_installed
    if _processors_installed:
        return
    from opentelemetry.sdk.trace import TracerProvider

    provider = trace_api.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        logger.debug("skip debug span export: tracer provider is not SDK TracerProvider")
        return
    provider.add_span_processor(_DebugSessionEnrichingProcessor())
    provider.add_span_processor(SimpleSpanProcessor(_DatabaseSpanExporter()))
    _processors_installed = True


@contextmanager
def debug_span_export_for_session(session_id: str, span_source: str = _DEFAULT_SPAN_SOURCE) -> Iterator[None]:
    """Mark OTel spans created in this block for export to ``debug_session_spans``."""
    ctx = _DebugExportContext(session_id=session_id, span_source=span_source)
    token: Token = _debug_export_ctx.set(ctx)
    try:
        yield
    finally:
        _debug_export_ctx.reset(token)


class _DebugSessionEnrichingProcessor(SpanProcessor):
    def on_start(self, span: SpanAPI, parent_context: Optional[Context] = None) -> None:
        exp = _debug_export_ctx.get()
        if exp is None:
            return
        span.set_attribute(ATTR_SESSION_ID, exp.session_id)
        span.set_attribute(ATTR_SPAN_SOURCE, exp.span_source)

    def on_end(self, span: ReadableSpan) -> None:
        return

    def shutdown(self) -> None:
        return

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def _ns_to_naive_utc(ns: int | None) -> datetime | None:
    if ns is None:
        return None
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).replace(tzinfo=None)


def _json_safe_attributes(attrs: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in attrs.items():
        if isinstance(val, (str, int, float, bool)) or val is None:
            out[key] = val
        elif isinstance(val, (list, tuple)):
            seq = [_json_safe_scalar(x) for x in val]
            out[key] = seq
        else:
            out[key] = str(val)
    return out


def _json_safe_scalar(val: Any) -> Any:
    if isinstance(val, (str, int, float, bool)) or val is None:
        return val
    if isinstance(val, (list, tuple)):
        return [_json_safe_scalar(x) for x in val]
    return str(val)


def _decode_error_attribute(attrs: dict[str, Any]) -> None:
    raw = attrs.get("error")
    if isinstance(raw, str):
        try:
            attrs["error"] = json.loads(raw)
        except json.JSONDecodeError:
            pass


def _events_from_readable(span: ReadableSpan) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for ev in span.events:
        payload: dict[str, Any] = {}
        ev_attrs = dict(ev.attributes) if ev.attributes else {}
        raw = ev_attrs.get(ATTR_EVENT_PAYLOAD_JSON)
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"_raw": raw}
        else:
            # Fallback: span.add_event() was called directly without ATTR_EVENT_PAYLOAD_JSON wrapper
            # (e.g., model_invocation spans in recording.py). Use remaining attributes as payload.
            for k, v in ev_attrs.items():
                if k != ATTR_EVENT_PAYLOAD_JSON:
                    payload[k] = v
        events.append({"event_type": ev.name, "payload": payload})
    return events


class _DatabaseSpanExporter(SpanExporter):
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        sink = _persist_sink
        if sink is None:
            return SpanExportResult.SUCCESS
        for ro in spans:
            attrs = dict(ro.attributes) if ro.attributes else {}
            session_id = attrs.get(ATTR_SESSION_ID) or attrs.get(_ATTR_SESSION_ID_LEGACY)
            if not isinstance(session_id, str) or not session_id.strip():
                session_id = "default"
            span_source = attrs.get(ATTR_SPAN_SOURCE)
            if not isinstance(span_source, str) or not span_source.strip():
                span_source = _DEFAULT_SPAN_SOURCE
            span_type = attrs.get(ATTR_SPAN_TYPE)
            if not isinstance(span_type, str) or not span_type.strip():
                span_type = _DEFAULT_SPAN_TYPE

            ctx = ro.context
            trace_id = format(ctx.trace_id, "032x")
            span_id = format(ctx.span_id, "016x")
            parent = ro.parent
            parent_span_id: str | None
            if parent is not None and parent.is_valid:
                parent_span_id = format(parent.span_id, "016x")
            else:
                parent_span_id = None

            if ro.status.status_code == StatusCode.ERROR:
                status = "error"
            else:
                status = "ok"

            stored_attrs = _json_safe_attributes(attrs)
            for k in (ATTR_SESSION_ID, _ATTR_SESSION_ID_LEGACY, ATTR_SPAN_SOURCE, ATTR_SPAN_TYPE):
                stored_attrs.pop(k, None)
            _decode_error_attribute(stored_attrs)

            if status == "error":
                desc = ro.status.description
                if isinstance(desc, str) and desc.strip():
                    # OTel span status description is not otherwise persisted; mirror into attributes for Trace UI.
                    stored_attrs["span_status_message"] = desc.strip()[:12000]

            events = _events_from_readable(ro)
            if events:
                stored_attrs["_events"] = events

            start_time = _ns_to_naive_utc(ro.start_time)
            end_time = _ns_to_naive_utc(ro.end_time)
            duration_ms: float | None = None
            if ro.start_time is not None and ro.end_time is not None:
                duration_ms = (ro.end_time - ro.start_time) / 1e6

            sink(
                {
                    "span_id": span_id,
                    "trace_id": trace_id,
                    "parent_span_id": parent_span_id,
                    "session_id": session_id,
                    "name": ro.name,
                    "span_type": span_type,
                    "start_time": start_time or datetime.now(timezone.utc).replace(tzinfo=None),
                    "end_time": end_time,
                    "duration_ms": duration_ms,
                    "attributes": stored_attrs,
                    "status": status,
                    "span_source": span_source,
                },
            )
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return
