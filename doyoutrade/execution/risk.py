from __future__ import annotations

from dataclasses import dataclass
from typing import List

from doyoutrade.core.models import RiskDecision
from doyoutrade.core.share_math import floor_whole_share_count
from doyoutrade.execution.settlement import SettlementMode, sell_intent_exceeds_sellable
from doyoutrade.money.decimal_helpers import decimal_from_number


@dataclass
class RiskConfig:
    #: ``None`` = do not veto on per-order notional cap.
    max_single_order_amount: float | None
    max_position_ratio: float


def merge_risk_config(
    base: RiskConfig,
    *,
    max_single_order_amount: float | None = None,
    max_position_ratio: float | None = None,
) -> RiskConfig:
    """Apply optional per-instance overrides; unset fields keep ``base`` values."""
    return RiskConfig(
        max_single_order_amount=(
            base.max_single_order_amount if max_single_order_amount is None else float(max_single_order_amount)
        ),
        max_position_ratio=(
            base.max_position_ratio if max_position_ratio is None else float(max_position_ratio)
        ),
    )


class BasicRiskEngine:
    def __init__(self, config: RiskConfig):
        self.config = config

    def evaluate(
        self,
        intents,
        account_snapshot,
        positions,
        *,
        cycle_state=None,
        settlement_mode: SettlementMode = "t0",
    ) -> List[RiskDecision]:
        decisions: List[RiskDecision] = []

        position_by_symbol = {position.symbol: position for position in positions}

        for intent in intents:
            if intent.action == "sell" and settlement_mode in ("t1", "broker"):
                if intent.amount is None or sell_intent_exceeds_sellable(
                    float(intent.amount),
                    positions,
                    intent.symbol,
                    settlement_mode,
                ):
                    decisions.append(
                        RiskDecision(
                            intent_id=intent.intent_id,
                            action="veto",
                            reason="settlement_sellable_exceeded",
                        )
                    )
                    continue

            notional = intent.quote_notional_decimal()
            max_cap = self.config.max_single_order_amount
            if max_cap is not None:
                max_order = decimal_from_number(max_cap)
                if notional > max_order:
                    decisions.append(
                        RiskDecision(
                            intent_id=intent.intent_id,
                            action="veto",
                            reason="max_single_order_amount exceeded",
                        )
                    )
                    continue

            if intent.action == "buy":
                existing = position_by_symbol.get(intent.symbol)
                existing_notional = decimal_from_number(0)
                if existing is not None:
                    q = floor_whole_share_count(float(existing.quantity))
                    existing_notional = decimal_from_number(q) * decimal_from_number(intent.price_reference)

                max_symbol_notional = account_snapshot.equity * decimal_from_number(
                    self.config.max_position_ratio
                )
                if existing_notional + notional > max_symbol_notional:
                    decisions.append(
                        RiskDecision(
                            intent_id=intent.intent_id,
                            action="veto",
                            reason="max_position_ratio exceeded",
                        )
                    )
                    continue

            decisions.append(RiskDecision(intent_id=intent.intent_id, action="pass"))

        return decisions


class PassThroughRiskEngine:
    """Always approves intents; sizing and direction are solely from the review agent."""

    def evaluate(
        self,
        intents,
        account_snapshot,
        positions,
        *,
        cycle_state=None,
        settlement_mode: SettlementMode = "t0",
    ) -> List[RiskDecision]:
        del account_snapshot, positions, cycle_state, settlement_mode
        return [RiskDecision(intent_id=intent.intent_id, action="pass") for intent in intents]
