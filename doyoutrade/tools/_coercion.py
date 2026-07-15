"""Schema-driven input coercion for assistant tool kwargs.

When a tool declares a field as ``type: object`` / ``type: array`` in its
parameters schema, weaker models occasionally serialize the value as a
JSON string instead of the native shape. Without a fallback the tool
either rejects the call with an opaque error or silently passes a
malformed payload downstream.

This module centralizes the polymorphic-input fallback so every tool can
opt in by declaring a list of :class:`SchemaCoercion` rules and calling
:func:`apply_schema_coercion`. The helper:

* tries ``json.loads`` on string values when the declared type is
  ``object`` / ``array``; for ``boolean`` uses case-insensitive word
  matching instead of JSON parsing,
* validates the resulting payload (and array item type when applicable),
* returns a structured ``invalid_<field>_json`` error compatible with
  the existing ``invalid_parameter_hints_json`` precedent.

The :class:`OperationHandler` base class exposes a thin wrapper
(``_apply_schema_coercion``) that defaults to an empty rule set.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SchemaCoercion:
    """Declarative rule for fixing a single tool kwarg's shape.

    ``field`` is the top-level kwarg name. ``declared_type`` must be
    ``"object"``, ``"array"``, or ``"boolean"`` and matches the parameter schema.
    ``item_type`` is required when ``declared_type == "array"`` and
    constrains item types (e.g. ``str``). ``error_code`` defaults to
    ``invalid_<field>_json`` so skill docs can refer to a stable token.
    """

    field: str
    declared_type: str  # "object" | "array" | "boolean"
    item_type: type | None = None
    error_code: str | None = None

    def resolved_error_code(self) -> str:
        return self.error_code or f"invalid_{self.field}_json"


@dataclass(frozen=True)
class CoercionResult:
    """Outcome of :func:`apply_schema_coercion`.

    ``kwargs`` is the (possibly-mutated) kwargs dict the caller should
    use. ``coerced_fields`` lists the fields whose JSON-string value was
    successfully parsed — surface this in debug events so future audits
    can grep for the model/route that keeps stringifying payloads.
    ``error`` is non-``None`` when a rule rejected the input.
    """

    kwargs: dict[str, Any]
    coerced_fields: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None


def apply_schema_coercion(
    kwargs: dict[str, Any],
    rules: tuple[SchemaCoercion, ...] | list[SchemaCoercion],
) -> CoercionResult:
    """Apply each rule in order. Stops at the first failing rule.

    The caller is responsible for emitting any debug events. The
    returned ``error`` payload is structured for the standard
    ``{"status": "error", **error}`` envelope and always carries
    ``error_code`` + ``error_type`` + ``error``.
    """

    normalized = dict(kwargs)
    coerced: list[str] = []
    for rule in rules:
        if rule.field not in normalized:
            continue
        raw = normalized[rule.field]
        if raw is None:
            continue
        coerced_value, was_json_string, err = _coerce_one(raw, rule)
        if err is not None:
            return CoercionResult(
                kwargs=normalized,
                coerced_fields=coerced,
                error=err,
            )
        normalized[rule.field] = coerced_value
        if was_json_string:
            coerced.append(rule.field)
    return CoercionResult(kwargs=normalized, coerced_fields=coerced)


def _coerce_one(
    raw: Any, rule: SchemaCoercion
) -> tuple[Any, bool, dict[str, Any] | None]:
    # Boolean coercion bypasses JSON parsing: it handles native bool directly,
    # and for strings it uses case-insensitive word matching rather than JSON.
    if rule.declared_type == "boolean":
        if isinstance(raw, bool):
            return raw, False, None
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in ("true", "1"):
                return True, True, None
            if normalized in ("false", "0"):
                return False, True, None
        return raw, False, _shape_error(rule, parse_error=False)

    was_json_string = isinstance(raw, str)
    payload: Any = raw
    if was_json_string:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw, False, _shape_error(rule, parse_error=True)

    if rule.declared_type == "object":
        if not isinstance(payload, dict):
            return raw, was_json_string, _shape_error(rule, parse_error=False)
        return payload, was_json_string, None

    if rule.declared_type == "array":
        if isinstance(payload, tuple):
            payload = list(payload)
        if not isinstance(payload, list):
            return raw, was_json_string, _shape_error(rule, parse_error=False)
        if rule.item_type is not None and not all(
            isinstance(item, rule.item_type) for item in payload
        ):
            return raw, was_json_string, _shape_error(rule, parse_error=False)
        return payload, was_json_string, None

    raise ValueError(
        f"unsupported coercion declared_type {rule.declared_type!r} for {rule.field}"
    )


def _shape_error(rule: SchemaCoercion, *, parse_error: bool) -> dict[str, Any]:
    if rule.declared_type == "object":
        shape_label = "an object or JSON object string"
        hint_shape = "object"
    elif rule.declared_type == "array":
        if rule.item_type is str:
            shape_label = "an array of strings or JSON array string"
        else:
            shape_label = "an array or JSON array string"
        hint_shape = "array"
    elif rule.declared_type == "boolean":
        shape_label = "a boolean or one of 'true'/'false'/'1'/'0' (case-insensitive)"
        hint_shape = "boolean"
    else:
        shape_label = f"a value matching declared type {rule.declared_type!r}"
        hint_shape = rule.declared_type

    suffix = " (failed to parse JSON)" if parse_error else ""
    message = f"{rule.field} must be {shape_label}{suffix}"
    return {
        "error_code": rule.resolved_error_code(),
        "error_type": "ValueError",
        "error": message,
        "hint": f"send {rule.field} as a native {hint_shape}, not a JSON string",
    }


__all__ = [
    "CoercionResult",
    "SchemaCoercion",
    "apply_schema_coercion",
]
