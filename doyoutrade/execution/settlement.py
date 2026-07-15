"""A-share and broker settlement rules (quantity vs sellable, day-boundary unlock)."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Iterable, Literal

from doyoutrade.core.models import PositionSnapshot
from doyoutrade.core.share_math import floor_whole_share_count
from doyoutrade.money.decimal_helpers import decimal_from_number

logger = logging.getLogger(__name__)

SettlementMode = Literal["t0", "t1", "broker"]

CN_A_SHARE_PROFILE = "cn_a_share"
_MARKET_TZ = "Asia/Shanghai"


def settlement_mode(market_profile: str | None, portfolio_source: str) -> SettlementMode:
    """Resolve how sells are capped for this cycle."""
    profile = (market_profile or CN_A_SHARE_PROFILE).strip().lower()
    src = (portfolio_source or "ledger").strip().lower()
    if src == "broker":
        return "broker"
    if profile in (CN_A_SHARE_PROFILE, "cn_a", "a_share", "cn-a-share"):
        return "t1"
    return "t0"


def trading_day_from_cycle_time(
    cycle_time: datetime | None,
    *,
    market_profile: str | None = None,
) -> date:
    """Calendar trading day in the market timezone (default A-share → Asia/Shanghai)."""
    del market_profile  # reserved for future market-specific calendars
    if cycle_time is None:
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(_MARKET_TZ)).date()
        except Exception:
            return datetime.utcnow().date()
    if cycle_time.tzinfo is not None:
        try:
            from zoneinfo import ZoneInfo

            return cycle_time.astimezone(ZoneInfo(_MARKET_TZ)).date()
        except Exception:
            return cycle_time.date()
    return cycle_time.date()


def should_run_settlement_trigger_b(
    last_settlement_trading_day: str | None,
    current_trading_day: date,
) -> tuple[bool, str | None]:
    """Return ``(run_unlock, new_last_settlement_day)`` for trigger B.

    - First cycle ever: no unlock, record *current_trading_day*.
    - New trading day: unlock (settle) and advance marker.
    - Same day: no-op.
    """
    current_s = current_trading_day.isoformat()
    if last_settlement_trading_day is None:
        return False, current_s
    if current_s > last_settlement_trading_day:
        return True, current_s
    return False, None


def aggregate_sellable_quantity(
    positions: Iterable[PositionSnapshot],
    symbol: str,
    mode: SettlementMode,
) -> tuple[int, bool]:
    """Sum sellable whole shares for *symbol*.

    Returns ``(sellable_shares, used_legacy_quantity_fallback)``.
    """
    total_sellable = 0
    total_qty = 0
    saw_available = False
    for p in positions:
        if p.symbol != symbol:
            continue
        q = floor_whole_share_count(float(p.quantity))
        if q <= 0:
            continue
        total_qty += q
        if mode == "broker":
            if p.available is not None:
                saw_available = True
                avail = floor_whole_share_count(float(p.available))
                total_sellable += min(q, max(0, avail))
            else:
                total_sellable += q
        else:
            if p.available is not None:
                saw_available = True
                total_sellable += floor_whole_share_count(float(p.available))
            else:
                total_sellable += q

    if mode in ("t1", "t0") and not saw_available and total_qty > 0:
        return total_sellable, True
    return total_sellable, False


def sell_intent_exceeds_sellable(
    intent_shares: float,
    positions: Iterable[PositionSnapshot],
    symbol: str,
    mode: SettlementMode,
) -> bool:
    """True when a sell intent requests more shares than sellable (t1/broker)."""
    if mode == "t0":
        return False
    requested = floor_whole_share_count(float(intent_shares))
    sellable, _ = aggregate_sellable_quantity(positions, symbol, mode)
    return requested > sellable


__all__ = [
    "CN_A_SHARE_PROFILE",
    "SettlementMode",
    "aggregate_sellable_quantity",
    "sell_intent_exceeds_sellable",
    "settlement_mode",
    "should_run_settlement_trigger_b",
    "trading_day_from_cycle_time",
]
