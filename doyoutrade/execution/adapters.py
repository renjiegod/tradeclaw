from __future__ import annotations

import logging

from doyoutrade.core.models import FillRecord, MarketContext, OrderIntent
from doyoutrade.debug import emit_debug_event_sync
from doyoutrade.execution.fill_pricing import ClosePriceFillPricingStrategy, FillPricingStrategy
from doyoutrade.execution.order_quantity import resolve_order_quantity


logger = logging.getLogger(__name__)


class PaperExecutionAdapter:
    """Records submitted intents and synthetic fills.

    When ``market_context`` is passed to :meth:`submit_intent`, fill price is
    resolved via ``fill_pricing`` (default: bar **close** / tick last, then last
    price map). Without ``market_context``, uses ``intent.price_reference``.
    """

    def __init__(self, fill_pricing: FillPricingStrategy | None = None, ledger: object | None = None):
        self.submitted: list = []
        self.fills: list[FillRecord] = []
        self._fill_pricing: FillPricingStrategy = fill_pricing or ClosePriceFillPricingStrategy()
        self._ledger = ledger

    async def submit_intent(
        self,
        intent,
        *,
        cycle_state=None,
        market_context: MarketContext | None = None,
    ):
        self.submitted.append(intent)
        if market_context is not None:
            price = float(self._fill_pricing.price_for_symbol(intent.symbol, market_context))
        else:
            price = float(intent.price_reference)
        resolved = resolve_order_quantity(intent, price)
        if not resolved.ok:
            # Non-OK outcomes used to produce a zero-quantity FillRecord that
            # propagated as "submitted" without an actual trade. That is
            # always either an upstream PositionManager bug or a price
            # provisioning gap — surface it instead of pretending the order
            # filled.
            emit_debug_event_sync(
                "execution_zero_fill",
                {
                    "intent_id": intent.intent_id,
                    "symbol": intent.symbol,
                    "action": intent.action,
                    "amount": intent.amount,
                    "price": price,
                    "reason": resolved.reason,
                    "hint": (
                        "PaperExecutionAdapter rejects this intent: amount_missing "
                        "→ OrderIntent.amount is None; non_positive_price → "
                        "fill_pricing.price_for_symbol returned 0 (data gap); "
                        "sub_one_share → notional below one share. PositionManager "
                        "should have caught all three upstream; if you see this "
                        "in production, an OrderIntent was constructed bypassing "
                        "PositionManager."
                    ),
                },
            )
            logger.warning(
                "execution adapter rejected intent intent_id=%s symbol=%s action=%s "
                "amount=%s price=%s reason=%s",
                intent.intent_id,
                intent.symbol,
                intent.action,
                intent.amount,
                price,
                resolved.reason,
            )
            return None
        fill = FillRecord(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.action,
            quantity=float(resolved.quantity),
            price=price,
        )
        self.fills.append(fill)
        apply_fn = getattr(self._ledger, "apply_synthetic_fill", None) if self._ledger is not None else None
        if callable(apply_fn):
            apply_fn(intent, fill)
        return fill

    def cancel_order(self, order_id):
        return {"order_id": order_id, "status": "cancelled"}

    def query_order_status(self, order_id):
        return {"order_id": order_id, "status": "filled"}

    def sync_account_state(self):
        return {"status": "ok"}


class SimulatedBrokerAdapter(PaperExecutionAdapter):
    """Same as :class:`PaperExecutionAdapter`; subclass name marks backtest / simulation stacks."""

    def __init__(self, fill_pricing: FillPricingStrategy | None = None, ledger: object | None = None):
        super().__init__(fill_pricing=fill_pricing, ledger=ledger)
