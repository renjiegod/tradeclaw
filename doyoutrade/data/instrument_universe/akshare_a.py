"""A-share (沪深京) listing via akshare code-name tables.

Uses `stock_info_a_code_name` (+ Beijing `stock_info_bj_name_code`) instead of
`stock_zh_a_spot_em` — fewer HTTP round-trips and less often blocked than the
realtime spot feed.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Tuple

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)

_CACHE: Dict[str, Tuple[float, List[Dict[str, str]]]] = {}
_CACHE_LOCK = asyncio.Lock()
_CACHE_TTL_SEC = 90.0

# Heuristic for tqdm progress-bar lines. tqdm emits e.g.
# "  3%|▎         | 30/1000 [00:01<00:32, 30.00it/s]"  (carriage-return updates)
# We treat lines that look like a percentage bar OR contain `it/s` as tqdm noise.
_TQDM_LINE_RE = re.compile(r"(\d+%\s*\|.*\|.*it/s|^\s*\d+%\s*\|)")


@contextmanager
def _silence_akshare_progress() -> Iterator[None]:
    """Suppress tqdm progress bars emitted by akshare during a single call.

    akshare's request layer writes tqdm progress bars to stdout/stderr, which
    pollute the CLI's captured tool output (eating model tokens and risking
    JSON-envelope corruption). We:

    1. Set ``TQDM_DISABLE=1`` so any tqdm instance created during the call is
       a no-op (tqdm honours this env var).
    2. Redirect ``sys.stdout`` / ``sys.stderr`` to in-memory buffers in case
       a tqdm instance was constructed before the env var was read.
    3. On exit, restore stdout/stderr + the prior ``TQDM_DISABLE`` value, and
       forward any **non-tqdm** stderr lines to ``logger.warning`` so genuine
       akshare warnings stay visible (CLAUDE.md mandate: no silent swallow).
    """
    prev_tqdm = os.environ.get("TQDM_DISABLE")
    os.environ["TQDM_DISABLE"] = "1"
    buf_out, buf_err = io.StringIO(), io.StringIO()
    prev_out, prev_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        yield
    finally:
        sys.stdout, sys.stderr = prev_out, prev_err
        if prev_tqdm is None:
            os.environ.pop("TQDM_DISABLE", None)
        else:
            os.environ["TQDM_DISABLE"] = prev_tqdm
        captured_err = buf_err.getvalue()
        # tqdm uses \r to overwrite; split on both CR and LF so each progress
        # frame is inspected independently.
        for line in re.split(r"[\r\n]+", captured_err):
            if not line.strip():
                continue
            if _TQDM_LINE_RE.search(line):
                continue
            logger.warning("akshare stderr: %s", line)


def clear_akshare_a_spot_cache() -> None:
    """Drop cached spot rows (for tests or forced refresh)."""
    _CACHE.clear()


def normalize_ak_a_symbol(code_raw: Any) -> str:
    """Map Eastmoney 6-digit code to Doyoutrade-style symbol with exchange suffix."""
    s = str(code_raw).strip()
    if not s:
        return s
    upper = s.upper()
    if "." in upper:
        return upper
    if not s.isdigit():
        return upper
    code = s.zfill(6)
    if len(code) != 6:
        return upper
    p2 = code[:2]
    p1 = code[0]
    if p2 in ("60", "68") or code.startswith("688"):
        return f"{code}.SH"
    # 上交所 ETF：51/56/58xxxx。若不显式识别会掉进末尾兜底被误判成 .SZ。
    if p2 in ("51", "56", "58"):
        return f"{code}.SH"
    if p2 in ("00", "30"):
        return f"{code}.SZ"
    if p1 in ("4", "8", "9") or p2 in ("43", "83", "87", "92"):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _cell_str(cell: Any) -> str:
    if cell is None:
        return ""
    try:
        if pd.isna(cell):
            return ""
    except TypeError:
        pass
    if isinstance(cell, bool):
        return ""
    if isinstance(cell, int):
        return str(cell)
    if isinstance(cell, float):
        if not str(cell).lower() in ("inf", "nan") and float(cell).is_integer():
            return str(int(cell))
    s = str(cell).strip()
    if not s or s.lower() in ("nan", "none"):
        return ""
    if s.replace(".", "", 1).isdigit() and "." in s:
        try:
            return str(int(float(s)))
        except ValueError:
            pass
    return s


def _sync_fetch_etf_rows() -> List[Dict[str, str]]:
    """ETF listings via ``fund_etf_spot_em`` (代码/名称 spot snapshot).

    Kept in its own wrapper so a partial ETF-feed failure is logged without
    losing the stock rows already collected (same tolerance policy as the BJ
    table). Rows are tagged ``instrument_type="etf"`` so downstream catalog
    sync writes them tradable rather than as plain stocks.
    """
    try:
        with _silence_akshare_progress():
            df_etf = ak.fund_etf_spot_em()
    except Exception as exc:
        logger.warning(
            "akshare fund_etf_spot_em failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        return []
    rows: List[Dict[str, str]] = []
    for _, row in df_etf.iterrows():
        code = _cell_str(row.get("代码"))
        name = _cell_str(row.get("名称"))
        if not code or not name:
            continue
        sym = normalize_ak_a_symbol(code)
        rows.append({"symbol": sym, "name": name, "market": "CN", "instrument_type": "etf"})
    return rows


def _sync_fetch_spot_rows() -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    with _silence_akshare_progress():
        df_a = ak.stock_info_a_code_name()
    for _, row in df_a.iterrows():
        code = _cell_str(row.get("code"))
        name = _cell_str(row.get("name"))
        if not code or not name:
            continue
        sym = normalize_ak_a_symbol(code)
        out.append({"symbol": sym, "name": name, "market": "CN", "instrument_type": "stock"})

    # Keep BJ fetch in its own wrapper so a partial failure here is logged
    # without losing the A-share rows we already collected above.
    try:
        with _silence_akshare_progress():
            df_bj = ak.stock_info_bj_name_code()
    except Exception as exc:
        # Pre-existing behaviour: tolerate a missing BJ table (akshare's BJ
        # endpoint goes down more often than the A-share one). Per CLAUDE.md
        # we don't silently swallow — surface the failure mode so operators
        # can see why BJ listings are missing.
        logger.warning(
            "akshare stock_info_bj_name_code failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        df_bj = None
    if df_bj is not None:
        for _, row in df_bj.iterrows():
            code = _cell_str(row.get("证券代码"))
            name = _cell_str(row.get("证券简称"))
            if not code or not name:
                continue
            sym = normalize_ak_a_symbol(code)
            out.append({"symbol": sym, "name": name, "market": "CN", "instrument_type": "stock"})

    # ETF listings (场内基金) so `stock lookup` and catalog sync surface them
    # alongside stocks. A missing/failed ETF feed degrades to zero ETF rows
    # (logged), never drops the stock rows above.
    out.extend(_sync_fetch_etf_rows())

    return out


async def _load_cached_rows() -> List[Dict[str, str]]:
    key = "akshare_a"
    async with _CACHE_LOCK:
        now = time.monotonic()
        hit = _CACHE.get(key)
        if hit is not None:
            ts, rows = hit
            if now - ts < _CACHE_TTL_SEC:
                return rows

    rows = await asyncio.to_thread(_sync_fetch_spot_rows)

    async with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), rows)
    return rows


def filter_akshare_rows(rows: List[Dict[str, str]], q: str, limit: int) -> List[Dict[str, str]]:
    from doyoutrade.data.instrument_catalog.search_match import matches_instrument_query

    qn = (q or "").strip()
    if not qn:
        return []
    matches: List[Dict[str, str]] = []
    for r in rows:
        if matches_instrument_query(qn, symbol=r["symbol"], display_name=r.get("name")):
            matches.append(r)
        if len(matches) >= limit:
            break
    return matches


async def search_akshare_a(*, q: str, limit: int) -> List[Dict[str, str]]:
    q_stripped = (q or "").strip()
    if not q_stripped:
        return []
    rows = await _load_cached_rows()
    return filter_akshare_rows(rows, q_stripped, limit)
