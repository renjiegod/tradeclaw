"""Canonical symbol strings shared with akshare and QMT listing paths."""

from __future__ import annotations

from typing import Any

from doyoutrade.data.instrument_universe.akshare_a import normalize_ak_a_symbol


def canonical_symbol_from_doyoutrade_or_akshare(code_raw: Any) -> str:
    """Normalize a 6-digit or suffixed A-share code to Doyoutrade form (e.g. ``600000.SH``)."""
    return normalize_ak_a_symbol(code_raw)


def canonical_symbol_from_qmt_stock_code(stock_code: str) -> str:
    """Map QMT ``stock_code`` to the same canonical form as akshare.

    Sector lists may attach a **wrong** exchange suffix (e.g. both ``000036.SH`` and
    ``000036.SZ``). For 6-digit A-share bases we **ignore** the suffix and re-derive
    it with :func:`normalize_ak_a_symbol` so one underlying code maps to **one** row.
    Non–6-digit or non–A-share forms (e.g. ``00003.HK``) keep the original string.
    """
    s = str(stock_code).strip()
    if not s:
        return s
    upper = s.upper()
    if "." in upper:
        parts = upper.rsplit(".", 1)
        if len(parts) == 2 and len(parts[0]) == 6 and parts[0].isdigit():
            return normalize_ak_a_symbol(parts[0])
        return upper
    return normalize_ak_a_symbol(s)
