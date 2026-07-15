from __future__ import annotations

from doyoutrade.core.models import ValidationResult


class OrderIntentValidator:
    def validate(self, intent) -> ValidationResult:
        if intent.amount is None:
            return ValidationResult(ok=False, error="amount is required")
        if intent.amount <= 0:
            return ValidationResult(ok=False, error="amount must be positive")

        if intent.action not in {"buy", "sell"}:
            return ValidationResult(ok=False, error="action must be buy or sell")
        if intent.price_reference <= 0:
            return ValidationResult(ok=False, error="price reference must be positive")

        return ValidationResult(ok=True)
