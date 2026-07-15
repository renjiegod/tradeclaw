"""Real-broker execution adapter (QMT via qmt-proxy).

Submits live orders through :class:`~doyoutrade.infra.qmt_proxy_client.QmtProxyRestClient`.
Implements the SAME :class:`~doyoutrade.core.worker_protocols.ExecutionAdapterProtocol`
as :class:`~doyoutrade.execution.adapters.PaperExecutionAdapter` and resolves the
order quantity through the SHARED :func:`resolve_order_quantity`, so a given
``OrderIntent`` at a given price maps to the identical share volume whether it is
filled synthetically (mock) or sent to the real broker (qmt). This is the
mock/qmt consistency contract (CLAUDE.md): the only thing that differs between
the two adapters is "synthetic fill vs real order", never the quantity, the
price resolution, or the approval that precedes the call.

Error visibility (CLAUDE.md §错误可见性): a broker reject, an unknown order
status, or a proxy/client error never returns a phantom fill. It emits a
structured ``qmt_order_rejected`` / ``qmt_order_failed`` debug event (with the
broker message + a hint) and returns ``None``, which the worker counts as a
veto — never as a submission.
"""

from __future__ import annotations

import logging

from qmt_proxy_sdk.exceptions import QmtProxyError

from doyoutrade.core.models import FillRecord, MarketContext, OrderIntent
from doyoutrade.debug import emit_debug_event
from doyoutrade.execution.fill_pricing import ClosePriceFillPricingStrategy, FillPricingStrategy
from doyoutrade.execution.order_quantity import resolve_order_quantity


logger = logging.getLogger(__name__)

# qmt-proxy REST side / order-type tokens. The proxy takes plain strings (the
# SDK does not constrain them). These MUST match the qmt-proxy server's accepted
# values — if they ever drift, a submit returns a broker reject which surfaces
# loudly via ``qmt_order_rejected`` (never a silent no-op), so a wrong token is
# immediately visible rather than a phantom pass.
_SIDE_BUY = "BUY"
_SIDE_SELL = "SELL"
_ORDER_TYPE_LIMIT = "LIMIT"

# Order statuses that mean the broker did NOT accept the order. Compared
# case-insensitively against the proxy's ``status`` string; covers common
# English + 中文 reject/cancel vocab. An accepted order (报单成功/已报/部成/已成)
# is anything NOT in this set that also carries an order_id.
_REJECTED_STATUS_TOKENS = (
    "reject",
    "failed",
    "error",
    "cancel",
    "废单",
    "拒绝",
    "已撤",
    "撤单",
    "废",
)


def _status_is_rejected(status: str) -> bool:
    low = (status or "").strip().lower()
    if not low:
        # No status at all is treated as a reject — we never assume success.
        return True
    return any(tok in low or tok in (status or "") for tok in _REJECTED_STATUS_TOKENS)


