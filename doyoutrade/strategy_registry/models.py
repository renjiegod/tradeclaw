from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from doyoutrade.persistence import StrategyDefinitionSnapshot
from doyoutrade.strategy_runtime.compiler import CompiledStrategyArtifact


@dataclass(frozen=True)
class StrategyDefinitionCreate:
    definition_id: str
    name: str
    api_version: str
    input_contract: dict[str, Any] | None = None
    parameter_schema: dict[str, Any] | None = None
    default_parameters: dict[str, Any] | None = None
    capabilities: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    generation_prompt: str = ""
    generation_model: str = ""
    generation_metadata: dict[str, Any] | None = None
    status: str = "active"
    # SHA-256 of the compiled artifact, set by finalize_strategy_authoring;
    # empty string until first authoring lifecycle run.
    code_hash: str = ""
    # Retained for backward compatibility with tests that still pass these;
    # ignored by the service and repository since the DB columns were removed
    # in the strategy-as-files refactor (Task 2).
    class_name: str = ""
    source_code: str = ""


@dataclass(frozen=True)
class RegisteredStrategyDefinition:
    definition: StrategyDefinitionSnapshot
    compiled: CompiledStrategyArtifact


@dataclass(frozen=True)
class StrategyDefinitionValidationResult:
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_valid(self) -> bool:
        return not self.errors


__all__ = [
    "RegisteredStrategyDefinition",
    "StrategyDefinitionCreate",
    "StrategyDefinitionValidationResult",
]
