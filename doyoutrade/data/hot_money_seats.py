"""游资席位静态标签库 (hot-money seat tagging) — loader + matcher.

Loads the static, hand-maintained tag library at ``hot_money_seats.yaml``
(a ``游资名 -> [营业部名称关键词, ...]`` map) and tags a 龙虎榜席位's
``交易营业部名称`` with a best-effort 游资名 (hot-money handle) by **substring**
match against those keywords.

The library is a *non-authoritative starter set*: a ``None`` label means only
"no keyword in our list matched", NOT "this seat is not a 游资".

Failure-mode discipline (per CLAUDE.md §错误可见性): a missing / malformed /
wrong-shaped YAML is surfaced **loudly** (``logger.warning`` with the exception
type + message + path) and then degrades to an *empty* library so seat tagging
simply produces ``hot_money=None`` for every seat — it never silently swallows
the failure and never crashes the whole 席位明细 fetch. The load is cached after
the first successful (or loud-failed → empty) attempt so we don't re-read the
file per seat.

``is_institution`` detection is orthogonal to the 游资 tag: a seat whose 类型 /
名称 contains ``机构专用`` is an institutional desk regardless of any 游资 match.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_SEAT_LIBRARY_PATH = Path(__file__).resolve().parent / "hot_money_seats.yaml"

# The marker upstream uses for an institutional (机构专用) trading desk. A seat
# whose 类型 or 名称 contains this is flagged is_institution=True.
_INSTITUTION_MARKER = "机构专用"


@lru_cache(maxsize=1)
def _load_seat_library() -> dict[str, list[str]]:
    """Load and cache the ``游资名 -> [关键词, ...]`` map.

    On any load / parse / shape failure we log **loudly** at WARNING (carrying
    the exception type + message + path) and return an empty map, so seat
    tagging degrades to ``hot_money=None`` rather than crashing the fetch or
    silently masking a broken library. Non-list / non-str values inside an
    otherwise valid file are dropped per-key with a WARNING (never silently).
    """
    try:
        raw_text = _SEAT_LIBRARY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "hot_money_seats library unreadable path=%s %s: %s — "
            "seat tags will be empty",
            _SEAT_LIBRARY_PATH,
            type(exc).__name__,
            exc,
        )
        return {}

    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        logger.warning(
            "hot_money_seats library not valid YAML path=%s %s: %s — "
            "seat tags will be empty",
            _SEAT_LIBRARY_PATH,
            type(exc).__name__,
            exc,
        )
        return {}

    if parsed is None:
        # Empty file (all comments) is a legitimate empty library, not an error.
        logger.info(
            "hot_money_seats library is empty path=%s — no seat tags",
            _SEAT_LIBRARY_PATH,
        )
        return {}

    if not isinstance(parsed, dict):
        logger.warning(
            "hot_money_seats library top-level must be a mapping, got %s "
            "path=%s — seat tags will be empty",
            type(parsed).__name__,
            _SEAT_LIBRARY_PATH,
        )
        return {}

    library: dict[str, list[str]] = {}
    for handle, keywords in parsed.items():
        if not isinstance(keywords, list):
            logger.warning(
                "hot_money_seats entry %r must map to a list of keyword "
                "strings, got %s path=%s — dropping this entry",
                handle,
                type(keywords).__name__,
                _SEAT_LIBRARY_PATH,
            )
            continue
        cleaned = [str(kw).strip() for kw in keywords if str(kw).strip()]
        if cleaned:
            library[str(handle).strip()] = cleaned
    return library


def match_hot_money(seat_name: str) -> Optional[str]:
    """Return the 游资名 whose keyword is a substring of ``seat_name``, else None.

    Substring match keeps the library forgiving as 营业部 names drift (branch
    suffixes, 有限公司/股份有限公司 variations). The first matching handle wins;
    ``None`` means "no starter-set keyword matched" — never a hard assertion
    that the seat is not a 游资.
    """
    if not seat_name:
        return None
    library = _load_seat_library()
    for handle, keywords in library.items():
        for keyword in keywords:
            if keyword and keyword in seat_name:
                return handle
    return None


def is_institution_seat(seat_name: str, seat_type: str = "") -> bool:
    """True when the seat's 类型 or 名称 marks it as 机构专用 (institutional)."""
    return _INSTITUTION_MARKER in (seat_type or "") or _INSTITUTION_MARKER in (
        seat_name or ""
    )


__all__ = ["match_hot_money", "is_institution_seat"]
