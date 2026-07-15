"""Shared data-layer constants."""

from __future__ import annotations


# Front-adjusted bars are the repo-wide default unless a caller explicitly
# requests another adjust mode.
DEFAULT_BAR_ADJUST = "qfq"


__all__ = ["DEFAULT_BAR_ADJUST"]
