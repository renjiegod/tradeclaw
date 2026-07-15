"""Shared instrument search matching (name / code / pinyin / initials)."""

from __future__ import annotations

import functools

from pypinyin import Style, lazy_pinyin


@functools.lru_cache(maxsize=16_384)
def display_name_search_keys(display_name: str) -> tuple[str, str]:
    """Return ``(pinyin_concat_lower, initials_upper)`` for a Chinese display name."""
    name = (display_name or "").strip()
    if not name:
        return "", ""
    syllables = lazy_pinyin(name, style=Style.NORMAL)
    full = "".join(syllables).lower()
    initials = "".join(s[0] for s in syllables if s).upper()
    return full, initials


def is_pinyin_style_query(q: str) -> bool:
    """True when *q* is non-empty ASCII letters only (pinyin / initials input)."""
    stripped = (q or "").strip()
    return bool(stripped) and stripped.isascii() and stripped.isalpha()


def matches_instrument_query(
    q: str,
    *,
    symbol: str,
    display_name: str | None,
) -> bool:
    """Match *q* against symbol, Chinese name substring, pinyin, or initials."""
    qn = (q or "").strip()
    if not qn:
        return False

    ql = qn.lower()
    sym = (symbol or "").strip()
    sym_lower = sym.lower()
    name = (display_name or "").strip()
    name_lower = name.lower()
    base = sym.split(".", 1)[0].lower() if sym else ""

    if sym_lower.startswith(ql) or ql in sym_lower:
        return True
    if name and ql in name_lower:
        return True
    if base and (base.startswith(ql) or ql in base):
        return True

    if not is_pinyin_style_query(qn) or not name:
        return False

    full, initials = display_name_search_keys(name)
    qu = qn.upper()
    if full and ql in full:
        return True
    if initials and qu in initials:
        return True
    return False
