"""Canonical-symbol classifiers: 沪深北 A 股股票 与 沪深 ETF。

``is_cn_a_share_equity_symbol`` keeps the historical **stock** allowlist (excludes
ETF/基金/债券/可转债). ``is_cn_a_share_etf_symbol`` recognises exchange-traded
funds so they can enter the catalog / K-line sync / universe as tradable
instruments alongside stocks (they were previously filtered out entirely).
"""

from __future__ import annotations


def _split_base_suffix(symbol: str) -> tuple[str, str] | None:
    """Return ``(6-digit base, exchange suffix)`` for a canonical A-share symbol.

    Returns ``None`` for anything that is not a ``NNNNNN.<SH|SZ|BJ>`` form, so
    both classifiers reject malformed / non-A-share symbols identically.
    """
    s = (symbol or "").strip().upper()
    if "." not in s:
        return None
    base, suf = s.rsplit(".", 1)
    if suf not in ("SH", "SZ", "BJ"):
        return None
    if len(base) != 6 or not base.isdigit():
        return None
    return base, suf


def is_cn_a_share_etf_symbol(symbol: str) -> bool:
    """Return True if ``symbol`` looks like a mainland A-share **ETF**.

    Prefix allowlist aligned with the on-exchange ETF ranges:

    * 上交所 (.SH): ``51xxxx`` / ``56xxxx`` / ``58xxxx`` (含科创板 ``588`` 系).
    * 深交所 (.SZ): ``15xxxx`` (``159xxx`` 系).

    Deliberately narrow — LOF (``16``/``50``xxxx SH, ``16``xxxx SZ), 封闭式基金
    (``18``xxxx SZ) 与债券 ETF 之外的场内基金不在此列，避免把非 ETF 合约当成
    ETF 灌进可交易 universe。ETF 卖出免征印花税、最小交易单位 100 份，与股票一致。
    """
    parsed = _split_base_suffix(symbol)
    if parsed is None:
        return False
    base, suf = parsed
    p2 = base[:2]
    if suf == "SH":
        return p2 in ("51", "56", "58")
    if suf == "SZ":
        return p2 == "15"
    return False


def is_cn_a_share_index_symbol(symbol: str) -> bool:
    """Return True if ``symbol`` looks like a mainland A-share **index** (指数).

    Prefix allowlist aligned with the standard index numbering — deliberately
    narrow, mirroring :func:`is_cn_a_share_etf_symbol`:

    * 上交所 (.SH): ``000xxx`` 系（``000001`` 上证综指、``000300`` 沪深300、
      ``000905`` 中证500、``000016`` 上证50 等）。上证个股是 ``60x``/``68x``/``90x``，
      不占 ``000`` 段，故 ``.SH`` + ``000`` 前缀即指数。
    * 深交所 (.SZ): ``399xxx`` 系（``399001`` 深证成指、``399006`` 创业板指 等）。
      深证个股从不以 ``399`` 开头。

    注意 ``000001.SZ`` 是平安银行（个股），``000001.SH`` 才是上证综指——分类必须
    同时看交易所后缀，不能只看数字。指数不复权，取数走 akshare 的 index_* 端点。
    """
    parsed = _split_base_suffix(symbol)
    if parsed is None:
        return False
    base, suf = parsed
    if suf == "SH":
        return base.startswith("000")
    if suf == "SZ":
        return base.startswith("399")
    return False


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
