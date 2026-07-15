"""Canonical-symbol whitelist: 沪深北 A 股股票（排除 ETF/基金/债券/可转债等非股票合约）。"""

from __future__ import annotations


def is_cn_a_share_equity_symbol(symbol: str) -> bool:
    """Return True if ``symbol`` looks like a mainland A-share **stock** (not fund/bond/ETF).

    Used to filter QMT sector stock lists, which mix ETFs (15/16/18xxxx SZ, 51xxxx SH),
    convertible bonds (11/12xxxx, 123xxx), etc. Those often return HTTP 400 from
    ``/api/v1/data/instrument/{code}``.

    Rules are a **prefix allowlist** aligned with common retail listings; extend if you
    need stricter exchange rules.
    """
    s = (symbol or "").strip().upper()
    if "." not in s:
        return False
    base, suf = s.rsplit(".", 1)
    if suf not in ("SH", "SZ", "BJ"):
        return False
    if len(base) != 6 or not base.isdigit():
        return False
    c = base
    head3 = int(c[:3])

    if suf == "SH":
        # 上证 A 股：600–605 段主板、688/689 科创板（排除 588 等 ETF）
        if c.startswith("588"):
            return False
        if head3 == 688 or head3 == 689:
            return True
        if 600 <= head3 <= 605:
            return True
        return False

    if suf == "SZ":
        # 深证 A 股：000–003 开头三位、创业板 300–302
        if head3 <= 3:
            return True
        if 300 <= head3 <= 302:
            return True
        return False

    if suf == "BJ":
        if c.startswith("920"):
            return True
        p2 = c[:2]
        if p2 in ("43", "83", "87", "88", "92"):
            return True
        return False

    return False
