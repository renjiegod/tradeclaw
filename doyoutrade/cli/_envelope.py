"""CLI output envelope, exit code rules, and ToolResult → JSON translation.

The CLI mirrors lark-cli's two-shape envelope: every command emits exactly
one structured payload on stdout (success or error), and the exit code
distinguishes failure modes so shell pipelines can branch without parsing
JSON. The envelope shape is contract-stable: skill docs reference these
keys and doyoutrade-cli callers (agent or human) parse them.

Success envelope::

    {
      "ok": true,
      "data": {...},                # tool-specific
      "meta": {                     # set whenever the env carries debug context
        "agent_id": "asst_...",
        "session_id": "sess_...",
        "debug_session_id": "dbg_...",
        "run_id": "run_..."         # optional, only when set
      },
      "_notice": {...}              # optional cross-cutting notices
    }

Error envelope::

    {
      "ok": false,
      "error": {
        "error_code": "...",         # stable token from existing tool error_code
        "error_type": "...",         # exception class or "validation_error"
        "message": "...",            # human-readable
        "hint": "...",               # optional repair suggestion
        "repair_hints": [...]        # optional, mirrors tool dicts
      },
      "meta": {...},
      "_notice": {...}
    }

Existing assistant tools return ``ToolResult(text=..., is_error=...)`` where
``text`` mixes prose with a fenced markdown json block (see
``doyoutrade/tools/_prose.append_json_payload``). This module parses the
fenced block back out so the CLI can surface the structured payload as
``data`` and keep the prose as ``data._summary``.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any

# Exit codes — stable contract for shell callers. Keep narrow so skill
# docs can document each one.
EXIT_OK = 0
EXIT_FAILURE = 1            # business failure (tool returned is_error without contract/identifier error)
EXIT_VALIDATION = 2         # input validation: unknown_arguments / validation_error / wrong_identifier_type / invalid_*_json / missing_*
EXIT_NOT_FOUND = 3          # task_not_found, skill_not_found, bash_task_not_found, file_not_found, etc.
EXIT_INTERNAL = 10          # uncaught exception from CLI itself (not from the tool)

# Error codes that map to EXIT_VALIDATION.
_VALIDATION_ERROR_CODES = frozenset(
    {
        "unknown_arguments",
        "validation_error",
        "wrong_identifier_type",
        "missing_query",
        "missing_name",
        "missing_strategy_task_ids",
        "missing_task_or_definition_id",
        "missing_task_id",
        "unsupported_action",
        "unsupported_file_type",
        # Mutually-exclusive flag combinations (e.g. ``data run --period``
        # vs ``--start/--end``). Per CLAUDE.md "no silent fallback", we
        # refuse to pick one for the caller and surface a structured token
        # they can branch on. Doesn't match the ``invalid_`` / ``missing_``
        # prefix rule, so list it explicitly.
        "conflicting_range_args",
        # Click-level structured errors (raised by main() before any tool runs).
        # Skill docs reference these tokens so agents can self-correct after a
        # single CLI round trip instead of hand-parsing prose like
        # "Error: No such command 'list'.".
        "unknown_command",
        "unknown_option",
        "missing_parameter",
        "usage_error",
    }
)

# User-utterance shortcuts mapped to real CLI entry points. When click raises
# a ``No such command 'list'.`` UsageError we look the unknown name up here and
# include the mapped command in the envelope's ``suggested_path`` so the model
# can self-correct on the next turn. Keep this list tight — only canonical
# aliases that work across every subgroup. Subgroup-specific suggestions are
# better surfaced via ``difflib.get_close_matches`` against the actual
# ``group.commands`` keys.
CLI_COMMAND_ALIASES: dict[str, str] = {
    "list": "inspect",
    "ls": "inspect",
    "show": "get",
    "describe": "get",
    "rm": "delete",
}

# Cross-command option-name aliases used when difflib can't suggest a
# close match. Maps each "intuitive but wrong" flag to a list of
# canonical flags the agent should consider, ordered by likelihood.
# The lookup is intentionally simple — just the bare option name (with
# leading dashes stripped, lowercased). Per-command precision is the
# structured error envelope's job (Click's own ``exc.possibilities``).
#
# Keep this table small and obviously correct: it only fires when Click
# itself has no close match, so an entry here saves the agent a round
# trip to ``--help``. Resist the urge to add fuzzy matches — that is
# difflib's job. If an aliased flag doesn't exist on the current
# command, the model will discover that on its next attempt with one
# fewer round-trip than today.
CLI_OPTION_ALIASES: dict[str, list[str]] = {
    "start": ["--range-start", "--period", "--start-date"],
    "end": ["--range-end", "--period", "--end-date"],
    "from": ["--range-start"],
    "to": ["--range-end"],
    "since": ["--range-start", "--period"],
    "until": ["--range-end"],
    "limit": ["--max-events"],
    "count": ["--limit"],
    "format": ["--format"],
    "output": ["--format"],
}

# Per-command (command_path_suffix, option_name) → positional-argument hint.
# Click renders ``unknown_option`` with empty ``possibilities`` when the
# user passed a flag for what is actually a positional argument. Agents
# trained on flag-style CLIs (most are) then hunt for the right flag name
# via ``--help`` (request1.json turn 9-12 burnt 4 tool calls discovering
# ``analysis pattern`` takes a positional ``CODE``, not ``--symbol``).
# Listing the common synonyms here lets the envelope reply
# ``positional_argument: "code"`` plus a corrected example invocation in
# one round trip.
#
# ``command_path_suffix`` matches the tail of click's
# ``ctx.command_path`` (e.g. ``"analysis pattern"`` matches
# ``doyoutrade-cli analysis pattern``). ``option_name`` is the bare flag
# (no leading dashes, lowercased). The value is a tuple of (positional
# argument name, example value used in the suggested invocation).
#
# Keep entries tight — every line saves an agent multiple round trips,
# but adding an entry for a flag that *does* exist would mislead. When
# in doubt run ``doyoutrade-cli <cmd> --help`` and verify.
CLI_POSITIONAL_ARG_HINTS: dict[tuple[str, str], tuple[str, str]] = {
    ("analysis pattern", "symbol"): ("code", "600522.SH"),
    ("analysis pattern", "code"): ("code", "600522.SH"),
    ("analysis pattern", "ticker"): ("code", "600522.SH"),
    ("analysis pattern", "stock"): ("code", "600522.SH"),
    ("analysis indicators", "symbol"): ("code", "600522.SH"),
    ("analysis indicators", "code"): ("code", "600522.SH"),
    ("analysis indicators", "ticker"): ("code", "600522.SH"),
    ("analysis indicators", "stock"): ("code", "600522.SH"),
    ("stock lookup", "q"): ("query", "中天科技"),
    ("stock lookup", "query"): ("query", "中天科技"),
    ("stock lookup", "name"): ("query", "中天科技"),
    ("stock lookup", "keyword"): ("query", "中天科技"),
    ("backtest summary", "run-id"): ("run_id", "btjob-<id>"),
    ("backtest summary", "id"): ("run_id", "btjob-<id>"),
    ("backtest summary", "run"): ("run_id", "btjob-<id>"),
    ("backtest watch", "run-id"): ("run_id", "btjob-<id>"),
    ("backtest watch", "id"): ("run_id", "btjob-<id>"),
    ("backtest watch", "run"): ("run_id", "btjob-<id>"),
    ("debug get-run-view", "run-id"): ("run_id", "btjob-<id>"),
    ("debug get-run-view", "id"): ("run_id", "btjob-<id>"),
    ("cycle get", "run-id"): ("run_id", "btjob-<id>"),
    ("cycle get", "id"): ("run_id", "btjob-<id>"),
}

# Per-command (command_path_suffix, option_name) → the sibling command the
# flag *actually* lives on, plus a corrected example invocation. Fires when
# an agent puts a flag on the wrong sibling command — distinct from
# ``CLI_OPTION_ALIASES`` (which suggests another flag on the *same* command)
# and ``CLI_POSITIONAL_ARG_HINTS`` (flag→positional on the same command).
#
# Motivating case (tmp/messages.json turn 10): the agent ran
# ``data fundamentals --min-float-mv 1e10`` to *filter* by float-cap, but
# ``data fundamentals`` only *reports* the value — the float-cap predicate
# lives on ``stock screen``. Without this hint the agent abandoned the
# first-party CLI entirely and fell back to raw akshare (timeout hell).
#
# ``command_path_suffix`` matches the tail of click's ``ctx.command_path``;
# ``option_name`` is the bare flag (no dashes, lowercased). The value is
# (canonical command, corrected example invocation).
CLI_OPTION_WRONG_COMMAND_HINTS: dict[tuple[str, str], tuple[str, str]] = {
    ("data fundamentals", "min-float-mv"): (
        "stock screen",
        "doyoutrade-cli stock screen --universe-file u.txt --min-float-mv 1e10",
    ),
    ("data fundamentals", "max-float-mv"): (
        "stock screen",
        "doyoutrade-cli stock screen --universe-file u.txt --max-float-mv 1e11",
    ),
    ("data run", "min-float-mv"): (
        "stock screen",
        "doyoutrade-cli stock screen --universe-file u.txt --min-float-mv 1e10",
    ),
    ("data run", "max-float-mv"): (
        "stock screen",
        "doyoutrade-cli stock screen --universe-file u.txt --max-float-mv 1e11",
    ),
}

# Error codes that map to EXIT_NOT_FOUND.
_NOT_FOUND_ERROR_CODES = frozenset(
    {
        "task_not_found",
        "skill_not_found",
        "bash_task_not_found",
        "file_not_found",
        "not_a_file",
        "unknown_source",
    }
)

# Match the fenced JSON block append_json_payload writes onto tool text:
# a blank line, ```json fence, body, closing fence at end-of-string.
_JSON_FENCE_RE = re.compile(r"\n\n```json\n(.*)\n```\s*$", re.DOTALL)

# Match the `[error:<code>] <message>` prefix format_error_text writes
# onto error-result text.
_ERROR_PREFIX_RE = re.compile(r"^\[error:([a-z0-9_]+)\]\s*(.*?)(?:\n|$)", re.DOTALL)

# Optional `Hint: ...` line format_error_text appends.
_HINT_LINE_RE = re.compile(r"^Hint:\s*(.*)$", re.MULTILINE)

# Canonical sentences emitted by ``format_unknown_args`` — used to
# reverse-extract structured fields when the contract-error tool didn't
# also ship a fenced JSON block.
_UNKNOWN_ARGS_LIST_RE = re.compile(r"Unknown arguments:\s*([^.]+)\.")
_ALLOWED_KEYS_RE = re.compile(r"Allowed top-level keys:\s*([^.]+)\.")
_SUGGESTED_RENAME_RE = re.compile(r"Suggested rename:\s*(.+?)\.\s*$", re.MULTILINE)


def extract_unknown_arguments_fields(message: str) -> dict[str, Any]:
    """Reverse-parse ``format_unknown_args`` prose into structured fields.

    Tools that hit ``_enforce_kwargs_contract``'s ``unknown_arguments``
    branch render their result as ``[error:unknown_arguments] <prose>``
    without an attached JSON block. To keep the CLI envelope as
    actionable as the in-process error dict, we extract ``unknown`` /
    ``allowed_top_level`` / ``suggested_path`` back out of that prose.
    Returns an empty dict when nothing matched — callers can merge it
    into ``error_envelope(extra=...)``.
    """

    out: dict[str, Any] = {}
    unknown_match = _UNKNOWN_ARGS_LIST_RE.search(message)
    if unknown_match:
        items = [s.strip() for s in unknown_match.group(1).split(",")]
        out["unknown"] = [item for item in items if item]
    allowed_match = _ALLOWED_KEYS_RE.search(message)
    if allowed_match:
        items = {s.strip() for s in allowed_match.group(1).split(",")}
        out["allowed_top_level"] = sorted(item for item in items if item)
    rename_match = _SUGGESTED_RENAME_RE.search(message)
    if rename_match:
        mapping: dict[str, str] = {}
        for pair in rename_match.group(1).split(";"):
            pair = pair.strip()
            if " -> " in pair:
                src, dest = pair.split(" -> ", 1)
                mapping[src.strip()] = dest.strip()
        if mapping:
            out["suggested_path"] = mapping
    return out


@dataclass(frozen=True)
class Meta:
    """Metadata propagated from the agent's session into the envelope.

    Populated from environment variables set by ``execute_bash`` when the
    CLI runs as a subprocess of the assistant. Empty when the CLI runs
    standalone from a developer's terminal.
    """

    agent_id: str | None = None
    session_id: str | None = None
    debug_session_id: str | None = None
    run_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.agent_id:
            out["agent_id"] = self.agent_id
        if self.session_id:
            out["session_id"] = self.session_id
        if self.debug_session_id:
            out["debug_session_id"] = self.debug_session_id
        if self.run_id:
            out["run_id"] = self.run_id
        out.update(self.extra)
        return out


def parse_tool_result(text: str, *, is_error: bool) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
    """Split a tool's ``ToolResult.text`` into structured pieces.

    Returns ``(data_block, summary, error_info)``:

    * ``data_block``: the parsed JSON object from the trailing fenced
      json block in ``text``, or ``None`` when no fence is present.
    * ``summary``: the prose head with the JSON block stripped off.
    * ``error_info``: ``{"error_code": ..., "message": ..., "hint": ...}``
      when the text starts with ``[error:<code>]`` prefix, else ``None``.

    The caller assembles the envelope from these pieces — the parser is
    deliberately format-aware (it knows about ``format_error_text`` and
    ``append_json_payload``) so the CLI envelope stays faithful to the
    same contract the tools already publish to skill docs.
    """

    raw = text or ""
    data_block: dict[str, Any] | None = None
    summary = raw

    fence_match = _JSON_FENCE_RE.search(raw)
    if fence_match is not None:
        body = fence_match.group(1)
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            data_block = parsed
            summary = raw[: fence_match.start()].rstrip()

    error_info: dict[str, Any] | None = None
    if is_error:
        prefix_match = _ERROR_PREFIX_RE.match(summary)
        if prefix_match is not None:
            code = prefix_match.group(1)
            message = prefix_match.group(2).strip()
            hint_match = _HINT_LINE_RE.search(summary)
            hint = hint_match.group(1).strip() if hint_match else None
            error_info = {"error_code": code, "message": message}
            if hint:
                error_info["hint"] = hint
        else:
            # Tool reported is_error but didn't use format_error_text. Keep
            # the raw summary as the message so the agent still sees it.
            error_info = {
                "error_code": "tool_error",
                "message": summary.strip() or "tool reported error without details",
            }

    return data_block, summary.strip(), error_info


def success_envelope(
    data: dict[str, Any] | None,
    summary: str,
    *,
    meta: Meta,
    notice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a success envelope. ``data`` may be ``None`` for confirmation-style tools."""

    payload: dict[str, Any] = {"ok": True}
    body = dict(data) if data else {}
    if summary and "_summary" not in body:
        body["_summary"] = summary
    if body:
        payload["data"] = body
    meta_dict = meta.to_dict()
    if meta_dict:
        payload["meta"] = meta_dict
    if notice:
        payload["_notice"] = notice
    return payload


