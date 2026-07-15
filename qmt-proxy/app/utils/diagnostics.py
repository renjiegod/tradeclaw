"""Lightweight observability for isolated xtdata operations.

Keeps a thread-safe in-memory ring buffer of the most recent xtdata subprocess
calls and mirrors each record to a JSONL file. The goal is post-mortem triage
without reproduction: when ``POST /api/v1/data/market`` (or any other xtdata
call) is slow or fails, an operator can hit the diagnostics endpoints and see
which operation ran, with what (summarised) arguments, how long it took, and
what error / exit code / stderr came back.

Recording must never break the main request flow, so file-write failures are
logged (``logger.warning``) but otherwise swallowed.
"""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

from app.utils.logger import logger

# Default ring-buffer capacity. Kept small to bound memory; the JSONL file is
# the durable record for anything older than the last N entries.
_DEFAULT_MAXLEN = 500

# JSONL path follows logger.py's "logs/" directory convention.
_JSONL_PATH = os.path.join("logs", "xtdata_ops.jsonl")

# How many characters of a stderr blob we keep in a single record.
_STDERR_SNIPPET_LIMIT = 500

_lock = threading.Lock()
_buffer: Deque[Dict[str, Any]] = deque(maxlen=_DEFAULT_MAXLEN)

# Keys we consider "interesting" when summarising kwargs. We look both at the
# top level and inside the download_and_get_market_data sub-dicts.
_SUMMARY_KEYS = (
    "stock_code",
    "stock_list",
    "period",
    "start_time",
    "end_time",
    "start_date",
    "end_date",
    "count",
    "dividend_type",
    "disable_download",
    "incrementally",
)


def summarize_kwargs(operation: str, kwargs: Any) -> Dict[str, Any]:
    """Build a compact, JSON-serialisable summary of an operation's kwargs.

    Robust against unexpected shapes: any failure produces a placeholder entry
    instead of crashing the caller.
    """
    summary: Dict[str, Any] = {}
    try:
        if not isinstance(kwargs, dict):
            return {"_note": f"kwargs not a dict (type={type(kwargs).__name__})"}

        # download_and_get_market_data nests two sub-dicts; flatten the
        # interesting bits with a prefix so they remain distinguishable.
        nested_groups = []
        for group_key in ("download", "market"):
            group = kwargs.get(group_key)
            if isinstance(group, dict):
                nested_groups.append((group_key, group))

        if nested_groups:
            for group_key, group in nested_groups:
                for key in _SUMMARY_KEYS:
                    if key in group:
                        summary[f"{group_key}.{key}"] = group.get(key)
        else:
            for key in _SUMMARY_KEYS:
                if key in kwargs:
                    summary[key] = kwargs.get(key)

        if not summary:
            # Nothing matched our known keys; record the key names so we still
            # have a breadcrumb without dumping potentially large values.
            summary["_keys"] = sorted(str(k) for k in kwargs.keys())
    except Exception as exc:  # pragma: no cover - defensive only
        return {"_note": f"kwargs summary failed: {type(exc).__name__}: {exc}"}

    return summary


def record(
    *,
    operation: str,
    kwargs: Any,
    duration_ms: float,
    ok: bool,
    client_id: str = "default",
    error: Optional[str] = None,
    exit_code: Optional[int] = None,
    stderr_snippet: Optional[str] = None,
) -> None:
    """Append one xtdata-operation record to the ring buffer and JSONL file.

    ``client_id`` 标记本次操作落在哪个 QMT 终端（多终端部署时用于区分；单终端
    部署默认为 ``"default"``）。
    """
    entry: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(),
        "client_id": client_id,
        "operation": operation,
        "kwargs_summary": summarize_kwargs(operation, kwargs),
        "duration_ms": round(float(duration_ms), 2),
        "ok": bool(ok),
        "error": error,
        "exit_code": exit_code,
        "stderr_snippet": (
            stderr_snippet[:_STDERR_SNIPPET_LIMIT] if stderr_snippet else None
        ),
    }

    with _lock:
        _buffer.append(entry)

    _append_jsonl(entry)


def _append_jsonl(entry: Dict[str, Any]) -> None:
    """Append a single record as one JSON line. Never raises into the caller."""
    try:
        log_dir = os.path.dirname(_JSONL_PATH)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        with open(_JSONL_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:
        # Must not break the main flow, but must not be silent either.
        logger.warning(
            f"写入 xtdata 诊断 JSONL 失败 [{type(exc).__name__}]: {exc} "
            f"(operation={entry.get('operation')})"
        )


def recent(
    limit: int = 50,
    *,
    only_errors: bool = False,
    min_duration_ms: Optional[float] = None,
    operation: Optional[str] = None,
    client_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return the most recent records (newest first), with optional filters."""
    with _lock:
        items = list(_buffer)

    if only_errors:
        items = [e for e in items if not e.get("ok", True)]
    if min_duration_ms is not None:
        items = [e for e in items if (e.get("duration_ms") or 0) >= min_duration_ms]
    if operation:
        items = [e for e in items if e.get("operation") == operation]
    if client_id:
        items = [e for e in items if e.get("client_id") == client_id]

    items.reverse()  # newest first
    if limit is not None and limit >= 0:
        items = items[:limit]
    return items


def _operation_stats(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """按 operation 聚合 count / error_count / avg / max 时延。"""
    by_op: Dict[str, Dict[str, Any]] = {}
    for entry in items:
        op = entry.get("operation", "unknown")
        bucket = by_op.setdefault(op, {"count": 0, "error_count": 0, "_durations": []})
        bucket["count"] += 1
        if not entry.get("ok", True):
            bucket["error_count"] += 1
        dur = entry.get("duration_ms")
        if isinstance(dur, (int, float)):
            bucket["_durations"].append(float(dur))

    operations: Dict[str, Dict[str, Any]] = {}
    for op, bucket in by_op.items():
        durations = bucket.pop("_durations")
        operations[op] = {
            "count": bucket["count"],
            "error_count": bucket["error_count"],
            "avg_duration_ms": (round(sum(durations) / len(durations), 2) if durations else None),
            "max_duration_ms": (round(max(durations), 2) if durations else None),
        }
    return operations


def summary(client_id: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate stats over the current ring-buffer contents.

    传 ``client_id`` 时只统计该终端；否则返回全量统计并附带 ``clients`` 的
    分终端拆分（count / error_count），便于多终端部署一眼看清各终端健康度。
    """
    with _lock:
        all_items = list(_buffer)

    items = [e for e in all_items if e.get("client_id") == client_id] if client_id else all_items

    total = len(items)
    error_count = sum(1 for e in items if not e.get("ok", True))

    result: Dict[str, Any] = {
        "total": total,
        "error_count": error_count,
        "buffer_capacity": _buffer.maxlen,
        "operations": _operation_stats(items),
    }
    if client_id:
        result["client_id"] = client_id
    else:
        clients: Dict[str, Dict[str, Any]] = {}
        for entry in all_items:
            cid = entry.get("client_id", "default")
            bucket = clients.setdefault(cid, {"count": 0, "error_count": 0})
            bucket["count"] += 1
            if not entry.get("ok", True):
                bucket["error_count"] += 1
        result["clients"] = clients
    return result


def _reset_for_tests() -> None:
    """Clear the ring buffer. Test-only helper."""
    with _lock:
        _buffer.clear()
