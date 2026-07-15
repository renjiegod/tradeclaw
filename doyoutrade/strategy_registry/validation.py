from __future__ import annotations

from doyoutrade.strategy_registry.models import StrategyDefinitionCreate, StrategyDefinitionValidationResult


class InvalidStrategyDefinitionError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        validation_errors: tuple[dict, ...] = (),
        repair_hints: tuple[str, ...] = (),
    ) -> None:
        rendered = f"{error_code}: {message}" if error_code else message
        super().__init__(rendered)
        self.error_code = error_code
        self.validation_errors = validation_errors
        self.repair_hints = repair_hints


def validate_strategy_definition(payload: StrategyDefinitionCreate) -> StrategyDefinitionValidationResult:
    errors: list[str] = []
    if not payload.definition_id.strip():
        errors.append("definition_id is required")
    if not payload.name.strip():
        errors.append("name is required")
    if not payload.api_version.strip():
        errors.append("api_version is required")
    return StrategyDefinitionValidationResult(errors=tuple(errors))


def raise_for_invalid_definition(payload: StrategyDefinitionCreate) -> None:
    result = validate_strategy_definition(payload)
    if result.is_valid:
        return
    raise InvalidStrategyDefinitionError("; ".join(result.errors))


__all__ = [
    "InvalidStrategyDefinitionError",
    "raise_for_invalid_definition",
    "validate_strategy_definition",
]
