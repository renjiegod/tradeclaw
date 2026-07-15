"""Condition-tree JSON schema + validator (the single shared validator).

Called by the API / CLI / any tool *before* a ``condition_json`` reaches the
``monitor_rules`` repo, so a malformed tree is a structured rejection — never a
runtime "try it and see" (CLAUDE.md §错误可见性). Mirrors
``doyoutrade.runtime.triggers.TriggerValidationError`` so the API surfaces a
stable ``error_code`` + ``message`` (+ optional ``field``). These codes are
stable tokens skill docs may reference.

Tree grammar (validated + normalized):

- logical node  : ``{"op": "and"|"or", "children": [<node>, ...]}``  (non-empty)
- preset leaf   : ``{"preset": "<name>", "params": {...}?}``
- predicate leaf: ``{"predicate": {"field": <name>, "op": <sym>, "value": <num>}}``
"""

from __future__ import annotations

from typing import Any

from doyoutrade.monitoring.evaluator import MAX_TREE_DEPTH, PREDICATE_FIELDS, PREDICATE_OPS
from doyoutrade.monitoring.presets import PRESET_NAMES

_OPS = ("and", "or")


class MonitorConditionError(ValueError):
    """Structured condition-tree validation failure carrying a stable error_code.

    Error codes:
      - ``condition_empty``           — tree missing / not a dict
      - ``condition_node_invalid``    — node lacks op/preset/predicate, or wrong type
      - ``condition_op_unknown``      — logical node op not in ('and','or')
      - ``condition_children_empty``  — logical node with no children list
      - ``condition_preset_unknown``  — preset leaf names an unregistered detector
      - ``condition_params_invalid``  — preset params present but not a dict
      - ``condition_predicate_invalid``— predicate leaf missing/bad field/op/value
      - ``condition_depth_exceeded``  — tree nested deeper than MAX_TREE_DEPTH
    """

    def __init__(self, error_code: str, message: str, *, field: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.field = field

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.field:
            payload["field"] = self.field
        return payload


def _validate_node(node: Any, depth: int) -> dict[str, Any]:
    if depth > MAX_TREE_DEPTH:
        raise MonitorConditionError(
            "condition_depth_exceeded",
            f"condition tree nested deeper than {MAX_TREE_DEPTH}",
        )
    if not isinstance(node, dict):
        raise MonitorConditionError(
            "condition_node_invalid",
            f"condition node must be an object, got {type(node).__name__}: {node!r}",
        )

    if "op" in node:
        op = node.get("op")
        if op not in _OPS:
            raise MonitorConditionError(
                "condition_op_unknown",
                f"node.op must be one of {_OPS}, got {op!r}",
                field="op",
            )
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise MonitorConditionError(
                "condition_children_empty",
                "logical node requires a non-empty 'children' list",
                field="children",
            )
        return {"op": op, "children": [_validate_node(c, depth + 1) for c in children]}

    if "preset" in node:
        name = node.get("preset")
        if name not in PRESET_NAMES:
            raise MonitorConditionError(
                "condition_preset_unknown",
                f"preset must be one of {tuple(sorted(PRESET_NAMES))}, got {name!r}",
                field="preset",
            )
        params = node.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise MonitorConditionError(
                "condition_params_invalid",
                f"preset params must be an object, got {type(params).__name__}",
                field="params",
            )
        return {"preset": name, "params": params}

    if "predicate" in node:
        pred = node.get("predicate")
        if not isinstance(pred, dict):
            raise MonitorConditionError(
                "condition_predicate_invalid",
                "predicate must be an object {field, op, value}",
                field="predicate",
            )
        fld = pred.get("field")
        op = pred.get("op")
        value = pred.get("value")
        if fld not in PREDICATE_FIELDS:
            raise MonitorConditionError(
                "condition_predicate_invalid",
                f"predicate.field must be one of {PREDICATE_FIELDS}, got {fld!r}",
                field="predicate.field",
            )
        if op not in PREDICATE_OPS:
            raise MonitorConditionError(
                "condition_predicate_invalid",
                f"predicate.op must be one of {tuple(PREDICATE_OPS)}, got {op!r}",
                field="predicate.op",
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MonitorConditionError(
                "condition_predicate_invalid",
                f"predicate.value must be a number, got {value!r}",
                field="predicate.value",
            )
        return {"predicate": {"field": fld, "op": op, "value": value}}

    raise MonitorConditionError(
        "condition_node_invalid",
        f"condition node must contain 'op', 'preset', or 'predicate': {node!r}",
    )


def validate_condition_tree(tree: Any) -> dict[str, Any]:
    """Validate + normalize a condition tree. Raises :class:`MonitorConditionError`."""
    if not isinstance(tree, dict) or not tree:
        raise MonitorConditionError(
            "condition_empty",
            "condition_json must be a non-empty object (a node or a leaf)",
            field="condition_json",
        )
    return _validate_node(tree, 0)


def iter_referenced_presets(tree: Any) -> set[str]:
    """Collect every preset name referenced by the (already-valid) tree."""
    out: set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if "children" in node and isinstance(node["children"], list):
            for child in node["children"]:
                walk(child)
        elif "preset" in node:
            out.add(node["preset"])

    walk(tree)
    return out


def preset_leaf(name: str, params: dict | None = None) -> dict[str, Any]:
    """Convenience: build a single-preset condition tree leaf (used by CLI --preset)."""
    leaf: dict[str, Any] = {"preset": name}
    if params:
        leaf["params"] = params
    return leaf
