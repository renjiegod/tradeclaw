from __future__ import annotations

import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import TextIO

from opentelemetry import trace


_HANDLER_NAME = "doyoutrade-observability-console"
_handler: logging.Handler | None = None
_LOG_TZ = ZoneInfo("Asia/Shanghai")


class DoyoutradeLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        dt = datetime.fromtimestamp(record.created, tz=_LOG_TZ)
        timestamp = dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d} +08:00"
        trace_id = self._trace_id(record)
        message = record.getMessage()
        return (
            f"time={timestamp} level={record.levelname} logger={record.name} "
            f"trace_id={trace_id} message={message}"
        )

    def _trace_id(self, record: logging.LogRecord) -> str:
        value = getattr(record, "otelTraceID", "")
        if value:
            return str(value)
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return f"{context.trace_id:032x}"
        return "-"


def configure_logging(
    *,
    log_level: str = "INFO",
    stream: TextIO | None = None,
    console_enabled: bool = True,
) -> logging.Handler | None:
    global _handler

    root = logging.getLogger()

    # Remove ALL existing root handlers so that third-party logging configs
    # (alembic fileConfig, uvicorn dictConfig) cannot leave stale handlers
    # that produce duplicate or non-standard-format log lines.
    for existing in list(root.handlers):
        root.removeHandler(existing)

    root.setLevel(_coerce_log_level(log_level))

    if not console_enabled:
        _handler = None
        return None

    _handler = logging.StreamHandler(stream or sys.stderr)
    _handler.set_name(_HANDLER_NAME)
    _handler.setFormatter(DoyoutradeLogFormatter())
    root.addHandler(_handler)

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
