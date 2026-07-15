"""Pluggable fill-price resolution for execution adapters.

Default behavior matches **close-of-bar** style pricing: prefer ``close`` (or
``last``) from :attr:`~doyoutrade.domain.models.MarketContext.symbol_to_tick`,
then fall back to :attr:`~doyoutrade.domain.models.MarketContext.symbol_to_price`.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from doyoutrade.core.models import MarketContext


@runtime_checkable
class FillPricingStrategy(Protocol):
    """Resolve execution price for a symbol given the current market snapshot."""

    def price_for_symbol(self, symbol: str, market_context: MarketContext) -> float:
        ...


def close_price_for_symbol(symbol: str, market_context: MarketContext) -> float:
    """Best-effort close / last price for *symbol* from *market_context*."""
    tick = market_context.symbol_to_tick.get(symbol) or {}
    if isinstance(tick, dict):
        for key in ("close", "last", "Close", "Last", "CLOSE", "LAST"):
            raw = tick.get(key)
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    ref = market_context.symbol_to_price.get(symbol)
    if ref is not None:
        try:
            return float(ref)
        except (TypeError, ValueError):
            pass
    return 0.0


def symbol_is_tradeable(symbol: str, market_context: MarketContext) -> bool:
    """Whether *symbol* may accept an order this cycle.

    ``False`` only when the simulated-bar overlay explicitly flagged the symbol
    as halted on the cycle day (``symbol_to_tick[symbol]["tradeable"] is
    False``). A halted symbol still carries a reference close for position MTM,
    but no order can execute during a trading halt — so the overlay sets the
    flag and ``PositionManager`` skips the order with ``reason=symbol_suspended``
    rather than fabricating a fill. Absent the flag (live cycles, or any tick
    that never went through the overlay) the symbol is assumed tradeable, so
    this is backward compatible with non-backtest paths.
    """
    tick = market_context.symbol_to_tick.get(symbol)
    if isinstance(tick, dict) and tick.get("tradeable") is False:
        return False
    return True


class ClosePriceFillPricingStrategy:
    """Concrete :class:`FillPricingStrategy` using :func:`close_price_for_symbol`."""

    def price_for_symbol(self, symbol: str, market_context: MarketContext) -> float:
        return close_price_for_symbol(symbol, market_context)
