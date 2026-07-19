"""Protected system ontology for the trading knowledge graph.

The current graph projection relies on a controlled vocabulary split between
deterministic and LLM extraction pipelines. This module exposes that vocabulary
as one immutable Schema contract for API clients and future custom-schema
validation. System definitions are deliberately protected: they may only
change through code and database migrations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class EntityTypeDefinition:
    """One protected entity type in the system namespace."""

    key: str
    label: str
    #: 该类型在图谱可视化里的填充色（``#rrggbb``）。前端据此着色；自定义
    #: 类型把同名字段存进 ``definition_json``，经 ``get_schema`` 一并下发。
    color: str | None = None
    parent_key: str | None = None
    protected: bool = True


@dataclass(frozen=True)
class RelationTypeDefinition:
    """One directed relation and its permitted endpoint types."""

    key: str
    label: str
    source_type: str
    target_type: str
    symmetric: bool = False
    transitive: bool = False
    inverse_key: str | None = None
    protected: bool = True


@dataclass(frozen=True)
class PropertyDefinition:
    """A typed property available on an entity or relation."""

    key: str
    label: str
    owner_kind: str
    value_type: str
    owner_key: str | None = None
    required: bool = False
    multiple: bool = False
    constraints: dict[str, Any] | None = None
    protected: bool = True


@dataclass(frozen=True)
class KnowledgeGraphSchema:
    """Immutable versioned Schema payload exposed to graph clients."""

    namespace: str
    version: int
    entity_types: tuple[EntityTypeDefinition, ...]
    relation_types: tuple[RelationTypeDefinition, ...]
    property_definitions: tuple[PropertyDefinition, ...]

    def to_payload(self) -> dict[str, Any]:
        """Return a stable JSON-serialisable representation."""

        return {
            "namespace": self.namespace,
            "version": self.version,
            "entity_types": [asdict(item) for item in self.entity_types],
            "relation_types": [asdict(item) for item in self.relation_types],
            "property_definitions": [
                asdict(item) for item in self.property_definitions
            ],
        }


SYSTEM_KNOWLEDGE_GRAPH_SCHEMA = KnowledgeGraphSchema(
    namespace="system",
    version=1,
    entity_types=(
        EntityTypeDefinition("cycle", "情绪周期", color="#3b6fd4"),
        EntityTypeDefinition("playbook", "战法", color="#6b7f2e"),
        EntityTypeDefinition("role", "个股角色", color="#2f8f6b"),
        EntityTypeDefinition("signal", "决策信号", color="#7b5fc0"),
        EntityTypeDefinition("symbol", "股票", color="#b26a1f"),
        EntityTypeDefinition("theme", "题材", color="#b8508f"),
    ),
    relation_types=(
        RelationTypeDefinition(
            "belongs_to_theme",
            "属于题材",
            "symbol",
            "theme",
        ),
        RelationTypeDefinition("has_role", "担任角色", "symbol", "role"),
        RelationTypeDefinition("leads_theme", "引领题材", "symbol", "theme"),
        RelationTypeDefinition(
            "linked_with",
            "个股联动",
            "symbol",
            "symbol",
            symmetric=True,
        ),
        RelationTypeDefinition("observed_in", "活跃于", "theme", "cycle"),
        RelationTypeDefinition("signals", "信号指向", "signal", "symbol"),
        RelationTypeDefinition("traded_in", "交易发生于", "symbol", "cycle"),
        RelationTypeDefinition(
            "uses_playbook",
            "使用战法",
            "symbol",
            "playbook",
        ),
    ),
    property_definitions=(
        PropertyDefinition(
            "display_name",
            "显示名称",
            "entity_type",
            "string",
        ),
        PropertyDefinition("attrs", "扩展属性", "entity_type", "json"),
        PropertyDefinition(
            "fact",
            "事实描述",
            "relation_type",
            "string",
            required=True,
        ),
        PropertyDefinition(
            "confidence",
            "置信度",
            "relation_type",
            "number",
            constraints={"minimum": 0, "maximum": 1},
        ),
        PropertyDefinition("attrs", "关系属性", "relation_type", "json"),
    ),
)


def system_schema_payload() -> dict[str, Any]:
    """Return the protected system Schema for API and tool consumers."""

    return SYSTEM_KNOWLEDGE_GRAPH_SCHEMA.to_payload()


__all__ = [
    "EntityTypeDefinition",
    "KnowledgeGraphSchema",
    "PropertyDefinition",
    "RelationTypeDefinition",
    "SYSTEM_KNOWLEDGE_GRAPH_SCHEMA",
    "system_schema_payload",
]
