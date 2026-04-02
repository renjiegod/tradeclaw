from __future__ import annotations

from dataclasses import dataclass
from typing import List

from tradeclaw.domain.models import RiskDecision


@dataclass
class RiskConfig:
    max_single_order_amount: float
    max_position_ratio: float


class BasicRiskEngine:
    def __init__(self, config: RiskConfig):
        self.config = config

    def evaluate(self, intents, account_snapshot, positions) -> List[RiskDecision]:
        decisions: List[RiskDecision] = []

        position_by_symbol = {position.symbol: position for position in positions}

        for intent in intents:
            notional = intent.amount if intent.amount is not None else (intent.quantity or 0.0) * intent.price_reference

            if notional > self.config.max_single_order_amount:
                decisions.append(
                    RiskDecision(
                        intent_id=intent.intent_id,
                        action="veto",
                        reason="max_single_order_amount exceeded",
                    )
                )
                continue

            if intent.side == "buy":
                existing = position_by_symbol.get(intent.symbol)
                existing_notional = 0.0
                if existing is not None:
                    existing_notional = existing.quantity * intent.price_reference

                max_symbol_notional = account_snapshot.equity * self.config.max_position_ratio
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
