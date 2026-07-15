from __future__ import annotations

import math
from decimal import Decimal
from typing import Any


def decimal_from_number(x: int | float | str | Decimal) -> Decimal:
    """Build a :class:`Decimal` for ledger / snapshot math.

    For finite floats, uses :func:`str` (not :func:`repr`) so values like ``89999.12``
    become ``Decimal('89999.12')`` instead of carrying IEEE 754 expansion noise from
    ``repr`` (e.g. ``89999.120000000006712``). Non-finite floats map to zero.
    """
    if isinstance(x, Decimal):
        return x
    if isinstance(x, int):
        return Decimal(x)
    if isinstance(x, float):
        if not math.isfinite(x):
            return Decimal(0)
        return Decimal(str(x))
    s = str(x).strip()
    if not s:
        return Decimal(0)
    return Decimal(s)


def decimal_to_json_str(d: Decimal) -> str:
    """Serialize a decimal for JSON: fixed-point string, no scientific notation.

    Strips **spurious trailing zeros** after ``format(..., 'f')`` so values like
    ``100000.0000000000000000`` (same number as ``100000``) do not leak
    ``Context.prec`` / operand exponent width into the API. This is not rounding:
    the numeric value is unchanged.
    """
    if not d.is_finite():
        return "0"
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def json_sanitize(value: Any) -> Any:
    """Recursively turn ``Decimal`` into strings for nested dict/list payloads."""
    if isinstance(value, Decimal):
        return decimal_to_json_str(value)
    if isinstance(value, dict):
        return {k: json_sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_sanitize(v) for v in value]
    if isinstance(value, tuple):
        return tuple(json_sanitize(v) for v in value)
    return value


def json_default_with_decimals(obj: Any) -> Any:
    """Use as ``json.dumps(..., default=json_default_with_decimals)`` project-wide.

    :class:`decimal.Decimal` is serialized with :func:`decimal_to_json_str` (no spurious
    trailing zeros). Any other non-JSON-native value falls back to ``str(obj)``, matching
    the previous ``default=str`` behavior for UUIDs, datetimes, etc.
    """
    if isinstance(obj, Decimal):
        return decimal_to_json_str(obj)
    return str(obj)