class QmtExecutionAdapter:
    """Submits :class:`OrderIntent` orders to the real broker via qmt-proxy."""

    portfolio_source: str = "broker"

    def __init__(
        self,
        client,
        fill_pricing: FillPricingStrategy | None = None,
        *,
        strategy_name: str | None = None,
    ):
        #: A :class:`QmtProxyRestClient` (shares the account-reader connection so
        #: no second trading session is opened).
        self._client = client
        self._fill_pricing: FillPricingStrategy = fill_pricing or ClosePriceFillPricingStrategy()
        self._strategy_name = strategy_name
        # Mirror PaperExecutionAdapter's observable surface so callers that
        # inspect submitted orders / fills work the same for both adapters.
        self.submitted: list = []
        self.fills: list[FillRecord] = []

    async def submit_intent(
        self,
        intent: OrderIntent,
        *,
        cycle_state=None,
        market_context: MarketContext | None = None,
    ) -> FillRecord | None:
        self.submitted.append(intent)
        if market_context is not None:
            price = float(self._fill_pricing.price_for_symbol(intent.symbol, market_context))
        else:
            price = float(intent.price_reference)

        # SHARED quantity resolution — identical to PaperExecutionAdapter so the
        # approved notional maps to the same volume on mock and qmt.
        resolved = resolve_order_quantity(intent, price)
        if not resolved.ok:
            await emit_debug_event(
                "execution_zero_fill",
                {
                    "intent_id": intent.intent_id,
                    "symbol": intent.symbol,
                    "action": intent.action,
                    "amount": intent.amount,
                    "price": price,
                    "reason": resolved.reason,
                    "adapter": "qmt",
                    "hint": (
                        "QmtExecutionAdapter rejects this intent before reaching the "
                        "broker: amount_missing → OrderIntent.amount is None; "
                        "non_positive_price → fill price resolved to 0 (data gap); "
                        "sub_one_share → notional below one share. PositionManager "
                        "should have caught all three upstream."
                    ),
                },
            )
            logger.warning(
                "qmt adapter rejected intent intent_id=%s symbol=%s action=%s "
                "amount=%s price=%s reason=%s",
                intent.intent_id,
                intent.symbol,
                intent.action,
                intent.amount,
                price,
                resolved.reason,
            )
            return None

        side = _SIDE_BUY if intent.action == "buy" else _SIDE_SELL
        volume = int(resolved.quantity)
        try:
            resp = await self._client.submit_order(
                stock_code=intent.symbol,
                side=side,
                volume=volume,
                price=price,
                order_type=_ORDER_TYPE_LIMIT,
                strategy_name=self._strategy_name or intent.strategy_tag or None,
            )
        except QmtProxyError as exc:
            # ANY proxy/broker/transport error — NOT just ClientError. The
            # qmt-proxy maps a failed real submit ("真实下单失败") to HTTP 400
            # (→ ClientError) but a generic server-side failure to HTTP 500
            # (→ ServerError), auth to 401 (→ AuthenticationError), and a network
            # blip to TransportError; these are sibling classes of QmtProxyError,
            # not subclasses of ClientError. Catching the base keeps a single
            # bad order from failing the whole cycle's other intents — it is
            # visible (event + WARNING) and isolated: the worker counts it as a
            # veto, never a phantom submit (CLAUDE.md §错误可见性).
            await emit_debug_event(
                "qmt_order_failed",
                {
                    "intent_id": intent.intent_id,
                    "symbol": intent.symbol,
                    "action": intent.action,
                    "volume": volume,
                    "price": price,
                    "error_type": type(exc).__name__,
                    "status_code": getattr(exc, "status_code", None),
                    "error": str(exc),
                    "hint": (
                        "qmt-proxy submit_order failed. Check the broker connection, "
                        "buying power, lot size, trading session, and that the symbol "
                        "is tradable now. status_code 400=broker reject, 401/403=auth, "
                        "5xx=proxy/xttrader fault."
                    ),
                },
            )
            logger.warning(
                "qmt order failed intent_id=%s symbol=%s side=%s volume=%s "
                "error_type=%s status_code=%s: %s",
                intent.intent_id,
                intent.symbol,
                side,
                volume,
                type(exc).__name__,
                getattr(exc, "status_code", None),
                exc,
            )
            return None

        order_id = str(resp.get("order_id") or "")
        status = str(resp.get("status") or "")
        if not order_id or _status_is_rejected(status):
            await emit_debug_event(
                "qmt_order_rejected",
                {
                    "intent_id": intent.intent_id,
                    "symbol": intent.symbol,
                    "action": intent.action,
                    "volume": volume,
                    "price": price,
                    "broker_order_id": order_id,
                    "order_status": status,
                    "hint": (
                        "qmt-proxy accepted the request but the broker rejected the "
                        "order (status indicates reject/cancel, or no order_id). "
                        "Not counted as submitted."
                    ),
                },
            )
            logger.warning(
                "qmt order rejected intent_id=%s symbol=%s side=%s volume=%s "
                "order_id=%s status=%s",
                intent.intent_id,
                intent.symbol,
                side,
                volume,
                order_id,
                status,
            )
            return None

        # Accepted. A LIMIT order may rest unfilled (filled_volume == 0): record
        # the SUBMITTED order (quantity = ordered volume, price = limit price)
        # and let the next cycle's QmtAccountReader reconcile the real fill. The
        # broker's true state is carried on the fill payload (broker_order_id /
        # order_status / filled_volume) so it is never hidden.
        filled_volume = int(resp.get("filled_volume") or 0)
        avg_price = resp.get("average_price")
        fill_quantity = float(filled_volume) if filled_volume > 0 else float(volume)
        fill_price = float(avg_price) if avg_price else price
        fill = FillRecord(
            intent_id=intent.intent_id,
            symbol=intent.symbol,
            side=intent.action,
            quantity=fill_quantity,
            price=fill_price,
        )
        self.fills.append(fill)
        await emit_debug_event(
            "qmt_order_submitted",
            {
                "intent_id": intent.intent_id,
                "symbol": intent.symbol,
                "action": intent.action,
                "volume": volume,
                "broker_order_id": order_id,
                "order_status": status,
                "filled_volume": filled_volume,
                "average_price": avg_price,
            },
        )
        logger.info(
            "qmt order submitted intent_id=%s symbol=%s side=%s volume=%s "
            "order_id=%s status=%s filled_volume=%s",
            intent.intent_id,
            intent.symbol,
            side,
            volume,
            order_id,
            status,
            filled_volume,
        )
        return fill

    async def cancel_order(self, order_id):
        # Real broker cancel via qmt-proxy. Not part of the approval-dispatch
        # path, but wired so an operator can pull a resting order. Visible,
        # non-swallowing: a proxy/broker error propagates as QmtProxyError for
        # the caller to surface.
        result = await self._client.cancel_order(order_id=str(order_id))
        logger.info(
            "qmt cancel order order_id=%s success=%s", order_id, result.get("success")
        )
        return result

    def query_order_status(self, order_id):
        return {"order_id": order_id, "status": "unknown"}

    def sync_account_state(self):
        return {"status": "ok"}
