"""Position manager: signal target → concrete :class:`OrderIntent` with final share counts.

Subsumes the sizing / cash-budget / whole-share rounding logic that used to be
split across :mod:`doyoutrade.strategies.review.deterministic`,
:mod:`doyoutrade.strategies.review.review_constraints`, and
``PaperExecutionAdapter._resolved_fill_quantity``.

Semantics (per cycle):

- Legacy 0/1 signals use per-symbol target notional
  ``T = min(equity * equity_fraction, max_single_order_amount)`` when a cap is
  set; otherwise ``T = equity * equity_fraction``.
- Explicit target-exposure signals compute a desired post-cycle notional
  ``equity * target_exposure`` and rebalance toward it, subject to the task's
  position constraints.
- Explicit target-quantity signals compute a desired post-cycle share
  inventory and trade only the share delta, which is the right primitive for
  strict inventory grids that should not rebalance within a price band.
- Both explicit-target paths honor ``PositionConstraints.lot_size`` (buy /
  partial-sell deltas floored to board-lot multiples; full exits exempt so odd
  lots always clear) and ``rebalance_hysteresis_lots`` (sub-dead-band
  rebalances skipped visibly) — see :class:`PositionConstraints`.
- For each signal:

  - ``value == 1`` and currently flat → buy ``floor(min(T, remaining_cash) / price)``
    shares. ``remaining_cash`` is decremented as buys are allocated, so multiple
    long signals share a single cash budget.
  - ``value == 0`` and currently long → sell *all* currently held whole shares.
  - target equals current state → no intent emitted.

Symbols with non-positive reference price are skipped (``no_reference_price``)
— sizing requires a quotable price. Symbols the simulated-bar overlay flagged
as halted on the cycle day are skipped separately (``symbol_suspended``): they
DO carry a carried-forward close for MTM, but no order can fill during a
trading halt, so the order is dropped rather than filled at a stale mark.
``OrderIntent.amount`` follows the runtime convention: notional
for ``buy``, share count for ``sell``. ``OrderIntent.intent_id`` is generated
here (UUID4) so callers can route fills by id without further bookkeeping.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

from doyoutrade.core.models import (
    AccountSnapshot,
    MarketContext,
    OrderIntent,
    PositionSnapshot,
    TaskBudgetSnapshot,
)
from doyoutrade.core.share_math import (
    floor_fraction_shares,
    floor_to_lot,
    floor_whole_share_count,
    max_whole_shares_affordable,
)
from doyoutrade.debug import emit_debug_event_sync
from doyoutrade.execution.fill_pricing import close_price_for_symbol, symbol_is_tradeable
from doyoutrade.execution.settlement import SettlementMode, aggregate_sellable_quantity
from doyoutrade.money.decimal_helpers import decimal_from_number
# PositionManager consumes a small per-symbol record (symbol + legacy target
# state or explicit target_exposure / target_quantity + optional
# tag/rationale) rather than the strategy-facing Signal class.
# Strategy.on_bar returns a typed Signal (direction + tag) and the runner
# adapts it onto this shape before calling compute_intents.


@dataclass(frozen=True)
class PositionSignal:
    """Per-symbol position target instruction consumed by ``PositionManager``.

    - ``value == 1`` → strategy wants the symbol held long after this cycle.
    - ``value == 0`` → strategy wants the symbol flat.
    - ``target_exposure`` → strategy wants the symbol rebalanced to that
      post-cycle long exposure (fraction of account equity in ``[0, 1]``).
    - ``target_quantity`` → strategy wants the symbol held at that absolute
      post-cycle share inventory.
    - ``tag`` is the factor identifier copied from ``Signal.tag`` so the
      OrderIntent — and downstream TradeFillRecord — record which factor
      combination triggered the entry / exit.
    """

    symbol: str
    value: int | None = None
    target_exposure: float | None = None
    target_quantity: float | None = None
    tag: str = ""
    rationale: str = ""
    #: Optional exit categorization copied from ``Signal.exit_reason``
    #: (one of :class:`doyoutrade.strategy_sdk.signal.ExitReason`). Only
    #: meaningful for an exit (``value == 0``); stamped onto the SELL
    #: ``OrderIntent`` so it reaches the fill payload and ``by_exit_reason``.
    exit_reason: str | None = None
    #: Portion of the held position to sell on an exit, in ``(0, 1]`` (copied
    #: from ``Signal.fraction``). ``1.0`` (default) = full exit, unchanged
    #: sizing. A smaller value scales the sell quantity.
    fraction: float = 1.0


logger = logging.getLogger(__name__)


def _emit_skip(
    symbol: str,
    *,
    target_state: int | str | None,
    current_qty: int,
    reason: str,
    detail: dict[str, Any],
) -> None:
    """Surface a silent skip so authors can debug why a signal didn't trade."""
    payload = {
        "symbol": symbol,
        "target_state": target_state,
        "current_qty": current_qty,
        "reason": reason,
        **detail,
    }
    emit_debug_event_sync("position_manager_skipped", payload)
    logger.info(
        "position_manager skipped symbol=%s target_state=%s current_qty=%s reason=%s detail=%s",
        symbol,
        target_state,
        current_qty,
        reason,
        detail,
    )


