"""Reusable pagination hint helpers for assistant tools.

When a tool returns a page of a larger result set, the model-facing prose
should explicitly tell it how to fetch the rest. Without that, the model
has to guess the kwarg name (``offset`` vs ``page`` vs ``cursor``) and
the carry-over filters. This helper centralises the formatting so every
``list_*`` / paginated tool can opt in with one call.

Example::

    hint = format_pagination_hint(
        tool_name="list_tasks",
        total=350,
        shown=20,
        limit=20,
        offset=0,
        filters={"q": "alpha", "status": None, "mode": None},
    )
    # → "330 more. Call list_tasks(q='alpha', offset=20, limit=20) to see the next page."

Returns ``None`` when there are no further items so callers can append
unconditionally without an ``if``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def format_pagination_hint(
    *,
    tool_name: str,
    total: int,
    shown: int,
    limit: int,
    offset: int,
    filters: Mapping[str, Any] | None = None,
    offset_param: str = "offset",
    limit_param: str = "limit",
) -> str | None:
    """Return a one-line "N more, call <tool>(...) to see the next page" hint.

    ``filters`` is the dict of non-pagination kwargs the caller originally
    sent (e.g. ``{"q": "alpha"}``). ``None`` values are skipped so callers
    can pass through their raw kwargs without filtering first.

    Returns ``None`` when ``total - offset - shown <= 0`` — there is no
    next page so the model needs no hint.
    """

    remaining = int(total) - int(offset) - int(shown)
    if remaining <= 0:
        return None

    parts: list[str] = []
    if filters:
        for key, value in filters.items():
            if value is None:
                continue
            parts.append(f"{key}={value!r}")
    parts.append(f"{offset_param}={int(offset) + int(limit)}")
    parts.append(f"{limit_param}={int(limit)}")

    return (
        f"{remaining} more. Call {tool_name}({', '.join(parts)}) "
        f"to see the next page."
    )


def append_pagination_hint(
    lines: list[str],
    *,
    tool_name: str,
    total: int,
    shown: int,
    limit: int,
    offset: int,
    filters: Mapping[str, Any] | None = None,
    offset_param: str = "offset",
    limit_param: str = "limit",
) -> list[str]:
    """Mutate ``lines`` in place: append a blank line + the hint when
    there is a next page. Returns the same list for chaining."""

    hint = format_pagination_hint(
        tool_name=tool_name,
        total=total,
        shown=shown,
        limit=limit,
        offset=offset,
        filters=filters,
        offset_param=offset_param,
        limit_param=limit_param,
    )
    if hint is not None:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(hint)
    return lines


__all__: Iterable[str] = ("format_pagination_hint", "append_pagination_hint")
