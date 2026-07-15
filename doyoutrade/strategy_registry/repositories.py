from __future__ import annotations

from typing import Protocol

from doyoutrade.persistence import StrategyDefinitionSnapshot


class StrategyDefinitionRepository(Protocol):
    async def create_definition(self, **kwargs) -> StrategyDefinitionSnapshot:
        ...

    async def get_definition(self, definition_id: str) -> StrategyDefinitionSnapshot:
        ...

    async def update_definition(self, definition_id: str, **kwargs) -> StrategyDefinitionSnapshot:
        ...

    async def delete_definition(self, definition_id: str) -> None:
        ...

    async def delete_definitions(self, definition_ids: list[str]) -> None:
        ...


__all__ = ["StrategyDefinitionRepository"]
