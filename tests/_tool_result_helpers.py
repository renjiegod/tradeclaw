"""Shared helpers for assistant tool tests.

Single-channel tool results carry their structured payload inside a
fenced ``json`` code block inside ``ToolResult.text``. Tests that need
to inspect those fields parse the block via :func:`payload`.
"""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


def payload(result: Any) -> dict:
    """Return the structured payload embedded in ``result.text``.

    Works for both raw ``str`` (legacy registry-routed results) and
    ``ToolResult`` / ``_ToolResultStr`` objects. Returns ``{}`` when no
    JSON block is present (i.e. confirmation-style tools that only
    carry prose — no structured payload to assert on).
    """

    text = result if isinstance(result, str) else result.text
    match = _JSON_BLOCK.search(text)
    if match is None:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


__all__ = ("payload",)