def error_envelope(
    *,
    error_code: str,
    message: str,
    error_type: str | None = None,
    hint: str | None = None,
    repair_hints: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    meta: Meta,
    notice: dict[str, Any] | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {
        "error_code": error_code,
        "message": message,
    }
    if error_type:
        err["error_type"] = error_type
    if hint:
        err["hint"] = hint
    if repair_hints:
        err["repair_hints"] = repair_hints
    if extra:
        for k, v in extra.items():
            if k not in err:
                err[k] = v
    payload: dict[str, Any] = {"ok": False, "error": err}
    meta_dict = meta.to_dict()
    if meta_dict:
        payload["meta"] = meta_dict
    if notice:
        payload["_notice"] = notice
    return payload


def exit_code_for_error(error_code: str) -> int:
    """Map a tool / CLI error_code to the canonical exit code."""

    if error_code in _NOT_FOUND_ERROR_CODES or error_code.endswith("_not_found") or error_code == "not_found":
        return EXIT_NOT_FOUND
    if error_code in _VALIDATION_ERROR_CODES or error_code.startswith("invalid_") or error_code.startswith("missing_"):
        return EXIT_VALIDATION
    return EXIT_FAILURE


def emit_envelope(envelope: dict[str, Any], *, stream: Any = None) -> None:
    """Write the envelope to stdout (or ``stream``) followed by a newline.

    Uses ``ensure_ascii=False`` so Chinese text round-trips cleanly through
    the agent's terminal; ``default=str`` so Decimal / datetime values from
    the platform service don't crash json encoding.
    """

    out = stream or sys.stdout
    out.write(json.dumps(envelope, ensure_ascii=False, default=str))
    out.write("\n")
    out.flush()


__all__ = [
    "CLI_COMMAND_ALIASES",
    "CLI_OPTION_ALIASES",
    "CLI_POSITIONAL_ARG_HINTS",
    "EXIT_OK",
    "EXIT_FAILURE",
    "EXIT_VALIDATION",
    "EXIT_NOT_FOUND",
    "EXIT_INTERNAL",
    "Meta",
    "emit_envelope",
    "error_envelope",
    "exit_code_for_error",
    "extract_unknown_arguments_fields",
    "parse_tool_result",
    "success_envelope",
]
