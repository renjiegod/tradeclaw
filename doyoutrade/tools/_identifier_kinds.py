"""Declarative identifier-kind guards.

Doyoutrade's primary id families have visually distinct shapes:

* ``task_id``           — uuid (no ``sd-`` / ``wl-`` prefix)
* ``definition_id``     — ``sd-...``
* ``watchlist_entry_id`` — ``wl-...``

Models occasionally pass one where another is expected (e.g. handing a
``sd-`` to ``get_task``). Each tool used to special-case this guard
inline; this module centralizes the shape rules so any tool can opt in
by declaring an :class:`IdentifierGuard` per parameter and letting the
:class:`OperationHandler` base class run the check before its own logic.

The error payload mirrors the legacy ``wrong_identifier_type_error``
shape so existing skills / callers keep working.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class IdentifierKind(str, Enum):
    TASK_ID = "task_id"
    DEFINITION_ID = "definition_id"
    WATCHLIST_ENTRY_ID = "watchlist_entry_id"
    MONITOR_ID = "monitor_id"


_KIND_LABELS: dict[IdentifierKind, str] = {
    IdentifierKind.TASK_ID: "task id",
    IdentifierKind.DEFINITION_ID: "strategy definition id",
    IdentifierKind.WATCHLIST_ENTRY_ID: "watchlist entry id",
    IdentifierKind.MONITOR_ID: "monitor rule id",
}

_KIND_REPAIR_TOOL: dict[IdentifierKind, str] = {
    IdentifierKind.TASK_ID: "get_task",
    IdentifierKind.DEFINITION_ID: "get_strategy_definition",
    IdentifierKind.WATCHLIST_ENTRY_ID: "doyoutrade-cli watchlist list/get",
    IdentifierKind.MONITOR_ID: "doyoutrade-cli monitor list/get",
}

# Map prefix → kind for shape detection. The empty prefix is reserved
# for ``TASK_ID`` (uuid-style values without a known prefix).
_PREFIX_TO_KIND: dict[str, IdentifierKind] = {
    "sd-": IdentifierKind.DEFINITION_ID,
    "wl-": IdentifierKind.WATCHLIST_ENTRY_ID,
    "mon-": IdentifierKind.MONITOR_ID,
}


@dataclass(frozen=True)
class IdentifierGuard:
    """Bind a kwarg name to its expected :class:`IdentifierKind`."""

    field: str
    kind: IdentifierKind


def detect_identifier_kind(value: str) -> IdentifierKind:
    """Best-effort classification of an id by visible shape.

    Values that don't carry a known prefix (``sd-`` / ``wl-``) are
    classified as :attr:`IdentifierKind.TASK_ID` (the uuid family).
    """

    for prefix, kind in _PREFIX_TO_KIND.items():
        if value.startswith(prefix):
            return kind
    return IdentifierKind.TASK_ID


def check_identifier_kind(
    value: Any, expected: IdentifierKind, *, field: str
) -> dict[str, Any] | None:
    """Return a structured error dict when ``value`` does not match.

    Returns ``None`` when ``value`` is missing/empty (the guard does not
    enforce presence — leave that to the schema's ``required`` list) or
    when its shape matches ``expected``.
    """

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    actual = detect_identifier_kind(value)
    if actual == expected:
        return None
    return _build_mismatch_error(
        field=field, expected=expected, actual=actual, value=value
    )


def _build_mismatch_error(
    *, field: str, expected: IdentifierKind, actual: IdentifierKind, value: str
) -> dict[str, Any]:
    expected_label = _KIND_LABELS[expected]
    actual_label = _KIND_LABELS[actual]
    expected_tool = _KIND_REPAIR_TOOL[expected]
    actual_tool = _KIND_REPAIR_TOOL[actual]
    return {
        "status": "error",
        "error_code": "wrong_identifier_type",
        "error_type": "WrongIdentifierType",
        "error": (
            f"{field}={value!r} looks like a {actual_label}, "
            f"but {field} requires a {expected_label}"
        ),
        "field": field,
        "expected_kind": expected.value,
        "actual_kind": actual.value,
        "repair_hints": [
            f"pass a {expected_label} to {field}",
            f"use {actual_tool} to look up the {actual_label}",
            f"use {expected_tool} to discover the right {expected_label}",
        ],
    }


def apply_identifier_guards(
    kwargs: dict[str, Any], guards: tuple[IdentifierGuard, ...] | list[IdentifierGuard]
) -> dict[str, Any] | None:
    """Run each guard in declaration order; return the first error or None."""

    for guard in guards:
        err = check_identifier_kind(kwargs.get(guard.field), guard.kind, field=guard.field)
        if err is not None:
            return err
    return None


__all__ = [
    "IdentifierGuard",
    "IdentifierKind",
    "apply_identifier_guards",
    "check_identifier_kind",
    "detect_identifier_kind",
]
