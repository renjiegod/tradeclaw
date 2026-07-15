"""Opt-in stderr diagnostics for hangs and ordering bugs.

Set environment variable ``DOYOUTRADE_RUNTIME_DIAG=1`` before running the process
(e.g. ``DOYOUTRADE_RUNTIME_DIAG=1 uv run python -m unittest tests.test_shared_approval_runtime -v``)
to print ``RUNTIME_DIAG:`` lines from bootstrap, ``tick_once``, ``mark_finished``, etc.
"""

from __future__ import annotations

import os
import sys


def runtime_diag(message: str) -> None:
    if os.environ.get("DOYOUTRADE_RUNTIME_DIAG") == "1":
        print(f"RUNTIME_DIAG: {message}", file=sys.stderr, flush=True)
