"""Akshare-based fundamentals / valuation provider.

Backed by ``stock_zh_a_spot_em`` — the 东方财富 whole-market spot snapshot.
A single upstream call returns every A-share's 流通市值 / 总市值 / 市盈率-动态
/ 市净率 / 最新价, so screening a universe costs **one** request rather than
N per-symbol round-trips. Values are point-in-time ("now"); the ``asof``
argument is accepted for interface uniformity but the snapshot cannot be
back-dated (fine for "current 流通市值" screening).

Market-cap columns are in 元 (yuan), so 100亿 is ``1e10`` — the same scale
``stock screen --min-float-mv`` expects.

Failure-mode discipline (per CLAUDE.md §错误可见性): a persistent upstream
failure re-raises (the ``data_fundamentals`` tool maps it to
``fundamentals_fetch_failed``); an empty snapshot yields an empty map. The
``data.akshare.fetch_fundamentals`` span + ``data_provider.fetch_fundamentals``
event always fire.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Optional

import akshare as ak

from doyoutrade.core.models import Fundamentals
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.instrument_catalog.normalize import (
    canonical_symbol_from_doyoutrade_or_akshare,
)
from doyoutrade.data.instrumentation import data_span
from doyoutrade.data.protocols import PROVIDER_NAME_AKSHARE, ProviderCapabilities

logger = logging.getLogger(__name__)

# stock_zh_a_spot_em columns (东方财富 whole-market snapshot).
_COL_CODE = "代码"
_COL_PRICE = "最新价"
_COL_FLOAT_MV = "流通市值"
_COL_TOTAL_MV = "总市值"
_COL_PE = "市盈率-动态"
_COL_PB = "市净率"

_MAX_ATTEMPTS = 3


def _opt_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


class AkshareFundamentalsProvider:
    """Valuation source backed by akshare ``stock_zh_a_spot_em``."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_AKSHARE,
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=False,
        is_realtime_capable=False,
        max_history_years=None,
    )

    async def get_fundamentals_batch(
        self, symbols: list[str], *, asof: str | None = None
    ) -> dict[str, Fundamentals]:
        with data_span("akshare", "fetch_fundamentals"):
            full = await asyncio.to_thread(self._sync_snapshot)
        wanted = {canonical_symbol_from_doyoutrade_or_akshare(s) for s in symbols}
        result = {code: f for code, f in full.items() if code in wanted}
        _emit_event(
            "data_provider.fetch_fundamentals",
            {"requested": len(symbols), "matched": len(result)},
        )
        return result

    async def get_fundamentals(
        self, symbol: str, *, asof: str | None = None
    ) -> Fundamentals | None:
        batch = await self.get_fundamentals_batch([symbol], asof=asof)
        return batch.get(canonical_symbol_from_doyoutrade_or_akshare(symbol))

    def _sync_snapshot(self) -> dict[str, Fundamentals]:
        df = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                df = ak.stock_zh_a_spot_em()
                break
            except Exception as exc:  # noqa: BLE001 — re-raised after retries
                logger.warning(
                    "akshare stock_zh_a_spot_em failed (attempt %d/%d): %s: %s",
                    attempt + 1, _MAX_ATTEMPTS, type(exc).__name__, exc,
                )
                if attempt == _MAX_ATTEMPTS - 1:
                    logger.error(
                        "akshare stock_zh_a_spot_em gave up: %s: %s",
                        type(exc).__name__, exc,
                    )
                    raise
                time.sleep(0.8 * (attempt + 1))

        if df is None or df.empty or _COL_CODE not in df.columns:
            logger.info("akshare spot snapshot empty / missing 代码 column")
            return {}

        out: dict[str, Fundamentals] = {}
        for _, row in df.iterrows():
            raw_code = row.get(_COL_CODE)
            if raw_code is None or str(raw_code).strip() == "":
                continue
            code = canonical_symbol_from_doyoutrade_or_akshare(raw_code)
            out[code] = Fundamentals(
                code=code,
                float_mv=_opt_float(row.get(_COL_FLOAT_MV)),
                total_mv=_opt_float(row.get(_COL_TOTAL_MV)),
                pe=_opt_float(row.get(_COL_PE)),
                pb=_opt_float(row.get(_COL_PB)),
                price=_opt_float(row.get(_COL_PRICE)),
                provider=PROVIDER_NAME_AKSHARE,
            )
        return out


def _emit_event(event_name: str, payload: dict) -> None:
    payload = {"provider": PROVIDER_NAME_AKSHARE, **payload}
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        pass


__all__ = ["AkshareFundamentalsProvider"]