@dataclass(frozen=True)
class PositionConstraints:
    """Explicit per-cycle constraints driving :class:`PositionManager`.

    - ``equity_fraction``: per-symbol fraction of total equity used to size a
      new long entry (``T = equity * f``). Default ``1.0`` (uses full equity
      per name, then clipped by available cash). Must be in ``(0, 1]``.
    - ``max_single_order_amount``: optional per-order notional cap. ``None``
      = uncapped.
    - ``max_position_ratio``: single-name concentration cap as a fraction of
      total equity (``equity * ratio``). **Enforced** here as a per-symbol
      sizing cap — a would-be-oversized buy is scaled down to fit rather than
      vetoed (the runtime risk engine is pass-through, so this is the only
      place the cap binds in production). Default ``1.0`` is non-binding when
      ``equity_fraction <= 1.0``. Must be in ``(0, 1]``.
    - ``lot_size``: exchange board lot in shares (A股 = 100). Applies to the
      explicit ``target_quantity`` / ``target_exposure`` rebalance paths only:
      buy deltas are floored to a lot multiple (below one lot → visible skip);
      sell deltas are floored too, except a full exit (target 0) which sells
      all sellable shares so odd-lot remainders can always be cleared. Default
      ``1`` = whole-share trading, byte-identical to pre-lot behavior. Must be
      an integer >= 1.
    - ``rebalance_hysteresis_lots``: dead band for the explicit-target paths,
      in lots. A rebalance whose share delta is below
      ``rebalance_hysteresis_lots * lot_size`` is skipped (visible via
      ``position_manager_skipped`` / ``hysteresis_dead_band``) so a grid
      oscillating around a band edge does not churn. Full exits (target 0)
      bypass the dead band. Default ``0`` = disabled. Must be an integer >= 0.
    - ``max_task_position_amount`` / ``max_task_position_ratio``: optional
      task-level total marked-to-market position cap. Unlike
      ``max_position_ratio`` (single-name cap), this constrains the SUM of the
      task's own logical holdings (derived from persisted fills). The current
      usage snapshot is injected at cycle time by the worker; buys are scaled
      down or skipped when the task has exhausted its budget.
    """

    equity_fraction: float = 1.0
    max_single_order_amount: float | None = None
    max_position_ratio: float = 1.0
    lot_size: int = 1
    rebalance_hysteresis_lots: int = 0
    max_task_position_amount: float | None = None
    max_task_position_ratio: float | None = None

    def __post_init__(self) -> None:
        if not (0.0 < self.equity_fraction <= 1.0):
            raise ValueError(
                f"equity_fraction must be in (0, 1], got {self.equity_fraction!r}"
            )
        if self.max_single_order_amount is not None and self.max_single_order_amount <= 0:
            raise ValueError(
                f"max_single_order_amount must be positive or None, got {self.max_single_order_amount!r}"
            )
        if not (0.0 < self.max_position_ratio <= 1.0):
            raise ValueError(
                f"max_position_ratio must be in (0, 1], got {self.max_position_ratio!r}"
            )
        if isinstance(self.lot_size, bool) or not isinstance(self.lot_size, int):
            raise ValueError(
                f"lot_size must be an integer, got {type(self.lot_size).__name__}: {self.lot_size!r}"
            )
        if self.lot_size < 1:
            raise ValueError(f"lot_size must be >= 1, got {self.lot_size!r}")
        if isinstance(self.rebalance_hysteresis_lots, bool) or not isinstance(
            self.rebalance_hysteresis_lots, int
        ):
            raise ValueError(
                f"rebalance_hysteresis_lots must be an integer, got "
                f"{type(self.rebalance_hysteresis_lots).__name__}: {self.rebalance_hysteresis_lots!r}"
            )
        if self.rebalance_hysteresis_lots < 0:
            raise ValueError(
                f"rebalance_hysteresis_lots must be >= 0, got {self.rebalance_hysteresis_lots!r}"
            )
        if self.max_task_position_amount is not None and self.max_task_position_amount <= 0:
            raise ValueError(
                "max_task_position_amount must be positive or None, "
                f"got {self.max_task_position_amount!r}"
            )
        if self.max_task_position_ratio is not None and not (
            0.0 < self.max_task_position_ratio <= 1.0
        ):
            raise ValueError(
                "max_task_position_ratio must be in (0, 1] or None, "
                f"got {self.max_task_position_ratio!r}"
            )


def _effective_task_budget_cap(
    *,
    constraints: PositionConstraints,
    account_snapshot: AccountSnapshot,
    snapshot: TaskBudgetSnapshot | None,
) -> Decimal | None:
    if snapshot is not None and snapshot.budget_cap is not None:
        return snapshot.budget_cap
    caps: list[Decimal] = []
    if constraints.max_task_position_amount is not None:
        caps.append(decimal_from_number(constraints.max_task_position_amount))
    if constraints.max_task_position_ratio is not None:
        caps.append(
            account_snapshot.equity * decimal_from_number(constraints.max_task_position_ratio)
        )
    if not caps:
        return None
    return min(caps)


def _aggregate_current_quantity(
    positions: Iterable[PositionSnapshot], symbol: str
) -> int:
    """Sum positive quantities across position rows for a symbol; floor to whole shares."""
    total = Decimal(0)
    for p in positions:
        if p.symbol != symbol:
            continue
        q = decimal_from_number(p.quantity)
        if q <= 0:
            continue
        total += q
    return floor_whole_share_count(float(total))


def _position_notional(quantity: int, price: float) -> Decimal:
    return Decimal(quantity) * decimal_from_number(price)


def _merge_rationale(base: str, appendix: str) -> str:
    a = (base or "").strip()
    b = (appendix or "").strip()
    if a and b:
        return f"{a}; {b}"
    return a or b


def _build_buy_intent(
    *,
    symbol: str,
    shares: int,
    price: float,
    rationale: str,
    strategy_tag: str,
    signal_tag: str = "",
) -> OrderIntent:
    notional = float(Decimal(shares) * decimal_from_number(price))
    return OrderIntent(
        intent_id=f"oi-{uuid.uuid4()}",
        symbol=symbol,
        action="buy",
        amount=notional,
        order_type="market",
        tif="day",
        strategy_tag=strategy_tag,
        price_reference=price,
        rationale=rationale,
        signal_tag=signal_tag,
    )


