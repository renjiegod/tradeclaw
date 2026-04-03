from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import TextIO

from opentelemetry import trace


_HANDLER_NAME = "tradeclaw-observability-console"
_handler: logging.Handler | None = None


class TradeclawLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        trace_id = self._trace_id(record)
        span_id = self._span_id(record)
        message = record.getMessage()
        return (
            f"time={timestamp} level={record.levelname} logger={record.name} "
            f"trace_id={trace_id} span_id={span_id} message={message}"
        )

    def _trace_id(self, record: logging.LogRecord) -> str:
        value = getattr(record, "otelTraceID", "")
        if value:
            return str(value)
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return f"{context.trace_id:032x}"
        return "-"

    def _span_id(self, record: logging.LogRecord) -> str:
        value = getattr(record, "otelSpanID", "")
        if value:
            return str(value)
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return f"{context.span_id:016x}"
        return "-"


def configure_logging(
    *,
    log_level: str = "INFO",
    stream: TextIO | None = None,
    console_enabled: bool = True,
) -> logging.Handler | None:
    global _handler

    root = logging.getLogger()
    root.setLevel(_coerce_log_level(log_level))

    if not console_enabled:
        if _handler is not None:
            root.removeHandler(_handler)
            _handler = None
        return None

    if _handler is None:
        _handler = logging.StreamHandler(stream or sys.stderr)
        _handler.set_name(_HANDLER_NAME)
        _handler.setFormatter(TradeclawLogFormatter())
        root.addHandler(_handler)
    elif stream is not None and hasattr(_handler, "setStream"):
        _handler.setStream(stream)

    return _handler


def reset_logging() -> None:
    global _handler

    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)
    _handler = None


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def _coerce_log_level(level: str) -> int:
    return getattr(logging, str(level).strip().upper(), logging.INFO)
