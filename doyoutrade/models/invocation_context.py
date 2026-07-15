"""Context for attributing model calls to a worker cycle (instance / run)."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

model_invocation_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "model_invocation_context",
    default=None,
)

model_invocation_call_kind: ContextVar[str | None] = ContextVar(
    "model_invocation_call_kind",
    default=None,
)


def attrs_from_cycle_state(cycle_state: Any | None) -> dict[str, Any]:
    if cycle_state is None:
        return {"task_id": None, "run_id": None, "trace_id": None}
    return {
        "task_id": getattr(cycle_state, "task_id", None),
        "run_id": getattr(cycle_state, "run_id", None),
        "trace_id": getattr(cycle_state, "trace_id", None),
    }


@contextmanager
def model_invocation_scope(
    cycle_state: Any | None,
    kind: str,
    *,
    extras: dict[str, Any] | None = None,
) -> Iterator[None]:
    base = attrs_from_cycle_state(cycle_state)
    if extras:
        base = {**base, **extras}
    ctx_tok = model_invocation_context.set(base)
    kind_tok = model_invocation_call_kind.set(kind)
    try:
        yield
    finally:
        model_invocation_call_kind.reset(kind_tok)
        model_invocation_context.reset(ctx_tok)
