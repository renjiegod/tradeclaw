"""Centralized ``monitor.*`` span names + run-id + detached-trace helper.

Spans are opened ONLY on a fire (``monitor.condition`` + ``monitor.delivery``)
and once per periodic sweep (``monitor.sweep``) — never per routine tick, which
would flood ``debug_session_spans``. High-frequency skips (out-of-session,
seal-data-missing, cooldown/edge suppression) are aggregated into per-sweep
counters and surfaced in the ``monitor_sweep_summary`` event, plus structured
``logger`` lines — visible without per-tick span overhead (CLAUDE.md §错误可见性).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

from opentelemetry import context as otel_context
from opentelemetry.trace import (
    INVALID_SPAN_CONTEXT,
    NonRecordingSpan,
    set_span_in_context,
)

# OTel span names (monitor.* namespace).
SPAN_CONDITION = "monitor.condition"
SPAN_DELIVERY = "monitor.delivery"
SPAN_SWEEP = "monitor.sweep"

# Debug-event names (each carries reason + hint where applicable).
EVENT_ALERT_FIRED = "monitor_alert_fired"
EVENT_DELIVERY_FAILED = "monitor_delivery_failed"
EVENT_DELIVERY_NO_TARGET = "monitor_delivery_no_target"
EVENT_CONDITION_TREE_INVALID = "monitor_condition_tree_invalid"
EVENT_SEAL_DATA_UNAVAILABLE = "monitor_seal_data_unavailable"
EVENT_SWEEP_SUMMARY = "monitor_sweep_summary"


def new_run_id() -> str:
    """Mint a per-fire run id (mirrors the worker's ``run-<uuid>`` shape)."""
    return f"run-{uuid.uuid4()}"


@contextmanager
def detached_trace_root() -> Iterator[None]:
    """Clear the inherited OTel parent so a fire starts its own trace.

    Mirrors ``worker._detached_cycle_trace_root`` so each monitor fire gets a
    distinct ``trace_id`` (reachable in the debug view) instead of sharing the
    long-lived daemon-task trace.
    """
    ctx = set_span_in_context(NonRecordingSpan(INVALID_SPAN_CONTEXT))
    token = otel_context.attach(ctx)
    try:
        yield
    finally:
        otel_context.detach(token)
