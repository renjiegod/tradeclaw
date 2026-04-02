from __future__ import annotations

from tradeclaw.domain.models import ValidationResult


class OrderIntentValidator:
    def validate(self, intent) -> ValidationResult:
        has_quantity = intent.quantity is not None
        has_amount = intent.amount is not None

        if has_quantity and has_amount:
            return ValidationResult(ok=False, error="quantity and amount are mutually exclusive")
        if not has_quantity and not has_amount:
            return ValidationResult(ok=False, error="either quantity or amount is required")

        if has_quantity and intent.quantity <= 0:
            return ValidationResult(ok=False, error="quantity must be positive")
        if has_amount and intent.amount <= 0:
            return ValidationResult(ok=False, error="amount must be positive")

        if intent.side not in {"buy", "sell"}:
            return ValidationResult(ok=False, error="side must be buy or sell")
        if intent.price_reference <= 0:
            return ValidationResult(ok=False, error="price reference must be positive")

        return ValidationResult(ok=True)
