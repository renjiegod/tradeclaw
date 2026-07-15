"""qmt-proxy-backed fundamentals provider (float market cap only).

QMT exposes a stock's float / total share volume via the instrument-detail
endpoint but no valuation ratios, so this provider derives
``float_mv = FloatVolume × latest_price`` (and ``total_mv`` likewise) and
leaves ``pe`` / ``pb`` as ``None``. Use akshare when PE/PB are needed; the
``auto`` chain prefers akshare for exactly this reason.

There is no whole-market snapshot, so ``get_fundamentals_batch`` loops the
requested symbols (one instrument-detail + one quote each). A per-symbol
failure is logged and skipped (the symbol is simply absent from the map)
rather than aborting the whole batch — matching the screener's per-symbol
skip discipline.
"""

from __future__ import annotations

import asyncio
import logging

from doyoutrade.core.models import Fundamentals
from doyoutrade.data.constants import DEFAULT_BAR_ADJUST
from doyoutrade.data.protocols import PROVIDER_NAME_QMT, ProviderCapabilities

logger = logging.getLogger(__name__)


class QmtFundamentalsProvider:
    """Float-market-cap source backed by qmt-proxy instrument detail + quotes."""

    capabilities = ProviderCapabilities(
        name=PROVIDER_NAME_QMT,
        supported_intervals=frozenset(),
        default_adjust=DEFAULT_BAR_ADJUST,
        requires_auth=True,
        is_realtime_capable=True,
        max_history_years=None,
    )

    def __init__(self, client):
        self.client = client

    async def get_fundamentals_batch(
        self, symbols: list[str], *, asof: str | None = None
    ) -> dict[str, Fundamentals]:
        out: dict[str, Fundamentals] = {}
        for symbol in symbols:
            f = await self.get_fundamentals(symbol, asof=asof)
            if f is not None:
                out[symbol] = f
        _emit_event(
            "data_provider.fetch_fundamentals",
            {"requested": len(symbols), "matched": len(out)},
        )
        return out

    async def get_fundamentals(
        self, symbol: str, *, asof: str | None = None
    ) -> Fundamentals | None:
        try:
            info = await self.client.fetch_instrument_info(symbol)
            quotes = await self.client.fetch_latest_quotes([symbol])
        except Exception as exc:  # noqa: BLE001 — per-symbol skip, logged
            logger.warning(
                "qmt fundamentals lookup failed for %s: %s: %s",
                symbol, type(exc).__name__, exc,
            )
            return None

        price = float(quotes[0]["price"]) if quotes else None
        float_vol = info.get("FloatVolume")
        total_vol = info.get("TotalVolume")
        float_mv = (
            float(float_vol) * price
            if (float_vol is not None and price is not None)
            else None
        )
        total_mv = (
            float(total_vol) * price
            if (total_vol is not None and price is not None)
            else None
        )
        return Fundamentals(
            code=symbol,
            float_mv=float_mv,
            total_mv=total_mv,
            pe=None,
            pb=None,
            price=price,
            provider=PROVIDER_NAME_QMT,
        )


def _emit_event(event_name: str, payload: dict) -> None:
    payload = {"provider": PROVIDER_NAME_QMT, **payload}
    try:
        from doyoutrade.debug import emit_debug_event

        loop = asyncio.get_running_loop()
        loop.create_task(emit_debug_event(event_name, payload))
    except RuntimeError:
        pass


__all__ = ["QmtFundamentalsProvider"]
