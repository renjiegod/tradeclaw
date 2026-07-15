"""Shared kwargs-contract layer for assistant tools.

This module centralizes the logic that historically lived inside
``CreateTaskTool``:

* lifting legacy top-level kwargs into their canonical nested location
  (e.g. ``universe`` -> ``settings.universe``) with optional JSON-string
  fallback, and
* rejecting truly unknown top-level kwargs with a structured
  ``unknown_arguments`` payload that names the offending field, lists
  the allowed top-level keys, and emits a ``suggested_path`` map so the
  agent can retry with the correct shape.

Tools opt into the contract by declaring ``legacy_top_level_lifts`` and
calling :func:`enforce_kwargs_contract` from their ``execute`` entry
point. The base :class:`OperationHandler` exposes a thin wrapper
(``_enforce_kwargs_contract``) that defaults ``allowed_top_level`` to
the keys declared on ``cls.parameters['properties']``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class LegacyLift:
    """Declarative migration for a single legacy top-level kwarg.

    The lift fires when ``source`` is present at the top of ``kwargs``.
    The raw value is JSON-decoded if it is a string, then handed to
    ``coerce`` for shape validation. The validated payload is written
    into ``target_path`` (a dotted path like ``"settings.universe"``).

    ``meta_moved_key`` and ``meta_was_json_string_key`` name the boolean
    flags emitted into the request debug event so downstream audits can
    grep for the specific normalization that fired.
    """

    target_path: str
    coerce: Callable[[Any], Any]
    json_string_error: str
    meta_moved_key: str
    meta_was_json_string_key: str


@dataclass(frozen=True)
class ContractResult:
    """Outcome of :func:`enforce_kwargs_contract`.

    ``kwargs`` is the (possibly-mutated) kwargs dict the caller should
    use going forward. ``legacy_normalization`` is the metadata to
    surface in the ``.request`` debug event when the call ultimately
    succeeds. ``error`` is non-``None`` when the contract rejected the
    input — callers should return immediately and emit the matching
    ``.failed`` (validation_error) or ``.rejected`` (unknown_arguments)
    debug event.
    """

    kwargs: dict[str, Any]
    legacy_normalization: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    error_kind: str | None = None  # "validation_error" | "unknown_arguments"


def coerce_object_payload(field_name: str) -> Callable[[Any], dict[str, Any]]:
    """Return a coercion that validates a JSON object payload."""

    def _coerce(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError(f"{field_name} must be an object or JSON object string")
        return raw

    return _coerce


def coerce_string_array_payload(field_name: str) -> Callable[[Any], list[str]]:
    """Return a coercion that validates a JSON array of strings."""

    def _coerce(raw: Any) -> list[str]:
        if isinstance(raw, tuple):
            raw = list(raw)
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ValueError(
                f"{field_name} must be an array of strings or JSON array string"
            )
        return raw

    return _coerce


def enforce_kwargs_contract(
    kwargs: dict[str, Any],
    *,
    allowed_top_level: frozenset[str] | set[str],
    suggested_paths: dict[str, str],
    legacy_lifts: dict[str, LegacyLift],
    autocreate_missing_parents: bool = False,
) -> ContractResult:
    """Apply legacy lifts then reject unknown top-level kwargs.

    The ``kwargs`` mapping is shallow-copied; the original is not
    mutated. Nested dicts on the lift path are also shallow-copied so
    callers' state stays untouched.

    ``autocreate_missing_parents``: when True, missing intermediate
    dicts on the lift's ``target_path`` are auto-created instead of
    raising ``ValueError``. Useful for partial-update tools like
    ``update_task`` where ``settings`` may legitimately be omitted.
    """

    normalized = dict(kwargs)
    legacy_meta: dict[str, Any] = {}

    for source, lift in legacy_lifts.items():
        if source not in normalized:
            continue
        try:
            parent, leaf_key = _resolve_parent(
                normalized,
                lift.target_path,
                autocreate=autocreate_missing_parents,
            )
            raw = normalized.pop(source)
            was_json_string = isinstance(raw, str)
            payload: Any = raw
            if was_json_string:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(lift.json_string_error) from exc
            payload = lift.coerce(payload)
            if leaf_key in parent:
                raise ValueError(
                    f"cannot provide both top-level {source} and {lift.target_path}"
                )
            parent[leaf_key] = payload
            legacy_meta[lift.meta_moved_key] = True
            legacy_meta[lift.meta_was_json_string_key] = was_json_string
        except ValueError as exc:
            return ContractResult(
                kwargs=normalized,
                legacy_normalization=legacy_meta,
                error={"type": "validation_error", "message": str(exc)},
                error_kind="validation_error",
            )

    unknown_keys = sorted(set(normalized) - set(allowed_top_level))
    if unknown_keys:
        return ContractResult(
            kwargs=normalized,
            legacy_normalization=legacy_meta,
            error=_build_unknown_arguments_error(
                unknown_keys=unknown_keys,
                allowed_top_level=allowed_top_level,
                suggested_paths=suggested_paths,
            ),
            error_kind="unknown_arguments",
        )

    return ContractResult(kwargs=normalized, legacy_normalization=legacy_meta)


def _resolve_parent(
    kwargs: dict[str, Any], target_path: str, *, autocreate: bool = False
) -> tuple[dict[str, Any], str]:
    """Walk ``target_path`` and shallow-copy each parent on the way.

    Returns ``(parent_dict, leaf_key)`` where ``parent_dict`` is the
    freshly-copied dict that the caller can write the lifted payload
    into. When ``autocreate`` is False, raises ``ValueError`` if any
    intermediate node is missing or not a dict. When True, missing
    parents are inserted as fresh empty dicts; non-dict existing
    parents still raise.
    """

    parts = target_path.split(".")
    if len(parts) < 2:
        raise ValueError(
            f"legacy lift target must be nested (got {target_path!r})"
        )
    *parent_parts, leaf_key = parts
    cursor = kwargs
    traversed: list[str] = []
    for part in parent_parts:
        traversed.append(part)
        existing = cursor.get(part)
        if existing is None and autocreate:
            new_parent: dict[str, Any] = {}
            cursor[part] = new_parent
            cursor = new_parent
            continue
        if not isinstance(existing, dict):
            raise ValueError(f"{'.'.join(traversed)} must be an object")
        copy = dict(existing)
        cursor[part] = copy
        cursor = copy
    return cursor, leaf_key


def _build_unknown_arguments_error(
    *,
    unknown_keys: list[str],
    allowed_top_level: frozenset[str] | set[str],
    suggested_paths: dict[str, str],
) -> dict[str, Any]:
    suggested_path = {
        key: suggested_paths[key]
        for key in unknown_keys
        if key in suggested_paths
    }
    allowed_sorted = sorted(allowed_top_level)
    if suggested_path:
        moves = ", ".join(
            f"'{src}' -> '{dest}'" for src, dest in sorted(suggested_path.items())
        )
        message = (
            f"unknown top-level argument(s) {unknown_keys}; "
            f"move them under their canonical path ({moves}). "
            f"Allowed top-level keys: {allowed_sorted}."
        )
        hint = (
            "Move these keys under their canonical path; only "
            + "/".join(allowed_sorted)
            + " are accepted at the top level."
        )
    else:
        message = (
            f"unknown top-level argument(s) {unknown_keys}; "
            f"allowed top-level keys are {allowed_sorted}."
        )
        hint = None

    error: dict[str, Any] = {
        "type": "unknown_arguments",
        "message": message,
        "unknown": unknown_keys,
        "allowed_top_level": allowed_sorted,
    }
    if suggested_path:
        error["suggested_path"] = suggested_path
    if hint is not None:
        error["hint"] = hint
    return error


__all__ = [
    "ContractResult",
    "LegacyLift",
    "coerce_object_payload",
    "coerce_string_array_payload",
    "enforce_kwargs_contract",
]
