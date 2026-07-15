from __future__ import annotations

from doyoutrade.strategy_registry.models import RegisteredStrategyDefinition, StrategyDefinitionCreate
from doyoutrade.strategy_registry.repositories import StrategyDefinitionRepository
from doyoutrade.strategy_registry.validation import (
    InvalidStrategyDefinitionError,
    raise_for_invalid_definition,
)
from doyoutrade.strategy_runtime.compiler import StrategyCompiler


class StrategyRegistryService:
    """Metadata-only registry service.

    Source compilation is owned by the authoring lifecycle
    (``open_strategy_authoring`` / ``compile_strategy_draft`` /
    ``finalize_strategy_authoring``).  This service stores and retrieves
    definition metadata; it does NOT compile source code on write.
    """

    def __init__(
        self,
        definition_repository: StrategyDefinitionRepository,
        *,
        compiler: StrategyCompiler | None = None,
    ) -> None:
        self._definition_repository = definition_repository
        # Compiler retained for callers that still pass source_code via
        # register_definition (CLI bootstrap path); will be removed once
        # those callers are fully migrated in a follow-up task.
        self._compiler = compiler or StrategyCompiler()

    async def create_definition(
        self,
        payload: StrategyDefinitionCreate,
    ):
        """Persist a new strategy definition (metadata only).

        The compile gate that previously lived here has been moved to the
        authoring lifecycle tools.  ``payload.source_code`` and
        ``payload.class_name`` are accepted for backward compatibility but
        are intentionally ignored — the DB columns no longer exist.
        """
        raise_for_invalid_definition(payload)
        definition = await self._definition_repository.create_definition(
            definition_id=payload.definition_id,
            name=payload.name,
            api_version=payload.api_version,
            input_contract_json=payload.input_contract,
            parameter_schema_json=payload.parameter_schema,
            default_parameters_json=payload.default_parameters,
            capabilities_json=payload.capabilities,
            provenance_json=payload.provenance,
            code_hash=payload.code_hash or "",
            generation_prompt=payload.generation_prompt,
            generation_model=payload.generation_model,
            generation_metadata_json=payload.generation_metadata,
            status=payload.status,
        )
        return definition

    async def update_definition(
        self,
        definition_id: str,
        *,
        name: str | None = None,
        source_code: str | None = None,
        class_name: str | None = None,
        api_version: str | None = None,
        input_contract: dict | None = None,
        parameter_schema: dict | None = None,
        default_parameters: dict | None = None,
        capabilities: dict | None = None,
        provenance: dict | None = None,
        generation_prompt: str | None = None,
        generation_model: str | None = None,
        generation_metadata: dict | None = None,
        status: str | None = None,
        code_hash: str | None = None,
    ):
        """Update definition metadata (patch semantics).

        ``source_code`` and ``class_name`` kwargs are accepted for backward
        compatibility with callers that have not yet been migrated; they
        are ignored since the DB columns no longer exist.
        """
        existing = await self._definition_repository.get_definition(definition_id)
        update_kwargs: dict = {}
        if name is not None:
            update_kwargs["name"] = name
        if api_version is not None:
            update_kwargs["api_version"] = api_version
        if input_contract is not None:
            update_kwargs["input_contract_json"] = input_contract
        if parameter_schema is not None:
            update_kwargs["parameter_schema_json"] = parameter_schema
        if default_parameters is not None:
            update_kwargs["default_parameters_json"] = default_parameters
        if capabilities is not None:
            update_kwargs["capabilities_json"] = capabilities
        if provenance is not None:
            update_kwargs["provenance_json"] = provenance
        if generation_prompt is not None:
            update_kwargs["generation_prompt"] = generation_prompt
        if generation_model is not None:
            update_kwargs["generation_model"] = generation_model
        if generation_metadata is not None:
            update_kwargs["generation_metadata_json"] = generation_metadata
        if status is not None:
            update_kwargs["status"] = status
        if code_hash is not None:
            update_kwargs["code_hash"] = code_hash

        if not update_kwargs:
            # Nothing changed — return the existing snapshot unchanged.
            return existing

        return await self._definition_repository.update_definition(
            definition_id,
            **update_kwargs,
        )

    async def delete_definition(self, definition_id: str) -> None:
        await self._definition_repository.delete_definition(definition_id)

    async def delete_definitions(self, definition_ids: list[str]) -> None:
        await self._definition_repository.delete_definitions(definition_ids)

    async def register_definition(
        self,
        *,
        definition_id: str,
        name: str,
        class_name: str,
        source_code: str,
        api_version: str,
        input_contract_json: dict | None = None,
        parameter_schema_json: dict | None = None,
        default_parameters_json: dict | None = None,
        capabilities_json: dict | None = None,
        provenance_json: dict | None = None,
        generation_prompt: str = "",
        generation_model: str = "",
        generation_metadata_json: dict | None = None,
        status: str = "active",
    ) -> RegisteredStrategyDefinition:
        """Legacy registration path used by CLI bootstrap.

        Compiles the strategy from source (the old path) so that existing
        bootstrap callers keep working.  New callers should use the
        authoring lifecycle instead.
        """
        compile_result = self._compiler.validate_definition(source_code, class_name)
        if not compile_result.success or compile_result.artifact is None:
            raise InvalidStrategyDefinitionError(
                "; ".join(compile_result.errors),
                error_code=compile_result.error_code,
                validation_errors=compile_result.validation_errors,
                repair_hints=compile_result.repair_hints,
            )

        payload = StrategyDefinitionCreate(
            definition_id=definition_id,
            name=name,
            api_version=api_version,
            input_contract=input_contract_json,
            parameter_schema=parameter_schema_json,
            default_parameters=default_parameters_json,
            capabilities=capabilities_json,
            provenance=provenance_json,
            generation_prompt=generation_prompt,
            generation_model=generation_model,
            generation_metadata=generation_metadata_json,
            status=status,
            code_hash=compile_result.code_hash,
        )
        definition = await self.create_definition(payload)
        return RegisteredStrategyDefinition(
            definition=definition,
            compiled=compile_result.artifact,
        )


__all__ = ["StrategyRegistryService"]
