"""Shared order-quantity resolution for **all** execution adapters.

The single source of truth for "given an :class:`OrderIntent` and a resolved
fill price, how many whole shares does this order touch?". Both
:class:`~doyoutrade.execution.adapters.PaperExecutionAdapter` (mock / paper) and
:class:`~doyoutrade.execution.qmt_adapter.QmtExecutionAdapter` (real broker) call
this, so the same intent + same price always maps to the same volume regardless
of which account/adapter is behind it.

This is the consistency contract that prevents the "mock works but qmt diverges"
class of bug (CLAUDE.md §错误可见性): quantity is decided here, not per-adapter.
``amount`` semantics follow :class:`OrderIntent` — buy = quote notional, sell =
share count.
"""

from __future__ import annotations

from dataclasses import dataclass

from doyoutrade.core.models import OrderIntent
from doyoutrade.core.share_math import floor_whole_share_count, max_whole_shares_affordable


#: Non-OK reasons. Adapters surface these via ``execution_zero_fill`` and return
#: ``None`` rather than pretending the order filled — these are always either an
#: upstream PositionManager bug or a price-provisioning gap.
QTY_OK = "ok"
QTY_AMOUNT_MISSING = "amount_missing"
QTY_NON_POSITIVE_PRICE = "non_positive_price"
QTY_SUB_ONE_SHARE = "sub_one_share"


@dataclass(frozen=True)
class ResolvedOrderQty:
    quantity: float
    #: One of the ``QTY_*`` constants.
    reason: str

    @property
    def ok(self) -> bool:
        return self.reason == QTY_OK


def resolve_order_quantity(intent: OrderIntent, price: float) -> ResolvedOrderQty:
    """Resolve the whole-share quantity for *intent* at *price*.

    - ``amount is None`` → ``amount_missing``
    - ``price <= 0`` → ``non_positive_price`` (a data gap)
    - buy: largest whole share count affordable at *price* for ``amount`` notional
    - sell: ``floor`` of ``amount`` (already a share count for sells)
    - result ``<= 0`` → ``sub_one_share``
    """
    if intent.amount is None:
        return ResolvedOrderQty(quantity=0.0, reason=QTY_AMOUNT_MISSING)
    if price <= 0:
        return ResolvedOrderQty(quantity=0.0, reason=QTY_NON_POSITIVE_PRICE)
    if intent.action == "buy":
        # Whole shares (e.g. A-share); stable max shares affordable (avoids
        # float floor(notional/price) off-by-one).
        qty = float(max_whole_shares_affordable(float(intent.amount), price))
    else:
        qty = float(floor_whole_share_count(float(intent.amount)))
    if qty <= 0:
        return ResolvedOrderQty(quantity=0.0, reason=QTY_SUB_ONE_SHARE)
    return ResolvedOrderQty(quantity=qty, reason=QTY_OK)
