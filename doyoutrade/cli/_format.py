"""Output format dispatch for the CLI.

Currently supports ``json`` (default, compact one-line) and ``pretty``
(indented json). NDJSON is reserved for streaming commands (``backtest
watch`` etc.) that will land in Phase 2; table / csv land later when
human-facing UX justifies the cost. The format flag is a global click
option on the root command (``--format``) so every subcommand inherits.
"""

from __future__ import annotations

import json
import sys
from typing import Any

FORMAT_JSON = "json"
FORMAT_PRETTY = "pretty"
FORMAT_NDJSON = "ndjson"

SUPPORTED_FORMATS = (FORMAT_JSON, FORMAT_PRETTY, FORMAT_NDJSON)


def write_envelope(envelope: dict[str, Any], *, fmt: str = FORMAT_JSON, stream: Any = None) -> None:
    """Serialize and write one envelope according to ``fmt``.

    ``ndjson`` here is treated identically to ``json`` for a single
    envelope; streaming commands call this once per event. The newline
    terminator is always written so NDJSON consumers can split on ``\n``.
    """

    out = stream or sys.stdout
    if fmt == FORMAT_PRETTY:
        body = json.dumps(envelope, ensure_ascii=False, indent=2, default=str)
    else:
        body = json.dumps(envelope, ensure_ascii=False, default=str)
    out.write(body)
    out.write("\n")
    out.flush()


__all__ = [
    "FORMAT_JSON",
    "FORMAT_NDJSON",
    "FORMAT_PRETTY",
    "SUPPORTED_FORMATS",
    "write_envelope",
]
