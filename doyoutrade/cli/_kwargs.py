"""Click flag → assistant-tool kwargs translation helpers.

Each CLI write command builds a kwargs dict that the underlying
``OperationHandler.execute`` validates via its own contract chain
(``_enforce_kwargs_contract`` → ``_apply_schema_coercion`` →
``_apply_identifier_guards``). The CLI deliberately does not duplicate
that validation — it only handles the *click-shaped* parts:

* Parse ``--params '<json>'`` into a dict (and surface ``invalid_params_json``
  on failure, with the same exit code 2 the tool contract uses).
* Split comma-separated lists into native arrays.
* Skip ``None`` so click defaults don't accidentally overwrite values
  the tool would otherwise inherit.

Anything more complex (nested object validation, identifier guards, JSON
coercion of object/array fields) belongs in the tool, not here.
"""

from __future__ import annotations

import json
from typing import Any

from doyoutrade.cli._envelope import Meta, error_envelope, exit_code_for_error


def parse_params_json(raw: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Parse a ``--params`` JSON object.

    Returns ``(parsed, error_envelope_or_none)``. On parse failure the
    second element is a ready-to-emit error envelope (without ``meta``;
    caller injects). The shape mirrors the tool-level ``invalid_*_json``
    coercion errors so skill docs can document a single set of tokens.
    """

    if raw is None or raw == "":
        return None, None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        err = error_envelope(
            error_code="invalid_params_json",
            error_type="ValueError",
            message=f"--params must be valid JSON: {exc}",
            hint='Pass a JSON object string, e.g. --params \'{"agent": {"react_max_turns": 3}}\'',
            meta=Meta(),
        )
        return None, err
    if not isinstance(decoded, dict):
        err = error_envelope(
            error_code="invalid_params_json",
            error_type="ValueError",
            message="--params must be a JSON object (got a non-object value)",
            hint='Wrap multiple fields in {}, e.g. --params \'{"universe": [...]}\'',
            meta=Meta(),
        )
        return None, err
    return decoded, None


def split_csv(raw: str | None) -> list[str] | None:
    """Split a comma-separated string into a list, trimming whitespace.

    Returns ``None`` when ``raw`` is ``None`` so callers can preserve the
    "not provided" semantics; empty entries (stray commas) are dropped.
    """

    if raw is None:
        return None
    parts = [seg.strip() for seg in raw.split(",")]
    return [p for p in parts if p]


def merge_flat_over_params(
    params: dict[str, Any] | None,
    flat: dict[str, Any],
) -> dict[str, Any]:
    """Merge ``flat`` over ``params``, dropping ``None`` values from ``flat``.

    Click's default-of-None semantics means optional flags arrive as
    ``None`` when not supplied — we drop those so the tool's
    ``additionalProperties: false`` doesn't reject a noise key. Flat
    values win over ``--params`` keys (the explicit flag is more
    intentional than a json payload).
    """

    out: dict[str, Any] = dict(params or {})
    for key, value in flat.items():
        if value is None:
            continue
        out[key] = value
    return out


def exit_for_invalid_params(envelope: dict[str, Any]) -> int:
    """Map the envelope from :func:`parse_params_json` to its exit code."""

    err = envelope.get("error") if isinstance(envelope, dict) else None
    code = (err or {}).get("error_code", "invalid_params_json")
    return exit_code_for_error(code)


__all__ = [
    "exit_for_invalid_params",
    "merge_flat_over_params",
    "parse_params_json",
    "split_csv",
]
