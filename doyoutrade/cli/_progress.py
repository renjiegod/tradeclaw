"""Stderr progress bar for long-running CLI commands (``backtest run``).

stdout is the single-line JSON-envelope contract that agents parse, so the
progress bar renders to **stderr** only, and only when stderr is an
interactive TTY (or the caller forces it on with ``--progress``).
Automated / agent callers (stderr not a TTY) see no progress output and
the stdout envelope is byte-for-byte identical to the non-progress path.

The renderer is intentionally dependency-free (no rich / tqdm): a single
carriage-return line that overwrites itself, terminated by one newline
when the run reaches a terminal status. The pure ``render_progress_line``
formatter is split out from the I/O so it can be unit-tested without a
TTY.
"""

from __future__ import annotations

import sys
from typing import TextIO

_BAR_WIDTH = 28
_FILLED = "█"  # █
_EMPTY = "░"  # ░


def should_show_progress(flag: bool | None, *, stream: TextIO | None = None) -> bool:
    """Decide whether to render a progress bar.

    ``flag`` is the tri-state ``--progress/--no-progress`` option:
    ``True`` forces on, ``False`` forces off, ``None`` auto-detects an
    interactive stderr (the default, matching git / pip / docker).
    """

    if flag is not None:
        return bool(flag)
    target: TextIO = stream if stream is not None else sys.stderr
    try:
        return bool(target.isatty())
    except Exception:
        # A stream without isatty (e.g. a captured buffer) is treated as
        # non-interactive so we never corrupt a piped consumer.
        return False


def render_progress_line(
    bars_completed: int,
    bars_total: int,
    status: str | None,
    *,
    width: int = _BAR_WIDTH,
) -> str:
    """Format one progress line (no leading ``\\r`` / trailing newline).

    ``bars_total <= 0`` means the run is still preparing its trading-day
    list, so we render an indeterminate bar rather than dividing by zero.
    ``bars_completed`` is clamped into ``[0, bars_total]`` so a stale read
    can never overflow the bar.
    """

    status_label = (status or "").strip() or "running"
    if bars_total <= 0:
        return f"[{_EMPTY * width}]  --% (preparing) {status_label}"
    completed = max(0, min(int(bars_completed), int(bars_total)))
    frac = completed / bars_total
    filled = max(0, min(int(round(frac * width)), width))
    bar = _FILLED * filled + _EMPTY * (width - filled)
    pct = int(frac * 100)
    return f"[{bar}] {pct:3d}% {completed}/{bars_total} bars {status_label}"


class ProgressReporter:
    """Carriage-return progress writer bound to a stream (default stderr).

    Stays silent when ``enabled`` is ``False`` so callers can construct it
    unconditionally and let the flag decide. ``close`` only emits the
    terminating newline if at least one line was actually drawn, so a run
    that finishes before the first poll leaves no stray blank line.
    """

    def __init__(self, *, enabled: bool, stream: TextIO | None = None) -> None:
        self.enabled = enabled
        self._stream = stream if stream is not None else sys.stderr
        self._last = ""
        self._drawn = False

    def update(self, bars_completed: int, bars_total: int, status: str | None) -> None:
        if not self.enabled:
            return
        line = render_progress_line(bars_completed, bars_total, status)
        if line == self._last:
            return
        # Pad with trailing spaces so a shorter line fully overwrites a
        # longer previous render before the carriage return wraps back.
        pad = max(0, len(self._last) - len(line))
        self._stream.write("\r" + line + (" " * pad))
        self._stream.flush()
        self._last = line
        self._drawn = True

    def close(self) -> None:
        if not self.enabled or not self._drawn:
            return
        # Terminate the carriage-return line so the stdout envelope / shell
        # prompt starts on a fresh line.
        self._stream.write("\n")
        self._stream.flush()
        self._drawn = False


__all__ = ["ProgressReporter", "render_progress_line", "should_show_progress"]
