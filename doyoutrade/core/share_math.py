"""Whole-share math: avoid float dust in ``notional / price`` and share counts.

- :func:`max_whole_shares_affordable` — buy notional → shares; partial-sell cap by *T*.
- :func:`floor_whole_share_count` — float *qty* / LLM amount → integer shares (use everywhere
  we previously used ``math.floor(qty)`` on a value that should be whole shares).
- :func:`floor_partial_sell_shares` — ``min(qty, target_notional/price)`` when *T* binds.
- :func:`floor_to_lot` — align a share count down to the exchange lot (A股 100 股一手).
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, Decimal

from doyoutrade.money.decimal_helpers import decimal_from_number

_WHOLE_SHARE_EPS = 1e-9


def floor_whole_share_count(qty: float) -> int:
    """Floor *qty* to a whole number of shares (A-share / integer lots).

    Uses a small epsilon so values like ``3355.9999999995`` map to ``3356`` when they
    represent an integer count from positions, LLM output, or ``float`` serialization.
    """
    if not math.isfinite(qty):
        return 0
    if qty <= 0:
        return 0
    return max(0, int(math.floor(float(qty) + _WHOLE_SHARE_EPS)))


def max_whole_shares_affordable(cap: float, price: float) -> int:
    """Largest integer share count *n* such that *n × price ≤ cap* (same semantics as ``floor(cap/price)`` but stable).

    Used for: buy notional → shares, and sell sizing when *target_notional* caps shares.
    """
    if price <= 0 or cap <= 0 or not math.isfinite(cap) or not math.isfinite(price):
        return 0
    try:
        c = decimal_from_number(cap)
        p = decimal_from_number(price)
    except Exception:
        return max(0, int(math.floor(float(cap) / float(price))))
    if p <= 0:
        return 0
    return int((c / p).to_integral_value(rounding=ROUND_DOWN))


def floor_partial_sell_shares(qty: float, target_notional: float, price: float) -> int:
    """``floor(min(qty, target_notional/price))`` for sell path when *T* binds below sellable notional."""
    if price <= 0:
        return 0
    q = floor_whole_share_count(qty)
    n = max_whole_shares_affordable(target_notional, price)
    return min(q, n)


def floor_to_lot(shares: int, lot_size: int) -> int:
    """Align *shares* down to a multiple of *lot_size* (A-share 100-share lots).

    ``lot_size <= 1`` returns *shares* unchanged (whole-share trading).
    A positive share count that floors to 0 means "below one lot" — callers
    must surface that as a visible skip rather than emitting an odd-lot buy.
    """
    if shares <= 0:
        return 0
    if lot_size <= 1:
        return shares
    return (shares // lot_size) * lot_size


def floor_fraction_shares(qty: int, fraction: float) -> int:
    """``floor(qty * fraction)`` whole shares for a partial exit.

    ``fraction`` is the portion of the held position to sell, in ``(0, 1]``
    (validated upstream at ``Signal.sell``). ``fraction == 1.0`` returns
    ``qty`` unchanged so full exits are byte-identical to the pre-partial-exit
    path. A small fraction on a tiny position can floor to 0 — callers must
    surface that as a visible skip rather than emitting a zero-share order.
    """
    if qty <= 0 or not (fraction > 0.0):
        return 0
    if fraction >= 1.0:
        return floor_whole_share_count(float(qty))
    return floor_whole_share_count(float(qty) * float(fraction))
