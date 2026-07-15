"""Condition-tree evaluation: EvalContext, field predicates, full-eval tree walk.

The tree is ``{op: 'and'|'or', children: [...]}`` where each child is either a
nested node, a preset leaf ``{preset: '<name>', params?: {...}}``, or a predicate
leaf ``{predicate: {field, op, value}}``.

Evaluation is FULL (never short-circuit): every leaf is evaluated so the alert
card can show *why* each condition did or did not fire (CLAUDE.md §错误可见性 —
diagnostics over free text). A malformed node that slips past create-time
validation raises :class:`MonitorEvalError` (a structured, distinguishable
failure) rather than silently evaluating to False; the daemon isolates that to
the one rule's tick and emits ``monitor_condition_tree_invalid``.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from doyoutrade.monitoring.state import SymbolIntradayState

# Predicate operators (canonical symbols; the validator enforces this set).
PREDICATE_OPS: dict[str, Callable[[Any, Any], bool]] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

# Whitelisted predicate fields → how to read them from snapshot / state.
# A field that resolves to None means "unavailable" and the predicate skips
# (returns False with a skipped_reason) rather than coercing a default.
PREDICATE_FIELDS: tuple[str, ...] = (
    "price",
    "change_pct",
    "bid_vol1",
    "ask_vol1",
    "limit_up_price",
    "limit_down_price",
    "seal_peak_bid",
    "seal_peak_ask",
    "volume",
    "amount",
)

_STATE_FIELDS = {"seal_peak_bid", "seal_peak_ask"}

MAX_TREE_DEPTH = 8


class MonitorEvalError(Exception):
    """A condition tree malformed at evaluation time (defense in depth)."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass
class EvalContext:
    snapshot: Any  # QuoteSnapshot (with bid_vol1/ask_vol1/limit_*_price)
    state: SymbolIntradayState
    now: datetime


@dataclass
class LeafDiag:
    """One evaluated leaf — collected for the alert payload / debug event."""

    kind: str  # "preset" | "predicate"
    label: str
    triggered: bool
    diagnostics: dict = field(default_factory=dict)


def resolve_field(name: str, ctx: EvalContext) -> Any:
    if name in _STATE_FIELDS:
        return getattr(ctx.state, name, None)
    return getattr(ctx.snapshot, name, None)


def eval_predicate(leaf: dict, ctx: EvalContext) -> LeafDiag:
    pred = leaf.get("predicate")
    if not isinstance(pred, dict):
        raise MonitorEvalError("predicate_malformed", f"predicate leaf missing dict: {leaf!r}")
    fld = pred.get("field")
    op = pred.get("op")
    value = pred.get("value")
    if fld not in PREDICATE_FIELDS:
        raise MonitorEvalError(
            "predicate_field_unknown",
            f"predicate.field must be one of {PREDICATE_FIELDS}, got {fld!r}",
        )
    fn = PREDICATE_OPS.get(op)
    if fn is None:
        raise MonitorEvalError(
            "predicate_op_unknown",
            f"predicate.op must be one of {tuple(PREDICATE_OPS)}, got {op!r}",
        )
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise MonitorEvalError(
            "predicate_value_invalid",
            f"predicate.value must be a number, got {value!r}",
        )
    label = f"{fld} {op} {value}"
    actual = resolve_field(fld, ctx)
    if actual is None:
        return LeafDiag(
            kind="predicate",
            label=label,
            triggered=False,
            diagnostics={
                "field": fld,
                "skipped_reason": "field_unavailable",
                "hint": "snapshot/state field is None (quote not yet seen or seal data missing)",
            },
        )
    triggered = bool(fn(actual, value))
    return LeafDiag(
        kind="predicate",
        label=label,
        triggered=triggered,
        diagnostics={"field": fld, "op": op, "value": value, "actual": actual},
    )


def eval_preset(leaf: dict, ctx: EvalContext, detectors: dict) -> LeafDiag:
    name = leaf.get("preset")
    params = leaf.get("params") or {}
    detector = detectors.get(name)
    if detector is None:
        raise MonitorEvalError(
            "preset_unknown",
            f"unknown preset detector: {name!r} (registered: {tuple(detectors)})",
        )
    triggered, diagnostics = detector(ctx, params)
    return LeafDiag(
        kind="preset",
        label=str(name),
        triggered=bool(triggered),
        diagnostics=dict(diagnostics or {}),
    )


def _evaluate(node: Any, ctx: EvalContext, detectors: dict, out: list[LeafDiag], depth: int) -> bool:
    if depth > MAX_TREE_DEPTH:
        raise MonitorEvalError("depth_exceeded", f"condition tree deeper than {MAX_TREE_DEPTH}")
    if not isinstance(node, dict):
        raise MonitorEvalError("node_invalid", f"condition node must be a dict, got {node!r}")

    if "op" in node:
        op = node.get("op")
        children = node.get("children")
        if op not in ("and", "or"):
            raise MonitorEvalError("op_unknown", f"node.op must be 'and'|'or', got {op!r}")
        if not isinstance(children, list) or not children:
            raise MonitorEvalError("children_empty", f"logical node needs non-empty children: {node!r}")
        # FULL eval: evaluate every child so all leaves get diagnostics.
        results = [_evaluate(child, ctx, detectors, out, depth + 1) for child in children]
        return all(results) if op == "and" else any(results)

    if "preset" in node:
        diag = eval_preset(node, ctx, detectors)
        out.append(diag)
        return diag.triggered

    if "predicate" in node:
        diag = eval_predicate(node, ctx)
        out.append(diag)
        return diag.triggered

    raise MonitorEvalError(
        "node_invalid",
        f"condition node must have 'op', 'preset', or 'predicate': {node!r}",
    )


def evaluate_tree(
    node: Any,
    ctx: EvalContext,
    *,
    detectors: dict | None = None,
) -> tuple[bool, list[LeafDiag]]:
    """Walk the condition tree. Returns (triggered, per-leaf diagnostics).

    ``detectors`` is the preset registry; defaults to the built-in 6 (lazy import
    avoids an import cycle evaluator → presets → evaluator).
    """
    if detectors is None:
        from doyoutrade.monitoring.presets import PRESET_DETECTORS

        detectors = PRESET_DETECTORS
    leaves: list[LeafDiag] = []
    triggered = _evaluate(node, ctx, detectors, leaves, 0)
    return triggered, leaves
