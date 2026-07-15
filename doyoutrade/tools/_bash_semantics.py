"""Per-command exit-code semantics for ``execute_bash``.

Many shell commands use non-zero exit codes for ordinary outcomes — grep
returns 1 when there are no matches, diff returns 1 when files differ,
test returns 1 when the condition is false. Treating those as failures
makes the assistant retry valid no-op runs. This module mirrors what
ClaudeCode's ``commandSemantics.ts`` does so ``ExecuteBashTool`` can
distinguish "ran but said no" from "actually broken".
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class BashSemantic:
    """Outcome of interpreting a command's exit code.

    ``is_error`` is the value ``ToolResult.is_error`` should take. The
    optional ``interpretation`` is a short human-readable label (e.g.
    "No matches found") routed to the debug event payload, **not** to
    the model text — the model already sees the raw stdout/stderr and
    can act on the exit code if surfaced via ``Exit code N``.
    """

    is_error: bool
    interpretation: str | None = None


_GREP_LIKE = frozenset({"grep", "rg", "egrep", "fgrep"})
_FIND_LIKE = frozenset({"find"})
_DIFF_LIKE = frozenset({"diff", "cmp"})
_TEST_LIKE = frozenset({"test", "["})

_SILENT_COMMANDS = frozenset(
    {
        "mv",
        "cp",
        "rm",
        "mkdir",
        "rmdir",
        "chmod",
        "chown",
        "chgrp",
        "touch",
        "ln",
        "cd",
        "export",
        "unset",
        "wait",
    }
)


def _last_segment_base(command: str) -> str:
    """Return the base command of the last segment in a compound command.

    For ``cd repo && grep foo``, the *final* segment determines the exit
    code surfaced by the shell, so its base name (``grep``) is what
    drives the semantic lookup. We do not do full shell parsing — that
    is the policy engine's job; here we just split on the common
    operators (``&&`` / ``||`` / ``;`` / ``|``) and take the right-most
    chunk. Pipelines are debatable (the exit code is the last command
    by default unless ``pipefail`` is set), but we follow the same
    "last command wins" heuristic ClaudeCode uses.
    """

    tail = command
    for sep in ("&&", "||", ";", "|"):
        if sep in tail:
            tail = tail.rsplit(sep, 1)[-1]
    tail = tail.strip()
    if not tail:
        return ""
    try:
        parts = shlex.split(tail, posix=True)
    except ValueError:
        parts = tail.split()
    return parts[0] if parts else ""


def interpret_command_result(
    command: str,
    exit_code: int | None,
    *,
    timed_out: bool,
) -> BashSemantic:
    """Map ``(command, exit_code, timed_out)`` to a semantic outcome.

    Behaviour:
    - ``timed_out`` → always an error (no interpretation).
    - ``exit_code is None`` → treated as error (process state unknown).
    - ``exit_code == 0`` → success.
    - Otherwise: dispatch on the base command of the last segment to
      decide whether the non-zero code is an error or just a signal.
    """

    if timed_out:
        return BashSemantic(is_error=True, interpretation=None)
    if exit_code is None:
        return BashSemantic(is_error=True, interpretation=None)
    if exit_code == 0:
        return BashSemantic(is_error=False, interpretation=None)

    base = _last_segment_base(command)
    if base in _GREP_LIKE:
        if exit_code == 1:
            return BashSemantic(is_error=False, interpretation="No matches found")
        return BashSemantic(is_error=exit_code >= 2, interpretation=None)
    if base in _FIND_LIKE:
        if exit_code == 1:
            return BashSemantic(
                is_error=False,
                interpretation="Some directories were inaccessible",
            )
        return BashSemantic(is_error=exit_code >= 2, interpretation=None)
    if base in _DIFF_LIKE:
        if exit_code == 1:
            return BashSemantic(is_error=False, interpretation="Files differ")
        return BashSemantic(is_error=exit_code >= 2, interpretation=None)
    if base in _TEST_LIKE:
        if exit_code == 1:
            return BashSemantic(is_error=False, interpretation="Condition is false")
        return BashSemantic(is_error=exit_code >= 2, interpretation=None)

    return BashSemantic(is_error=True, interpretation=None)


def is_silent_command(command: str) -> bool:
    """True when the command is expected to print nothing on success.

    Used so a successful ``mkdir foo`` doesn't render as an empty model
    response — the caller appends a ``(no output)`` marker instead.
    """

    base = _last_segment_base(command)
    return base in _SILENT_COMMANDS


__all__ = (
    "BashSemantic",
    "interpret_command_result",
    "is_silent_command",
)