def _build_sell_intent(
    *,
    symbol: str,
    shares: int,
    price: float,
    rationale: str,
    strategy_tag: str,
    signal_tag: str = "",
    exit_reason: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        intent_id=f"oi-{uuid.uuid4()}",
        symbol=symbol,
        action="sell",
        amount=float(shares),
        order_type="market",
        tif="day",
        strategy_tag=strategy_tag,
        price_reference=price,
        rationale=rationale,
        signal_tag=signal_tag,
        exit_reason=exit_reason,
    )


@dataclass
class PositionManager:
    """Diff (signal target − current holdings) → :class:`OrderIntent` list.

    Deterministic, side-effect free. Designed so the worker can call it once
    per cycle and trust the emitted intents need no further sizing
    adjustment downstream — the execution adapter just records the fill.
    """

    constraints: PositionConstraints = field(default_factory=PositionConstraints)
    strategy_tag: str = ""
    settlement_mode: SettlementMode = "t0"

    def compute_intents(
        self,
        signals: list["PositionSignal"],
        account_snapshot: AccountSnapshot,
        positions: list[PositionSnapshot],
        market_context: MarketContext,
        *,
        task_budget_snapshot: TaskBudgetSnapshot | None = None,
        settlement_mode: SettlementMode | None = None,
    ) -> list[OrderIntent]:
        """Translate signals to ordered intents using current account / positions.

        Buys are processed in input order and share a single decrementing
        cash budget; sells are emitted regardless of cash state. Symbols
        with non-positive reference price are silently skipped.
        """
        mode: SettlementMode = settlement_mode if settlement_mode is not None else self.settlement_mode

        equity_d = account_snapshot.equity
        cash_d = account_snapshot.cash
        eq_fraction_d = decimal_from_number(self.constraints.equity_fraction)
        per_symbol_eq_d = equity_d * eq_fraction_d
        cap = self.constraints.max_single_order_amount
        per_symbol_cap_d: Decimal | None = (
            decimal_from_number(cap) if cap is not None else None
        )
        ratio_cap_d = equity_d * decimal_from_number(self.constraints.max_position_ratio)
        legacy_caps: list[Decimal] = [per_symbol_eq_d, ratio_cap_d]
        if per_symbol_cap_d is not None:
            legacy_caps.append(per_symbol_cap_d)
        t_d = min(legacy_caps)
        if ratio_cap_d < per_symbol_eq_d:
            emit_debug_event_sync(
                "position_manager_ratio_capped",
                {
                    "reason": "max_position_ratio_scaled",
                    "max_position_ratio": float(self.constraints.max_position_ratio),
                    "equity_fraction_budget": str(per_symbol_eq_d),
                    "ratio_cap": str(ratio_cap_d),
                    "applied_target_notional": str(t_d),
                    "hint": (
                        "Per-symbol buy size is capped by max_position_ratio "
                        "(equity * ratio); raise max_position_ratio to allow larger "
                        "single-name exposure."
                    ),
                },
            )

        lot = self.constraints.lot_size
        # Dead band for the explicit-target rebalance paths, in shares. 0 = off.
        hysteresis_shares = self.constraints.rebalance_hysteresis_lots * lot

        remaining_cash_d = cash_d
        task_budget_cap_d = _effective_task_budget_cap(
            constraints=self.constraints,
            account_snapshot=account_snapshot,
            snapshot=task_budget_snapshot,
        )
        current_task_usage_d = (
            task_budget_snapshot.current_usage
            if task_budget_snapshot is not None
            else Decimal(0)
        )
        remaining_task_budget_d: Decimal | None = None
        if task_budget_cap_d is not None:
            remaining_task_budget_d = max(
                Decimal(0), task_budget_cap_d - current_task_usage_d
            )
            emit_debug_event_sync(
                "task_budget_snapshot",
                {
                    "budget_cap": str(task_budget_cap_d),
                    "current_usage": str(current_task_usage_d),
                    "remaining_budget": str(remaining_task_budget_d),
                    "max_task_position_amount": (
                        str(self.constraints.max_task_position_amount)
                        if self.constraints.max_task_position_amount is not None
                        else None
                    ),
                    "max_task_position_ratio": self.constraints.max_task_position_ratio,
                    "position_count": len(task_budget_snapshot.positions)
                    if task_budget_snapshot is not None
                    else 0,
                    "warnings": list(task_budget_snapshot.warnings)
                    if task_budget_snapshot is not None
                    else [],
                },
            )
        intents: list[OrderIntent] = []

        for signal in signals:
            symbol = signal.symbol
            target_state = signal.value
            current_qty = _aggregate_current_quantity(positions, symbol)
            current_state = 1 if current_qty > 0 else 0

            price = close_price_for_symbol(symbol, market_context)
            if price <= 0:
                _emit_skip(
                    symbol,
                    target_state=(
                        "target_exposure" if signal.target_exposure is not None else target_state
                    ),
                    current_qty=current_qty,
                    reason="no_reference_price",
                    detail={
                        "price": float(price),
                        "hint": (
                            "close_price_for_symbol returned <= 0 — symbol missing from "
                            "market_context.symbol_to_price / symbol_to_tick. Check the "
                            "universe provisioning and that the data provider returned "
                            "quotes for this cycle."
                        ),
                    },
                )
                continue

            if not symbol_is_tradeable(symbol, market_context):
                # The symbol carries a reference close (so it is NOT a
                # no_reference_price case) but the simulated-bar overlay flagged
                # the cycle day as a confirmed halt. No order can execute during
                # a trading halt, so skip with a precise reason instead of
                # fabricating a fill at the carried-forward mark (§错误可见性).
                _emit_skip(
                    symbol,
                    target_state=(
                        "target_exposure" if signal.target_exposure is not None else target_state
                    ),
                    current_qty=current_qty,
                    reason="symbol_suspended",
                    detail={
                        "price": float(price),
                        "hint": (
                            "symbol halted on the cycle day (tradestatus==0); the close is "
                            "carried forward for MTM only and no order can fill during a "
                            "trading halt. Distinct from no_reference_price (a missing-row "
                            "data gap, which DOES price and trade via carry-forward)."
                        ),
                    },
                )
                continue

            if signal.target_exposure is not None:
                raw_target_notional_d = equity_d * decimal_from_number(signal.target_exposure)
                target_notional_d = min(raw_target_notional_d, per_symbol_eq_d, ratio_cap_d)
                if target_notional_d < raw_target_notional_d:
                    cap_reasons: list[str] = []
                    if per_symbol_eq_d < raw_target_notional_d:
                        cap_reasons.append("equity_fraction")
                    if ratio_cap_d < raw_target_notional_d:
                        cap_reasons.append("max_position_ratio")
                    emit_debug_event_sync(
                        "position_manager_target_exposure_capped",
                        {
                            "symbol": symbol,
                            "reason": "target_exposure_capped",
                            "target_exposure": float(signal.target_exposure),
                            "requested_target_notional": str(raw_target_notional_d),
                            "equity_fraction_cap": str(per_symbol_eq_d),
                            "ratio_cap": str(ratio_cap_d),
                            "applied_target_notional": str(target_notional_d),
                            "cap_reasons": cap_reasons,
                            "hint": (
                                "Explicit target_exposure was capped by task-level position "
                                "constraints. Raise equity_fraction or max_position_ratio "
                                "to allow a larger target inventory."
                            ),
                        },
                    )
                current_notional_d = _position_notional(current_qty, price)
                if current_notional_d == target_notional_d:
                    continue
                rebalance_delta_shares = floor_whole_share_count(
                    float(
                        abs(target_notional_d - current_notional_d)
                        / decimal_from_number(price)
                    )
                )
                # Full exits (target 0) bypass the dead band — an exit signal
                # must always be able to flatten the position.
                if (
                    hysteresis_shares > 0
                    and target_notional_d > 0
                    and rebalance_delta_shares < hysteresis_shares
                ):
                    _emit_skip(
                        symbol,
                        target_state="target_exposure",
                        current_qty=current_qty,
                        reason="hysteresis_dead_band",
                        detail={
                            "target_exposure": float(signal.target_exposure),
                            "delta_shares": rebalance_delta_shares,
                            "hysteresis_lots": self.constraints.rebalance_hysteresis_lots,
                            "lot_size": lot,
                            "current_notional": str(current_notional_d),
                            "target_notional": str(target_notional_d),
                            "hint": (
                                "Rebalance delta is below the configured "
                                "rebalance_hysteresis_lots dead band; lower the "
                                "hysteresis if the position should track the "
                                "target more tightly."
                            ),
                        },
                    )
                    continue
                if current_notional_d < target_notional_d:
                    desired_delta_d = target_notional_d - current_notional_d
                    budget_caps: list[Decimal] = [desired_delta_d, remaining_cash_d]
                    if per_symbol_cap_d is not None:
                        budget_caps.append(per_symbol_cap_d)
                    if remaining_task_budget_d is not None:
                        budget_caps.append(remaining_task_budget_d)
                    budget_d = min(budget_caps)
                    if budget_d <= 0:
                        exhausted_by_task_budget = (
                            remaining_task_budget_d is not None
                            and remaining_task_budget_d <= 0
                        )
                        _emit_skip(
                            symbol,
                            target_state="target_exposure",
                            current_qty=current_qty,
                            reason=(
                                "task_budget_exhausted"
                                if exhausted_by_task_budget
                                else "insufficient_cash_budget"
                            ),
                            detail={
                                "remaining_cash": str(remaining_cash_d),
                                "remaining_task_budget": (
                                    str(remaining_task_budget_d)
                                    if remaining_task_budget_d is not None
                                    else None
                                ),
                                "target_exposure": float(signal.target_exposure),
                                "current_notional": str(current_notional_d),
                                "target_notional": str(target_notional_d),
                                "desired_delta_notional": str(desired_delta_d),
                                "hint": (
                                    "This task has exhausted its own configured total "
                                    "budget; lower current exposure, raise "
                                    "max_task_position_amount/max_task_position_ratio, "
                                    "or wait for sells to reduce task-owned inventory."
                                    if exhausted_by_task_budget
                                    else
                                    "Rebalance toward target_exposure needs more cash than is "
                                    "available this cycle. Earlier buys may have exhausted the "
                                    "budget, or the target is above the configured limits."
                                ),
                            },
                        )
                        continue
                    task_budget_bound = (
                        remaining_task_budget_d is not None
                        and remaining_task_budget_d < desired_delta_d
                        and budget_d == remaining_task_budget_d
                    )
                    if task_budget_bound:
                        emit_debug_event_sync(
                            "position_manager_task_budget_capped",
                            {
                                "symbol": symbol,
                                "reason": "task_budget_capped",
                                "target_state": "target_exposure",
                                "desired_delta_notional": str(desired_delta_d),
                                "remaining_task_budget": str(remaining_task_budget_d),
                                "applied_budget": str(budget_d),
                                "hint": (
                                    "Task-level total position budget capped this buy. "
                                    "Lower task-owned inventory first or raise "
                                    "max_task_position_amount/max_task_position_ratio."
                                ),
                            },
                        )
                    shares_pre_lot = max_whole_shares_affordable(float(budget_d), price)
                    shares = floor_to_lot(shares_pre_lot, lot)
                    if shares <= 0:
                        below_one_lot = shares_pre_lot > 0
                        _emit_skip(
                            symbol,
                            target_state="target_exposure",
                            current_qty=current_qty,
                            reason=(
                                "target_exposure_buy_below_one_lot"
                                if below_one_lot
                                else "target_exposure_buy_rounds_to_zero"
                            ),
                            detail={
                                "price": price,
                                "budget": str(budget_d),
                                "lot_size": lot,
                                "affordable_shares": shares_pre_lot,
                                "target_exposure": float(signal.target_exposure),
                                "current_notional": str(current_notional_d),
                                "target_notional": str(target_notional_d),
                                "hint": (
                                    "The affordable rebalance delta is below one board lot "
                                    "(lot_size shares); buys must be lot multiples."
                                    if below_one_lot
                                    else "The remaining rebalance delta is below one share at the "
                                    "current price. Raise the target or accept that the "
                                    "position is already as close as whole-share trading allows."
                                ),
                            },
                        )
                        continue
                    actual_notional_d = Decimal(shares) * decimal_from_number(price)
                    remaining_cash_d -= actual_notional_d
                    if remaining_task_budget_d is not None:
                        remaining_task_budget_d = max(
                            Decimal(0), remaining_task_budget_d - actual_notional_d
                        )
                    rationale = _merge_rationale(
                        signal.rationale,
                        f"target_exposure={signal.target_exposure} rebalance buy: shares={shares} @ {price} "
                        f"(current_notional={current_notional_d}, target_notional={target_notional_d}, "
                        f"desired_delta={desired_delta_d}, budget={budget_d}, notional={actual_notional_d}, "
                        f"task_budget_remaining={remaining_task_budget_d})",
                    )
                    intents.append(
                        _build_buy_intent(
                            symbol=symbol,
                            shares=shares,
                            price=price,
                            rationale=rationale,
                            strategy_tag=self.strategy_tag,
                            signal_tag=getattr(signal, "tag", "") or "",
                        )
                    )
                    continue

                sellable, legacy_fallback = aggregate_sellable_quantity(
                    positions, symbol, mode
                )
                if legacy_fallback:
                    emit_debug_event_sync(
                        "settlement_legacy_position_no_available",
                        {
                            "symbol": symbol,
                            "settlement_mode": mode,
                            "hint": (
                                "position.available missing on ledger row; "
                                "fell back to quantity for sellable sizing"
                            ),
                        },
                    )
                    logger.warning(
                        "position_manager legacy available missing symbol=%s mode=%s",
                        symbol,
                        mode,
                    )
                if sellable <= 0 and current_qty > 0:
                    _emit_skip(
                        symbol,
                        target_state="target_exposure",
                        current_qty=current_qty,
                        reason="settlement_t1_no_available",
                        detail={
                            "sellable": sellable,
                            "quantity": current_qty,
                            "settlement_mode": mode,
                            "target_exposure": float(signal.target_exposure),
                            "hint": (
                                "Shares are held but not sellable under T+1 "
                                "(bought same trading day or not yet settled)."
                            ),
                        },
                    )
                    continue
                full_sellable = min(sellable, current_qty)
                desired_reduce_d = current_notional_d - target_notional_d
                if target_notional_d <= 0:
                    # Full exit: sell everything sellable, odd lots included —
                    # lot alignment must never strand a residual position.
                    desired_shares_to_sell = current_qty
                    sell_pre_lot = current_qty
                else:
                    sell_pre_lot = floor_whole_share_count(
                        float(desired_reduce_d / decimal_from_number(price))
                    )
                    desired_shares_to_sell = floor_to_lot(sell_pre_lot, lot)
                if desired_shares_to_sell <= 0 and full_sellable > 0:
                    below_one_lot = sell_pre_lot > 0
                    _emit_skip(
                        symbol,
                        target_state="target_exposure",
                        current_qty=current_qty,
                        reason=(
                            "target_exposure_sell_below_one_lot"
                            if below_one_lot
                            else "target_exposure_sell_rounds_to_zero"
                        ),
                        detail={
                            "sellable": full_sellable,
                            "lot_size": lot,
                            "desired_sell_shares": sell_pre_lot,
                            "target_exposure": float(signal.target_exposure),
                            "current_notional": str(current_notional_d),
                            "target_notional": str(target_notional_d),
                            "hint": (
                                "The rebalance-down delta is below one board lot; sells on "
                                "a partial reduce must be lot multiples (full exits are "
                                "exempt and clear odd lots)."
                                if below_one_lot
                                else "The rebalance-down delta is below one whole share at the "
                                "current price. Lower the target further if you need an exit."
                            ),
                        },
                    )
                    continue
                shares_to_sell = min(full_sellable, desired_shares_to_sell)
                rationale = _merge_rationale(
                    signal.rationale,
                    f"target_exposure={signal.target_exposure} rebalance sell: sell {shares_to_sell} shares @ {price} "
                    f"(sellable={sellable}, qty={current_qty}, current_notional={current_notional_d}, "
                    f"target_notional={target_notional_d}, desired_reduce={desired_reduce_d})",
                )
                intents.append(
                    _build_sell_intent(
                        symbol=symbol,
                        shares=shares_to_sell,
                        price=price,
                        rationale=rationale,
                        strategy_tag=self.strategy_tag,
                        signal_tag=getattr(signal, "tag", "") or "",
                        exit_reason=getattr(signal, "exit_reason", None),
                    )
                )
                continue

            if signal.target_quantity is not None:
                desired_target_qty = floor_whole_share_count(float(signal.target_quantity))
                lot_aligned_target_qty = floor_to_lot(desired_target_qty, lot)
                if lot_aligned_target_qty != desired_target_qty:
                    emit_debug_event_sync(
                        "position_manager_target_quantity_lot_aligned",
                        {
                            "symbol": symbol,
                            "reason": "target_quantity_lot_aligned",
                            "target_quantity": float(signal.target_quantity),
                            "requested_target_quantity": desired_target_qty,
                            "lot_size": lot,
                            "applied_target_quantity": lot_aligned_target_qty,
                            "hint": (
                                "target_quantity is not a multiple of the configured "
                                "lot_size; the strategy should emit lot-multiple targets "
                                "(e.g. shares_per_level = lot_size)."
                            ),
                        },
                    )
                # Cap is lot-aligned too — otherwise a binding cap reintroduces
                # odd-lot targets and band-edge churn against the aligned state.
                cap_target_qty = floor_to_lot(
                    floor_whole_share_count(
                        float(min(per_symbol_eq_d, ratio_cap_d) / decimal_from_number(price))
                    ),
                    lot,
                )
                effective_target_qty = min(lot_aligned_target_qty, cap_target_qty)
                if effective_target_qty < lot_aligned_target_qty:
                    emit_debug_event_sync(
                        "position_manager_target_quantity_capped",
                        {
                            "symbol": symbol,
                            "reason": "target_quantity_capped",
                            "target_quantity": float(signal.target_quantity),
                            "requested_target_quantity": lot_aligned_target_qty,
                            "equity_fraction_cap": str(per_symbol_eq_d),
                            "ratio_cap": str(ratio_cap_d),
                            "cap_target_quantity": cap_target_qty,
                            "applied_target_quantity": effective_target_qty,
                            "hint": (
                                "Explicit target_quantity was capped by task-level position "
                                "constraints. Raise equity_fraction or max_position_ratio "
                                "to allow a larger strict inventory target."
                            ),
                        },
                    )
                if effective_target_qty == current_qty:
                    continue
                # Full exits (target 0) bypass the dead band — an exit signal
                # must always be able to flatten the position.
                if (
                    hysteresis_shares > 0
                    and effective_target_qty > 0
                    and abs(effective_target_qty - current_qty) < hysteresis_shares
                ):
                    _emit_skip(
                        symbol,
                        target_state="target_quantity",
                        current_qty=current_qty,
                        reason="hysteresis_dead_band",
                        detail={
                            "target_quantity": float(signal.target_quantity),
                            "effective_target_qty": effective_target_qty,
                            "delta_shares": abs(effective_target_qty - current_qty),
                            "hysteresis_lots": self.constraints.rebalance_hysteresis_lots,
                            "lot_size": lot,
                            "hint": (
                                "Strict-inventory delta is below the configured "
                                "rebalance_hysteresis_lots dead band; lower the "
                                "hysteresis if the inventory should track the target "
                                "more tightly."
                            ),
                        },
                    )
                    continue
                if effective_target_qty > current_qty:
                    desired_delta_shares = effective_target_qty - current_qty
                    desired_delta_notional_d = _position_notional(
                        desired_delta_shares, price
                    )
                    budget_caps: list[Decimal] = [
                        desired_delta_notional_d,
                        remaining_cash_d,
                    ]
                    if per_symbol_cap_d is not None:
                        budget_caps.append(per_symbol_cap_d)
                    if remaining_task_budget_d is not None:
                        budget_caps.append(remaining_task_budget_d)
                    budget_d = min(budget_caps)
                    if budget_d <= 0:
                        exhausted_by_task_budget = (
                            remaining_task_budget_d is not None
                            and remaining_task_budget_d <= 0
                        )
                        _emit_skip(
                            symbol,
                            target_state="target_quantity",
                            current_qty=current_qty,
                            reason=(
                                "task_budget_exhausted"
                                if exhausted_by_task_budget
                                else "insufficient_cash_budget"
                            ),
                            detail={
                                "remaining_cash": str(remaining_cash_d),
                                "remaining_task_budget": (
                                    str(remaining_task_budget_d)
                                    if remaining_task_budget_d is not None
                                    else None
                                ),
                                "target_quantity": float(signal.target_quantity),
                                "current_qty": current_qty,
                                "effective_target_qty": effective_target_qty,
                                "desired_delta_shares": desired_delta_shares,
                                "hint": (
                                    "This task has exhausted its own configured total "
                                    "budget; lower current exposure, raise "
                                    "max_task_position_amount/max_task_position_ratio, "
                                    "or wait for sells to reduce task-owned inventory."
                                    if exhausted_by_task_budget
                                    else
                                    "Strict inventory rebalance needs more cash than is "
                                    "available this cycle. Earlier buys may have exhausted "
                                    "the budget, or the target is above configured limits."
                                ),
                            },
                        )
                        continue
                    task_budget_bound = (
                        remaining_task_budget_d is not None
                        and remaining_task_budget_d < desired_delta_notional_d
                        and budget_d == remaining_task_budget_d
                    )
                    if task_budget_bound:
                        emit_debug_event_sync(
                            "position_manager_task_budget_capped",
                            {
                                "symbol": symbol,
                                "reason": "task_budget_capped",
                                "target_state": "target_quantity",
                                "desired_delta_notional": str(desired_delta_notional_d),
                                "remaining_task_budget": str(remaining_task_budget_d),
                                "applied_budget": str(budget_d),
                                "hint": (
                                    "Task-level total position budget capped this buy. "
                                    "Lower task-owned inventory first or raise "
                                    "max_task_position_amount/max_task_position_ratio."
                                ),
                            },
                        )
                    shares_pre_lot = min(
                        desired_delta_shares,
                        max_whole_shares_affordable(float(budget_d), price),
                    )
                    shares = floor_to_lot(shares_pre_lot, lot)
                    if shares <= 0:
                        below_one_lot = shares_pre_lot > 0
                        _emit_skip(
                            symbol,
                            target_state="target_quantity",
                            current_qty=current_qty,
                            reason=(
                                "target_quantity_buy_below_one_lot"
                                if below_one_lot
                                else "target_quantity_buy_rounds_to_zero"
                            ),
                            detail={
                                "price": price,
                                "budget": str(budget_d),
                                "lot_size": lot,
                                "affordable_shares": shares_pre_lot,
                                "target_quantity": float(signal.target_quantity),
                                "current_qty": current_qty,
                                "effective_target_qty": effective_target_qty,
                                "hint": (
                                    "The affordable strict-inventory delta is below one "
                                    "board lot (lot_size shares); buys must be lot multiples."
                                    if below_one_lot
                                    else "The remaining strict-inventory delta is below one share "
                                    "at the current price. Raise the target quantity or "
                                    "accept that current holdings already match within "
                                    "whole-share limits."
                                ),
                            },
                        )
                        continue
                    actual_notional_d = _position_notional(shares, price)
                    remaining_cash_d -= actual_notional_d
                    if remaining_task_budget_d is not None:
                        remaining_task_budget_d = max(
                            Decimal(0), remaining_task_budget_d - actual_notional_d
                        )
                    rationale = _merge_rationale(
                        signal.rationale,
                        f"target_quantity={signal.target_quantity} strict-grid buy: shares={shares} @ {price} "
                        f"(current_qty={current_qty}, effective_target_qty={effective_target_qty}, "
                        f"desired_delta_shares={desired_delta_shares}, budget={budget_d}, "
                        f"notional={actual_notional_d}, task_budget_remaining={remaining_task_budget_d})",
                    )
                    intents.append(
                        _build_buy_intent(
                            symbol=symbol,
                            shares=shares,
                            price=price,
                            rationale=rationale,
                            strategy_tag=self.strategy_tag,
                            signal_tag=getattr(signal, "tag", "") or "",
                        )
                    )
                    continue

                sellable, legacy_fallback = aggregate_sellable_quantity(
                    positions, symbol, mode
                )
                if legacy_fallback:
                    emit_debug_event_sync(
                        "settlement_legacy_position_no_available",
                        {
                            "symbol": symbol,
                            "settlement_mode": mode,
                            "hint": (
                                "position.available missing on ledger row; "
                                "fell back to quantity for sellable sizing"
                            ),
                        },
                    )
                    logger.warning(
                        "position_manager legacy available missing symbol=%s mode=%s",
                        symbol,
                        mode,
                    )
                if sellable <= 0 and current_qty > 0:
                    _emit_skip(
                        symbol,
                        target_state="target_quantity",
                        current_qty=current_qty,
                        reason="settlement_t1_no_available",
                        detail={
                            "sellable": sellable,
                            "quantity": current_qty,
                            "settlement_mode": mode,
                            "target_quantity": float(signal.target_quantity),
                            "hint": (
                                "Shares are held but not sellable under T+1 "
                                "(bought same trading day or not yet settled)."
                            ),
                        },
                    )
                    continue
                reduce_pre_lot = current_qty - effective_target_qty
                if effective_target_qty > 0:
                    # Partial reduce must be a lot multiple; a full exit
                    # (target 0) sells everything so odd lots always clear.
                    desired_reduce_shares = floor_to_lot(reduce_pre_lot, lot)
                else:
                    desired_reduce_shares = reduce_pre_lot
                shares_to_sell = min(
                    min(sellable, current_qty), desired_reduce_shares
                )
                if shares_to_sell <= 0:
                    _emit_skip(
                        symbol,
                        target_state="target_quantity",
                        current_qty=current_qty,
                        reason="target_quantity_sell_below_one_lot",
                        detail={
                            "sellable": min(sellable, current_qty),
                            "lot_size": lot,
                            "desired_sell_shares": reduce_pre_lot,
                            "target_quantity": float(signal.target_quantity),
                            "effective_target_qty": effective_target_qty,
                            "hint": (
                                "The strict-inventory reduce delta is below one board "
                                "lot; sells on a partial reduce must be lot multiples "
                                "(full exits are exempt and clear odd lots)."
                            ),
                        },
                    )
                    continue
                rationale = _merge_rationale(
                    signal.rationale,
                    f"target_quantity={signal.target_quantity} strict-grid sell: sell {shares_to_sell} shares @ {price} "
                    f"(sellable={sellable}, current_qty={current_qty}, effective_target_qty={effective_target_qty}, "
                    f"desired_reduce_shares={desired_reduce_shares})",
                )
                intents.append(
                    _build_sell_intent(
                        symbol=symbol,
                        shares=shares_to_sell,
                        price=price,
                        rationale=rationale,
                        strategy_tag=self.strategy_tag,
                        signal_tag=getattr(signal, "tag", "") or "",
                        exit_reason=getattr(signal, "exit_reason", None),
                    )
                )
                continue

            if target_state == current_state:
                continue

            if target_state == 1:
                budget_caps = [t_d, remaining_cash_d]
                if remaining_task_budget_d is not None:
                    budget_caps.append(remaining_task_budget_d)
                budget_d = min(budget_caps)
                if budget_d <= 0:
                    exhausted_by_task_budget = (
                        remaining_task_budget_d is not None
                        and remaining_task_budget_d <= 0
                    )
                    _emit_skip(
                        symbol,
                        target_state=target_state,
                        current_qty=current_qty,
                        reason=(
                            "task_budget_exhausted"
                            if exhausted_by_task_budget
                            else "insufficient_cash_budget"
                        ),
                        detail={
                            "remaining_cash": str(remaining_cash_d),
                            "remaining_task_budget": (
                                str(remaining_task_budget_d)
                                if remaining_task_budget_d is not None
                                else None
                            ),
                            "target_notional_cap": str(t_d),
                            "hint": (
                                "This task has exhausted its own configured total "
                                "budget; lower current exposure, raise "
                                "max_task_position_amount/max_task_position_ratio, "
                                "or wait for sells to reduce task-owned inventory."
                                if exhausted_by_task_budget
                                else
                                "Equity*fraction or remaining cash hit zero. Earlier buys "
                                "this cycle may have exhausted the budget, or "
                                "equity_fraction is set too low."
                            ),
                        },
                    )
                    continue
                task_budget_bound = (
                    remaining_task_budget_d is not None
                    and remaining_task_budget_d < t_d
                    and budget_d == remaining_task_budget_d
                )
                if task_budget_bound:
                    emit_debug_event_sync(
                        "position_manager_task_budget_capped",
                        {
                            "symbol": symbol,
                            "reason": "task_budget_capped",
                            "target_state": target_state,
                            "requested_notional": str(t_d),
                            "remaining_task_budget": str(remaining_task_budget_d),
                            "applied_budget": str(budget_d),
                            "hint": (
                                "Task-level total position budget capped this buy. "
                                "Lower task-owned inventory first or raise "
                                "max_task_position_amount/max_task_position_ratio."
                            ),
                        },
                    )
                shares = max_whole_shares_affordable(float(budget_d), price)
                if shares <= 0:
                    _emit_skip(
                        symbol,
                        target_state=target_state,
                        current_qty=current_qty,
                        reason="sub_one_share_at_price",
                        detail={
                            "price": price,
                            "budget": str(budget_d),
                            "hint": (
                                "Budget is below the price of a single share. Raise "
                                "equity_fraction or max_single_order_amount, or accept "
                                "that this symbol is too expensive for the current account."
                            ),
                        },
                    )
                    continue
                actual_notional_d = Decimal(shares) * decimal_from_number(price)
                remaining_cash_d -= actual_notional_d
                if remaining_task_budget_d is not None:
                    remaining_task_budget_d = max(
                        Decimal(0), remaining_task_budget_d - actual_notional_d
                    )
                rationale = _merge_rationale(
                    signal.rationale,
                    f"signal=1 enter long: shares={shares} @ {price} "
                    f"(T={t_d}, budget={budget_d}, notional={actual_notional_d}, "
                    f"task_budget_remaining={remaining_task_budget_d})",
                )
                intents.append(
                    _build_buy_intent(
                        symbol=symbol,
                        shares=shares,
                        price=price,
                        rationale=rationale,
                        strategy_tag=self.strategy_tag,
                        signal_tag=getattr(signal, "tag", "") or "",
                    )
                )
                continue

            sellable, legacy_fallback = aggregate_sellable_quantity(
                positions, symbol, mode
            )
            if legacy_fallback:
                emit_debug_event_sync(
                    "settlement_legacy_position_no_available",
                    {
                        "symbol": symbol,
                        "settlement_mode": mode,
                        "hint": (
                            "position.available missing on ledger row; "
                            "fell back to quantity for sellable sizing"
                        ),
                    },
                )
                logger.warning(
                    "position_manager legacy available missing symbol=%s mode=%s",
                    symbol,
                    mode,
                )
            if sellable <= 0 and current_qty > 0:
                _emit_skip(
                    symbol,
                    target_state=target_state,
                    current_qty=current_qty,
                    reason="settlement_t1_no_available",
                    detail={
                        "sellable": sellable,
                        "quantity": current_qty,
                        "settlement_mode": mode,
                        "hint": (
                            "Shares are held but not sellable under T+1 "
                            "(bought same trading day or not yet settled)."
                        ),
                    },
                )
                continue
            full_sellable = min(sellable, current_qty)
            fraction = getattr(signal, "fraction", 1.0) or 1.0
            shares_to_sell = floor_fraction_shares(full_sellable, fraction)
            if shares_to_sell <= 0 and full_sellable > 0:
                _emit_skip(
                    symbol,
                    target_state=target_state,
                    current_qty=current_qty,
                    reason="partial_exit_rounds_to_zero",
                    detail={
                        "sellable": full_sellable,
                        "fraction": float(fraction),
                        "hint": (
                            "Signal.sell(fraction=...) * sellable shares floors "
                            "to 0 whole shares — position too small for this "
                            "fraction. Raise the fraction or exit in full."
                        ),
                    },
                )
                continue
            rationale = _merge_rationale(
                signal.rationale,
                f"signal=0 exit long: sell {shares_to_sell} shares @ {price} "
                f"(sellable={sellable}, qty={current_qty}, fraction={fraction})",
            )
            intents.append(
                _build_sell_intent(
                    symbol=symbol,
                    shares=shares_to_sell,
                    price=price,
                    rationale=rationale,
                    strategy_tag=self.strategy_tag,
                    signal_tag=getattr(signal, "tag", "") or "",
                    exit_reason=getattr(signal, "exit_reason", None),
                )
            )

        return intents


__all__ = ["PositionConstraints", "PositionManager", "PositionSignal"]
