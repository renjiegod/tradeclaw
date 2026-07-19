"""Audited manual knowledge-graph commands and Agent proposal approval.

Local users may apply validated changes immediately. Agents can only create an
immutable pending changeset; a human must approve the exact proposal hash while
the graph is still at its base revision. Every applied change and approval is
committed atomically with the manual graph edge it creates.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from doyoutrade.knowledge.lifecycle import (
    LIFECYCLE_OPERATION_MODELS,
    LifecycleValidationError,
    apply_lifecycle_operation,
    conflict_payload,
    evidence_payload,
    layout_payload,
    lineage_payload,
    normalize_lifecycle_operation,
    resolve_redirect,
)
from doyoutrade.knowledge.schema import (
    SYSTEM_KNOWLEDGE_GRAPH_SCHEMA,
    system_schema_payload,
)
from doyoutrade.persistence.models import (
    KnowledgeGraphApprovalDecisionRecord,
    KnowledgeGraphCanvasLayoutRecord,
    KnowledgeGraphChangeOperationRecord,
    KnowledgeGraphChangeSetRecord,
    KnowledgeGraphConflictRecord,
    KnowledgeGraphEdgeRecord,
    KnowledgeGraphEntityLineageRecord,
    KnowledgeGraphEvidenceRecord,
    KnowledgeGraphNodeRecord,
    KnowledgeGraphRevisionRecord,
    KnowledgeGraphSchemaItemRecord,
    KnowledgeGraphStateRecord,
)


class GraphEditError(ValueError):
    """Base class for structured graph-editing errors."""

    error_code = "graph_edit_failed"


class GraphSchemaValidationError(GraphEditError):
    """An operation violates the protected graph Schema."""

    error_code = "graph_schema_validation_error"


class GraphRevisionConflict(GraphEditError):
    """The proposal no longer targets the current graph revision."""

    error_code = "graph_revision_conflict"


class GraphProposalMismatch(GraphEditError):
    """Approval does not match the immutable proposal hash."""

    error_code = "graph_proposal_mismatch"


class GraphChangeSetNotFound(GraphEditError):
    """The requested changeset does not exist."""

    error_code = "graph_change_set_not_found"


class _EntityRef(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=1024)


class _CreateRelationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["create_relation"]
    source: _EntityRef
    relation: str = Field(min_length=1, max_length=64)
    target: _EntityRef
    fact: str = Field(min_length=1, max_length=10000)
    attrs: dict[str, Any] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    valid_at: datetime | None = None
    invalid_at: datetime | None = None


class _ReviseRelationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["revise_relation"]
    edge_id: str = Field(min_length=1, max_length=64)
    fact: str | None = Field(default=None, min_length=1, max_length=10000)
    attrs: dict[str, Any] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    valid_at: datetime | None = None
    invalid_at: datetime | None = None


class _RetractRelationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["retract_relation"]
    edge_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=2000)


class _EntityTypeSchemaDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=255)
    parent_key: str | None = Field(default=None, max_length=128)


class _RelationTypeSchemaDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=255)
    source_type: str = Field(min_length=1, max_length=128)
    target_type: str = Field(min_length=1, max_length=128)
    symmetric: bool = False
    transitive: bool = False
    inverse_key: str | None = Field(default=None, max_length=128)


class _PropertySchemaDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=255)
    owner_kind: Literal["entity_type", "relation_type"]
    owner_key: str = Field(min_length=1, max_length=128)
    value_type: Literal[
        "string",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "enum",
        "uri",
        "json",
        "entity_ref",
    ]
    required: bool = False
    multiple: bool = False
    constraints: dict[str, Any] | None = None


class _UpsertSchemaItemOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["upsert_schema_item"]
    kind: Literal["entity_type", "relation_type", "property"]
    key: str = Field(pattern=r"^custom\.[a-z][a-z0-9_.-]*$", max_length=128)
    expected_version: int = Field(ge=0)
    definition: dict[str, Any]


class _DeprecateSchemaItemOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["deprecate_schema_item"]
    kind: Literal["entity_type", "relation_type", "property"]
    key: str = Field(pattern=r"^custom\.[a-z][a-z0-9_.-]*$", max_length=128)
    expected_version: int = Field(ge=1)


class KnowledgeGraphCommandService:
    """Apply local changes and persist Agent proposals with one-time approval."""

    _STATE_KEY = "default"
    _MAX_OPERATIONS = 100

    def __init__(self, session_factory: Any):
        self._session_factory = session_factory
        self._relations = {
            item.key: item for item in SYSTEM_KNOWLEDGE_GRAPH_SCHEMA.relation_types
        }

    def _normalize_operations(
        self,
        operations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(operations, list) or not operations:
            raise GraphSchemaValidationError("operations must be a non-empty list")
        if len(operations) > self._MAX_OPERATIONS:
            raise GraphSchemaValidationError(
                f"operations exceeds limit {self._MAX_OPERATIONS}"
            )

        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(operations):
            if not isinstance(raw, dict):
                raise GraphSchemaValidationError(
                    f"operations[{index}] must be an object"
                )
            operation_type = raw.get("op")
            try:
                if operation_type in LIFECYCLE_OPERATION_MODELS:
                    try:
                        normalized.append(
                            normalize_lifecycle_operation(raw, index=index)
                        )
                    except LifecycleValidationError as exc:
                        raise GraphSchemaValidationError(str(exc)) from exc
                    continue
                if operation_type == "create_relation":
                    operation = _CreateRelationOperation.model_validate(raw)
                elif operation_type == "revise_relation":
                    operation = _ReviseRelationOperation.model_validate(raw)
                    if not (operation.model_fields_set - {"op", "edge_id"}):
                        raise GraphSchemaValidationError(
                            f"operations[{index}] revise_relation has no changes"
                        )
                elif operation_type == "retract_relation":
                    operation = _RetractRelationOperation.model_validate(raw)
                elif operation_type == "upsert_schema_item":
                    operation = _UpsertSchemaItemOperation.model_validate(raw)
                    definition_models = {
                        "entity_type": _EntityTypeSchemaDefinition,
                        "relation_type": _RelationTypeSchemaDefinition,
                        "property": _PropertySchemaDefinition,
                    }
                    definition = definition_models[operation.kind].model_validate(
                        operation.definition
                    )
                    operation.definition = definition.model_dump(mode="json")
                elif operation_type == "deprecate_schema_item":
                    operation = _DeprecateSchemaItemOperation.model_validate(raw)
                else:
                    raise GraphSchemaValidationError(
                        f"operations[{index}] uses unknown operation {operation_type!r}"
                    )
            except ValidationError as exc:
                raise GraphSchemaValidationError(
                    f"operations[{index}] is invalid: {exc.errors(include_url=False)}"
                ) from exc
            if isinstance(operation, _CreateRelationOperation):
                relation = self._relations.get(operation.relation)
                if relation is None and not operation.relation.startswith("custom."):
                    raise GraphSchemaValidationError(
                        f"operations[{index}] uses unknown relation "
                        f"{operation.relation!r}"
                    )
                if relation is not None and (
                    operation.source.type != relation.source_type
                    or operation.target.type != relation.target_type
                ):
                    raise GraphSchemaValidationError(
                        f"operations[{index}] relation {relation.key!r} requires "
                        f"{relation.source_type}->{relation.target_type}, got "
                        f"{operation.source.type}->{operation.target.type}"
                    )
            invalid_at = getattr(operation, "invalid_at", None)
            valid_at = getattr(operation, "valid_at", None)
            if invalid_at and valid_at:
                if invalid_at < valid_at:
                    raise GraphSchemaValidationError(
                        f"operations[{index}] invalid_at precedes valid_at"
                    )
            normalized.append(operation.model_dump(mode="json", exclude_unset=True))
        return normalized

    async def _validate_operations_against_schema(
        self,
        session: Any,
        operations: list[dict[str, Any]],
    ) -> None:
        result = await session.execute(select(KnowledgeGraphSchemaItemRecord))
        items: dict[tuple[str, str], dict[str, Any]] = {
            (record.kind, record.key): {
                "definition": dict(record.definition_json),
                "status": record.status,
                "version": record.version,
            }
            for record in result.scalars().all()
        }
        system_entities = {
            item.key for item in SYSTEM_KNOWLEDGE_GRAPH_SCHEMA.entity_types
        }
        system_relations = {
            item.key: {
                "source_type": item.source_type,
                "target_type": item.target_type,
            }
            for item in SYSTEM_KNOWLEDGE_GRAPH_SCHEMA.relation_types
        }

        def _active_entity(key: str | None) -> bool:
            if key is None:
                return True
            if key in system_entities:
                return True
            item = items.get(("entity_type", key))
            return item is not None and item["status"] == "active"

        def _active_relation(key: str | None) -> bool:
            if key in system_relations:
                return True
            item = items.get(("relation_type", str(key)))
            return item is not None and item["status"] == "active"

        def _check_entity_cycle(key: str) -> None:
            seen = {key}
            current = items.get(("entity_type", key))
            parent = (
                current["definition"].get("parent_key") if current is not None else None
            )
            while parent and parent.startswith("custom."):
                if parent in seen:
                    raise GraphSchemaValidationError(
                        f"entity inheritance cycle detected at {parent!r}"
                    )
                seen.add(parent)
                current = items.get(("entity_type", parent))
                if current is None or current["status"] != "active":
                    break
                parent = current["definition"].get("parent_key")

        for index, operation in enumerate(operations):
            operation_type = operation["op"]
            if operation_type == "upsert_schema_item":
                key = operation["key"]
                kind = operation["kind"]
                existing = items.get((kind, key))
                actual_version = existing["version"] if existing else 0
                if actual_version != operation["expected_version"]:
                    raise GraphRevisionConflict(
                        f"schema item {kind}/{key} expected version "
                        f"{operation['expected_version']}, current version is "
                        f"{actual_version}"
                    )
                definition = operation["definition"]
                if kind == "entity_type":
                    parent = definition.get("parent_key")
                    if parent and not _active_entity(parent):
                        raise GraphSchemaValidationError(
                            f"operations[{index}] parent entity type "
                            f"{parent!r} does not exist"
                        )
                elif kind == "relation_type":
                    for endpoint in (
                        definition["source_type"],
                        definition["target_type"],
                    ):
                        if not _active_entity(endpoint):
                            raise GraphSchemaValidationError(
                                f"operations[{index}] endpoint entity type "
                                f"{endpoint!r} does not exist"
                            )
                    inverse = definition.get("inverse_key")
                    if inverse and not _active_relation(inverse):
                        raise GraphSchemaValidationError(
                            f"operations[{index}] inverse relation "
                            f"{inverse!r} does not exist"
                        )
                else:
                    owner_kind = definition["owner_kind"]
                    owner_key = definition["owner_key"]
                    owner_exists = (
                        _active_entity(owner_key)
                        if owner_kind == "entity_type"
                        else _active_relation(owner_key)
                    )
                    if not owner_exists:
                        raise GraphSchemaValidationError(
                            f"operations[{index}] property owner "
                            f"{owner_kind}/{owner_key} does not exist"
                        )
                items[(kind, key)] = {
                    "definition": definition,
                    "status": "active",
                    "version": actual_version + 1,
                }
                if kind == "entity_type":
                    _check_entity_cycle(key)
            elif operation_type == "deprecate_schema_item":
                key = operation["key"]
                kind = operation["kind"]
                existing = items.get((kind, key))
                if existing is None:
                    raise GraphSchemaValidationError(
                        f"schema item {kind}/{key} does not exist"
                    )
                if existing["version"] != operation["expected_version"]:
                    raise GraphRevisionConflict(
                        f"schema item {kind}/{key} expected version "
                        f"{operation['expected_version']}, current version is "
                        f"{existing['version']}"
                    )
                if kind == "entity_type":
                    blockers = [
                        relation_key
                        for (item_kind, relation_key), item in items.items()
                        if item_kind == "relation_type"
                        and item["status"] == "active"
                        and key
                        in {
                            item["definition"].get("source_type"),
                            item["definition"].get("target_type"),
                        }
                    ]
                    if blockers:
                        raise GraphSchemaValidationError(
                            f"entity type {key!r} is referenced by active "
                            f"relations: {sorted(blockers)}"
                        )
                items[(kind, key)] = {
                    **existing,
                    "status": "deprecated",
                    "version": existing["version"] + 1,
                }
            elif operation_type == "create_relation":
                relation_key = operation["relation"]
                if relation_key in system_relations:
                    relation = system_relations[relation_key]
                else:
                    custom = items.get(("relation_type", relation_key))
                    if custom is None or custom["status"] != "active":
                        raise GraphSchemaValidationError(
                            f"operations[{index}] relation "
                            f"{relation_key!r} is missing or deprecated"
                        )
                    relation = custom["definition"]
                if (
                    operation["source"]["type"] != relation["source_type"]
                    or operation["target"]["type"] != relation["target_type"]
                ):
                    raise GraphSchemaValidationError(
                        f"operations[{index}] relation {relation_key!r} requires "
                        f"{relation['source_type']}->{relation['target_type']}"
                    )

    @staticmethod
    def _proposal_hash(
        operations: list[dict[str, Any]],
        *,
        summary: str,
        base_revision: int,
    ) -> str:
        body = json.dumps(
            {
                "base_revision": base_revision,
                "operations": operations,
                "summary": summary,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    async def _locked_state(self, session: Any, *, now: datetime):
        result = await session.execute(
            select(KnowledgeGraphStateRecord)
            .where(KnowledgeGraphStateRecord.state_key == self._STATE_KEY)
            .with_for_update()
        )
        state = result.scalar_one_or_none()
        if state is None:
            state = KnowledgeGraphStateRecord(
                state_key=self._STATE_KEY,
                head_revision=0,
                updated_at=now,
            )
            session.add(state)
            await session.flush()
        return state

    @staticmethod
    async def _load_or_create_node(
        session: Any,
        entity: dict[str, Any],
        *,
        now: datetime,
    ) -> KnowledgeGraphNodeRecord:
        result = await session.execute(
            select(KnowledgeGraphNodeRecord).where(
                KnowledgeGraphNodeRecord.node_type == entity["type"],
                KnowledgeGraphNodeRecord.name == entity["name"],
            )
        )
        node = result.scalar_one_or_none()
        if node is None:
            node = KnowledgeGraphNodeRecord(
                id=f"kgn-{uuid.uuid4().hex[:12]}",
                node_type=entity["type"],
                name=entity["name"],
                display_name=entity.get("display_name"),
                attrs=None,
                status="active",
                retired_at=None,
                redirect_to_id=None,
                created_at=now,
                updated_at=now,
            )
            session.add(node)
            await session.flush()
            return node
        node = await resolve_redirect(session, node)
        if node.status != "active":
            raise GraphSchemaValidationError(
                f"entity {entity['type']}/{entity['name']} is {node.status!r}"
            )
        if entity.get("display_name") and not node.display_name:
            node.display_name = entity["display_name"]
            node.updated_at = now
        return node

    @staticmethod
    async def _manual_edge(
        session: Any,
        payload: dict[str, Any],
        *,
        require_active: bool = True,
    ) -> KnowledgeGraphEdgeRecord:
        edge: KnowledgeGraphEdgeRecord | None = None
        edge_id = payload.get("edge_id")
        if isinstance(edge_id, str) and edge_id:
            edge = await session.get(KnowledgeGraphEdgeRecord, edge_id)
        dedupe_key = payload.get("dedupe_key")
        if (
            (edge is None or (require_active and edge.expired_at is not None))
            and isinstance(dedupe_key, str)
            and dedupe_key
        ):
            result = await session.execute(
                select(KnowledgeGraphEdgeRecord)
                .where(
                    KnowledgeGraphEdgeRecord.dedupe_key == dedupe_key,
                    KnowledgeGraphEdgeRecord.expired_at.is_(None),
                )
                .order_by(KnowledgeGraphEdgeRecord.created_at.desc())
            )
            edge = result.scalars().first()
        if edge is None:
            raise GraphSchemaValidationError(
                f"manual relation not found: edge_id={edge_id!r}"
            )
        if edge.provenance != "manual":
            raise GraphSchemaValidationError(
                "automatic and LLM relations are read-only; correct the source "
                "or create a manual override instead"
            )
        if require_active and edge.expired_at is not None:
            raise GraphRevisionConflict(
                f"manual relation {edge.id!r} is no longer active"
            )
        return edge

    @staticmethod
    async def _edge_snapshot(
        session: Any,
        edge: KnowledgeGraphEdgeRecord,
    ) -> dict[str, Any]:
        source = await session.get(KnowledgeGraphNodeRecord, edge.src_id)
        target = await session.get(KnowledgeGraphNodeRecord, edge.dst_id)
        if source is None or target is None:
            raise GraphSchemaValidationError(
                f"relation {edge.id!r} has a missing endpoint"
            )

        def _iso(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "edge_id": edge.id,
            "source": {
                "type": source.node_type,
                "name": source.name,
                "display_name": source.display_name,
            },
            "relation": edge.relation,
            "target": {
                "type": target.node_type,
                "name": target.name,
                "display_name": target.display_name,
            },
            "fact": edge.fact,
            "attrs": edge.attrs,
            "confidence": edge.confidence,
            "valid_at": _iso(edge.valid_at),
            "invalid_at": _iso(edge.invalid_at),
            "dedupe_key": edge.dedupe_key,
            "source_key": edge.source_key,
            "source_ref": edge.source_ref,
        }

    async def _insert_manual_edge(
        self,
        session: Any,
        payload: dict[str, Any],
        *,
        change_set_id: str,
        position: int,
        now: datetime,
    ) -> KnowledgeGraphEdgeRecord:
        source = await self._load_or_create_node(
            session,
            payload["source"],
            now=now,
        )
        target = await self._load_or_create_node(
            session,
            payload["target"],
            now=now,
        )
        dedupe_key = payload.get("dedupe_key") or (f"manual|{change_set_id}|{position}")
        active_result = await session.execute(
            select(KnowledgeGraphEdgeRecord).where(
                KnowledgeGraphEdgeRecord.dedupe_key == dedupe_key,
                KnowledgeGraphEdgeRecord.expired_at.is_(None),
            )
        )
        active = active_result.scalars().first()
        if active is not None:
            if active.provenance != "manual":
                raise GraphSchemaValidationError(
                    f"dedupe key {dedupe_key!r} belongs to an automatic fact"
                )
            active.expired_at = now
            await session.flush()

        def _datetime(value: Any) -> datetime | None:
            if isinstance(value, datetime):
                return value
            if isinstance(value, str) and value:
                return datetime.fromisoformat(value)
            return None

        source_ref = f"manual:change-set/{change_set_id}"
        edge = KnowledgeGraphEdgeRecord(
            id=f"kge-{uuid.uuid4().hex[:12]}",
            src_id=source.id,
            dst_id=target.id,
            relation=payload["relation"],
            fact=payload["fact"],
            attrs=payload.get("attrs"),
            dedupe_key=dedupe_key,
            state_key=None,
            provenance="manual",
            confidence=payload.get("confidence"),
            source_key=payload.get("source_key") or source_ref,
            source_ref=source_ref,
            valid_at=_datetime(payload.get("valid_at")),
            invalid_at=_datetime(payload.get("invalid_at")),
            created_at=now,
            expired_at=None,
        )
        session.add(edge)
        await session.flush()
        return edge

    async def _apply_operations(
        self,
        session: Any,
        *,
        change_set: KnowledgeGraphChangeSetRecord,
        operations: list[KnowledgeGraphChangeOperationRecord],
        revision: int,
        now: datetime,
    ) -> list[str]:
        edge_ids: list[str] = []
        for operation_record in operations:
            payload = dict(operation_record.after_json)
            operation_type = operation_record.op_type
            if operation_type in LIFECYCLE_OPERATION_MODELS:
                try:
                    affected_id = await apply_lifecycle_operation(
                        session,
                        operation_record=operation_record,
                        change_set_id=change_set.id,
                        revision=revision,
                        now=now,
                        insert_manual_edge=self._insert_manual_edge,
                        edge_snapshot=self._edge_snapshot,
                    )
                except LifecycleValidationError as exc:
                    raise GraphSchemaValidationError(str(exc)) from exc
                edge_ids.append(affected_id)
                continue
            if operation_type in {
                "upsert_schema_item",
                "deprecate_schema_item",
            }:
                item_result = await session.execute(
                    select(KnowledgeGraphSchemaItemRecord).where(
                        KnowledgeGraphSchemaItemRecord.kind == payload["kind"],
                        KnowledgeGraphSchemaItemRecord.key == payload["key"],
                    )
                )
                item = item_result.scalar_one_or_none()
                if operation_type == "upsert_schema_item":
                    expected_version = payload["expected_version"]
                    actual_version = item.version if item is not None else 0
                    if actual_version != expected_version:
                        raise GraphRevisionConflict(
                            f"schema item {payload['kind']}/{payload['key']} "
                            f"expected version {expected_version}, current "
                            f"version is {actual_version}"
                        )
                    operation_record.before_json = (
                        {
                            "definition": dict(item.definition_json),
                            "status": item.status,
                            "schema_version": item.version,
                        }
                        if item is not None
                        else None
                    )
                    if item is None:
                        item = KnowledgeGraphSchemaItemRecord(
                            id=f"kgsi-{uuid.uuid4().hex[:12]}",
                            namespace="custom",
                            kind=payload["kind"],
                            key=payload["key"],
                            definition_json=payload["definition"],
                            status="active",
                            version=1,
                            created_at=now,
                            updated_at=now,
                        )
                        session.add(item)
                    else:
                        item.definition_json = payload["definition"]
                        item.status = "active"
                        item.version += 1
                        item.updated_at = now
                else:
                    if item is None:
                        raise GraphSchemaValidationError(
                            f"schema item {payload['kind']}/{payload['key']} "
                            "does not exist"
                        )
                    if item.version != payload["expected_version"]:
                        raise GraphRevisionConflict(
                            f"schema item {payload['kind']}/{payload['key']} "
                            f"expected version {payload['expected_version']}, "
                            f"current version is {item.version}"
                        )
                    operation_record.before_json = {
                        "definition": dict(item.definition_json),
                        "status": item.status,
                        "schema_version": item.version,
                    }
                    item.status = "deprecated"
                    item.version += 1
                    item.updated_at = now
                await session.flush()
                operation_record.target_id = item.id
                operation_record.after_json = {
                    **payload,
                    "schema_version": item.version,
                    "status": item.status,
                    "revision": revision,
                }
                continue
            if operation_type == "create_relation":
                operation = _CreateRelationOperation.model_validate(payload)
                edge_payload = operation.model_dump(mode="python")
                edge_payload["dedupe_key"] = payload.get("dedupe_key")
                edge = await self._insert_manual_edge(
                    session,
                    edge_payload,
                    change_set_id=change_set.id,
                    position=operation_record.position,
                    now=now,
                )
            elif operation_type == "revise_relation":
                operation = _ReviseRelationOperation.model_validate(payload)
                edge = await self._manual_edge(session, payload)
                before = await self._edge_snapshot(session, edge)
                operation_record.before_json = before
                changes = operation.model_dump(
                    mode="python",
                    exclude_unset=True,
                    exclude={"op", "edge_id"},
                )
                revised_payload = {**before, **changes}
                edge.expired_at = now
                await session.flush()
                edge = await self._insert_manual_edge(
                    session,
                    revised_payload,
                    change_set_id=change_set.id,
                    position=operation_record.position,
                    now=now,
                )
            elif operation_type == "retract_relation":
                edge = await self._manual_edge(session, payload)
                operation_record.before_json = await self._edge_snapshot(
                    session,
                    edge,
                )
                edge.expired_at = now
                await session.flush()
            elif operation_type == "restore_relation":
                snapshot = payload.get("snapshot")
                if not isinstance(snapshot, dict):
                    raise GraphSchemaValidationError(
                        "restore_relation requires a snapshot"
                    )
                existing_result = await session.execute(
                    select(KnowledgeGraphEdgeRecord).where(
                        KnowledgeGraphEdgeRecord.dedupe_key
                        == snapshot.get("dedupe_key"),
                        KnowledgeGraphEdgeRecord.expired_at.is_(None),
                    )
                )
                existing = existing_result.scalars().first()
                if existing is not None:
                    operation_record.before_json = await self._edge_snapshot(
                        session,
                        existing,
                    )
                    existing.expired_at = now
                    await session.flush()
                edge = await self._insert_manual_edge(
                    session,
                    snapshot,
                    change_set_id=change_set.id,
                    position=operation_record.position,
                    now=now,
                )
            else:
                raise GraphSchemaValidationError(
                    f"unsupported operation type {operation_type!r}"
                )

            if operation_type == "retract_relation":
                operation_record.target_id = edge.id
                operation_record.after_json = {
                    **payload,
                    "dedupe_key": edge.dedupe_key,
                    "revision": revision,
                }
                edge_ids.append(edge.id)
            else:
                operation_record.target_id = edge.id
                operation_record.after_json = {
                    **payload,
                    "edge_id": edge.id,
                    "dedupe_key": edge.dedupe_key,
                    "revision": revision,
                }
                edge_ids.append(edge.id)
        return edge_ids

    @staticmethod
    def _change_set_payload(
        record: KnowledgeGraphChangeSetRecord,
        *,
        edge_ids: list[str] | None = None,
        operations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "id": record.id,
            "status": record.status,
            "actor_type": record.actor_type,
            "actor_id": record.actor_id,
            "base_revision": record.base_revision,
            "revision": record.revision,
            "proposal_hash": record.proposal_hash,
            "summary": record.summary,
            "created_at": record.created_at.isoformat(),
            "applied_at": (
                record.applied_at.isoformat() if record.applied_at else None
            ),
            "edge_ids": list(edge_ids or ()),
        }
        if operations is not None:
            payload["operations"] = operations
        return payload

    @staticmethod
    def _operation_records(
        change_set_id: str,
        operations: list[dict[str, Any]],
    ) -> list[KnowledgeGraphChangeOperationRecord]:
        return [
            KnowledgeGraphChangeOperationRecord(
                id=f"kgop-{uuid.uuid4().hex[:12]}",
                change_set_id=change_set_id,
                position=index,
                op_type=operation["op"],
                target_id=None,
                before_json=None,
                after_json=operation,
            )
            for index, operation in enumerate(operations)
        ]

    async def _commit_local_operations(
        self,
        session: Any,
        *,
        state: KnowledgeGraphStateRecord,
        operations: list[dict[str, Any]],
        summary: str,
        actor_id: str,
        now: datetime,
        reverts_revision: int | None = None,
        replays_revision: int | None = None,
    ) -> dict[str, Any]:
        change_set_id = f"kgcs-{uuid.uuid4().hex[:12]}"
        proposal_hash = self._proposal_hash(
            operations,
            summary=summary,
            base_revision=state.head_revision,
        )
        next_revision = state.head_revision + 1
        change_set = KnowledgeGraphChangeSetRecord(
            id=change_set_id,
            status="applied",
            actor_type="local_user",
            actor_id=actor_id,
            base_revision=state.head_revision,
            revision=next_revision,
            proposal_hash=proposal_hash,
            summary=summary,
            created_at=now,
            applied_at=now,
            rejected_at=None,
        )
        session.add(change_set)
        # Flush parent before child operations so PG FK checks pass
        # (SQLite often skips FK checks and hides this ordering bug).
        await session.flush()
        operation_records = self._operation_records(change_set_id, operations)
        session.add_all(operation_records)
        edge_ids = await self._apply_operations(
            session,
            change_set=change_set,
            operations=operation_records,
            revision=next_revision,
            now=now,
        )
        session.add(
            KnowledgeGraphRevisionRecord(
                revision=next_revision,
                parent_revision=state.head_revision,
                change_set_id=change_set_id,
                reverts_revision=reverts_revision,
                replays_revision=replays_revision,
                created_at=now,
            )
        )
        state.head_revision = next_revision
        state.updated_at = now
        await session.commit()
        return self._change_set_payload(
            change_set,
            edge_ids=edge_ids,
            operations=[record.after_json for record in operation_records],
        )

    async def apply_local_change(
        self,
        operations: list[dict[str, Any]],
        *,
        summary: str,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Validate and immediately apply a local user's audited changeset."""

        normalized = self._normalize_operations(operations)
        summary = str(summary or "").strip()
        async with self._session_factory() as session:
            await self._validate_operations_against_schema(session, normalized)
            state = await self._locked_state(session, now=now)
            if state.head_revision != expected_revision:
                raise GraphRevisionConflict(
                    f"expected revision {expected_revision}, "
                    f"current revision is {state.head_revision}"
                )
            return await self._commit_local_operations(
                session,
                state=state,
                operations=normalized,
                summary=summary,
                actor_id=actor_id,
                now=now,
            )

    async def create_agent_draft(
        self,
        operations: list[dict[str, Any]],
        *,
        summary: str,
        actor_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Persist an immutable Agent proposal without changing the graph."""

        normalized = self._normalize_operations(operations)
        summary = str(summary or "").strip()
        async with self._session_factory() as session:
            await self._validate_operations_against_schema(session, normalized)
            state = await self._locked_state(session, now=now)
            change_set_id = f"kgcs-{uuid.uuid4().hex[:12]}"
            proposal_hash = self._proposal_hash(
                normalized,
                summary=summary,
                base_revision=state.head_revision,
            )
            change_set = KnowledgeGraphChangeSetRecord(
                id=change_set_id,
                status="pending",
                actor_type="agent",
                actor_id=actor_id,
                base_revision=state.head_revision,
                revision=None,
                proposal_hash=proposal_hash,
                summary=summary,
                created_at=now,
                applied_at=None,
                rejected_at=None,
            )
            session.add(change_set)
            # Postgres enforces FKs on flush; without an ORM relationship,
            # child rows (operations) can be INSERTed before the parent
            # change_set. Flush the parent first (same pattern as
            # apply_projection audit rows).
            await session.flush()
            session.add_all(self._operation_records(change_set_id, normalized))
            await session.commit()
            return self._change_set_payload(
                change_set,
                operations=normalized,
            )

    async def approve_draft(
        self,
        change_set_id: str,
        *,
        proposal_hash: str,
        resolver_id: str,
        decision_source: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Approve and atomically apply exactly one pending Agent proposal."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(KnowledgeGraphChangeSetRecord)
                .where(KnowledgeGraphChangeSetRecord.id == change_set_id)
                .with_for_update()
            )
            change_set = result.scalar_one_or_none()
            if change_set is None:
                raise GraphChangeSetNotFound(change_set_id)
            if change_set.status != "pending":
                raise GraphRevisionConflict(
                    f"change set {change_set_id!r} is already {change_set.status!r}"
                )
            if change_set.proposal_hash != proposal_hash:
                raise GraphProposalMismatch(
                    "approval proposal_hash does not match the pending change set"
                )

            state = await self._locked_state(session, now=now)
            if state.head_revision != change_set.base_revision:
                change_set.status = "stale"
                await session.commit()
                raise GraphRevisionConflict(
                    f"draft base revision {change_set.base_revision}, "
                    f"current revision is {state.head_revision}"
                )
            operation_result = await session.execute(
                select(KnowledgeGraphChangeOperationRecord)
                .where(
                    KnowledgeGraphChangeOperationRecord.change_set_id == change_set_id
                )
                .order_by(KnowledgeGraphChangeOperationRecord.position)
            )
            operations = list(operation_result.scalars().all())
            await self._validate_operations_against_schema(
                session,
                [dict(operation.after_json) for operation in operations],
            )
            next_revision = state.head_revision + 1
            edge_ids = await self._apply_operations(
                session,
                change_set=change_set,
                operations=operations,
                revision=next_revision,
                now=now,
            )
            change_set.status = "applied"
            change_set.revision = next_revision
            change_set.applied_at = now
            session.add(
                KnowledgeGraphApprovalDecisionRecord(
                    id=f"kgad-{uuid.uuid4().hex[:12]}",
                    change_set_id=change_set_id,
                    proposal_hash=proposal_hash,
                    decision="approved",
                    resolver_id=resolver_id,
                    decision_source=decision_source,
                    reason="",
                    decided_at=now,
                )
            )
            session.add(
                KnowledgeGraphRevisionRecord(
                    revision=next_revision,
                    parent_revision=state.head_revision,
                    change_set_id=change_set_id,
                    created_at=now,
                )
            )
            state.head_revision = next_revision
            state.updated_at = now
            await session.commit()
            return self._change_set_payload(
                change_set,
                edge_ids=edge_ids,
                operations=[record.after_json for record in operations],
            )

    async def reject_draft(
        self,
        change_set_id: str,
        *,
        proposal_hash: str,
        resolver_id: str,
        decision_source: str,
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Reject one pending Agent proposal without changing the graph."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(KnowledgeGraphChangeSetRecord)
                .where(KnowledgeGraphChangeSetRecord.id == change_set_id)
                .with_for_update()
            )
            change_set = result.scalar_one_or_none()
            if change_set is None:
                raise GraphChangeSetNotFound(change_set_id)
            if change_set.status != "pending":
                raise GraphRevisionConflict(
                    f"change set {change_set_id!r} is already {change_set.status!r}"
                )
            if change_set.proposal_hash != proposal_hash:
                raise GraphProposalMismatch(
                    "rejection proposal_hash does not match the pending change set"
                )
            change_set.status = "rejected"
            change_set.rejected_at = now
            session.add(
                KnowledgeGraphApprovalDecisionRecord(
                    id=f"kgad-{uuid.uuid4().hex[:12]}",
                    change_set_id=change_set_id,
                    proposal_hash=proposal_hash,
                    decision="rejected",
                    resolver_id=resolver_id,
                    decision_source=decision_source,
                    reason=str(reason or "").strip(),
                    decided_at=now,
                )
            )
            await session.commit()
            return self._change_set_payload(change_set)

    @staticmethod
    async def _revision_operations(
        session: Any,
        revision: int,
    ) -> tuple[
        KnowledgeGraphChangeSetRecord,
        list[KnowledgeGraphChangeOperationRecord],
    ]:
        revision_record = await session.get(
            KnowledgeGraphRevisionRecord,
            revision,
        )
        if revision_record is None:
            raise GraphChangeSetNotFound(f"revision {revision}")
        change_set = await session.get(
            KnowledgeGraphChangeSetRecord,
            revision_record.change_set_id,
        )
        if change_set is None:
            raise GraphChangeSetNotFound(revision_record.change_set_id)
        if change_set.actor_type == "system":
            raise GraphSchemaValidationError(
                "automatic source revisions cannot be undone directly; "
                "correct the source or add a manual override"
            )
        result = await session.execute(
            select(KnowledgeGraphChangeOperationRecord)
            .where(KnowledgeGraphChangeOperationRecord.change_set_id == change_set.id)
            .order_by(KnowledgeGraphChangeOperationRecord.position)
        )
        return change_set, list(result.scalars().all())

    async def undo_revision(
        self,
        revision: int,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Create a compensating revision that reverses one manual revision."""

        async with self._session_factory() as session:
            state = await self._locked_state(session, now=now)
            if state.head_revision != expected_revision:
                raise GraphRevisionConflict(
                    f"expected revision {expected_revision}, "
                    f"current revision is {state.head_revision}"
                )
            change_set, operations = await self._revision_operations(
                session,
                revision,
            )
            inverse: list[dict[str, Any]] = []
            for operation in reversed(operations):
                if operation.op_type == "create_relation":
                    edge = await session.get(
                        KnowledgeGraphEdgeRecord,
                        operation.target_id,
                    )
                    if edge is None:
                        raise GraphSchemaValidationError(
                            f"created relation {operation.target_id!r} is missing"
                        )
                    inverse.append(
                        {
                            "op": "retract_relation",
                            "edge_id": edge.id,
                            "dedupe_key": edge.dedupe_key,
                            "reason": f"undo revision {revision}",
                        }
                    )
                elif operation.op_type in {
                    "revise_relation",
                    "retract_relation",
                    "restore_relation",
                }:
                    if operation.before_json is None:
                        inverse.append(
                            {
                                "op": "retract_relation",
                                "edge_id": operation.target_id,
                                "dedupe_key": operation.after_json.get("dedupe_key"),
                                "reason": f"undo revision {revision}",
                            }
                        )
                    else:
                        inverse.append(
                            {
                                "op": "restore_relation",
                                "snapshot": operation.before_json,
                            }
                        )
                elif operation.op_type in {
                    "upsert_schema_item",
                    "deprecate_schema_item",
                }:
                    item = await session.get(
                        KnowledgeGraphSchemaItemRecord,
                        operation.target_id,
                    )
                    if item is None:
                        raise GraphSchemaValidationError(
                            f"schema item {operation.target_id!r} is missing"
                        )
                    if operation.op_type == "upsert_schema_item" and (
                        operation.before_json is None
                    ):
                        inverse.append(
                            {
                                "op": "deprecate_schema_item",
                                "kind": item.kind,
                                "key": item.key,
                                "expected_version": item.version,
                            }
                        )
                    else:
                        before = operation.before_json or {}
                        inverse.append(
                            {
                                "op": "upsert_schema_item",
                                "kind": item.kind,
                                "key": item.key,
                                "expected_version": item.version,
                                "definition": before["definition"],
                            }
                        )
                elif operation.op_type == "create_entity":
                    inverse.append(
                        {
                            "op": "retire_entity",
                            "entity_id": operation.target_id
                            or operation.after_json.get("entity_id"),
                            "reason": f"undo revision {revision}",
                        }
                    )
                elif operation.op_type in {"update_entity", "retire_entity"}:
                    if operation.before_json is None:
                        raise GraphSchemaValidationError(
                            f"{operation.op_type} missing before snapshot"
                        )
                    inverse.append(
                        {
                            "op": "restore_entity",
                            "snapshot": operation.before_json,
                        }
                    )
                elif operation.op_type == "restore_entity":
                    if operation.before_json is None:
                        inverse.append(
                            {
                                "op": "retire_entity",
                                "entity_id": operation.target_id,
                                "reason": f"undo revision {revision}",
                            }
                        )
                    else:
                        inverse.append(
                            {
                                "op": "restore_entity",
                                "snapshot": operation.before_json,
                            }
                        )
                elif operation.op_type == "merge_entities":
                    before = operation.before_json or {}
                    for loser in before.get("losers") or []:
                        inverse.append(
                            {"op": "restore_entity", "snapshot": loser}
                        )
                    if before.get("survivor"):
                        inverse.append(
                            {
                                "op": "restore_entity",
                                "snapshot": before["survivor"],
                            }
                        )
                    for edge in before.get("edges") or []:
                        inverse.append(
                            {"op": "restore_relation", "snapshot": edge}
                        )
                elif operation.op_type == "split_entity":
                    before = operation.before_json or {}
                    for part in before.get("parts") or []:
                        inverse.append(
                            {
                                "op": "retire_entity",
                                "entity_id": part["entity_id"],
                                "reason": f"undo revision {revision}",
                            }
                        )
                    if before.get("source"):
                        inverse.append(
                            {
                                "op": "restore_entity",
                                "snapshot": before["source"],
                            }
                        )
                    for edge in before.get("edges") or []:
                        inverse.append(
                            {"op": "restore_relation", "snapshot": edge}
                        )
                elif operation.op_type == "override_relation":
                    if operation.before_json is None:
                        raise GraphSchemaValidationError(
                            "override_relation missing before snapshot"
                        )
                    inverse.append(
                        {
                            "op": "restore_relation",
                            "snapshot": operation.before_json,
                        }
                    )
                elif operation.op_type == "resolve_conflict":
                    raise GraphSchemaValidationError(
                        "conflict resolutions cannot be undone; open a new decision"
                    )
                elif operation.op_type == "attach_evidence":
                    inverse.append(
                        {
                            "op": "detach_evidence",
                            "evidence_id": operation.target_id
                            or operation.after_json.get("evidence_id"),
                        }
                    )
                elif operation.op_type == "detach_evidence":
                    if operation.before_json is None:
                        raise GraphSchemaValidationError(
                            "detach_evidence missing before snapshot"
                        )
                    inverse.append(
                        {
                            "op": "restore_evidence",
                            "snapshot": operation.before_json,
                        }
                    )
                elif operation.op_type == "restore_evidence":
                    inverse.append(
                        {
                            "op": "detach_evidence",
                            "evidence_id": operation.target_id,
                        }
                    )
                elif operation.op_type == "save_layout":
                    raise GraphSchemaValidationError(
                        "canvas layout revisions are append-only and cannot be undone"
                    )
                else:
                    raise GraphSchemaValidationError(
                        f"revision {revision} contains unsupported operation "
                        f"{operation.op_type!r}"
                    )
            if not inverse:
                raise GraphSchemaValidationError(
                    f"revision {revision} produced no inverse operations"
                )
            await self._validate_operations_against_schema(session, inverse)
            return await self._commit_local_operations(
                session,
                state=state,
                operations=inverse,
                summary=f"撤销 revision {revision}：{change_set.summary}",
                actor_id=actor_id,
                now=now,
                reverts_revision=revision,
            )

    async def redo_revision(
        self,
        revision: int,
        *,
        expected_revision: int,
        actor_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Replay the resulting relation states of one manual revision."""

        async with self._session_factory() as session:
            state = await self._locked_state(session, now=now)
            if state.head_revision != expected_revision:
                raise GraphRevisionConflict(
                    f"expected revision {expected_revision}, "
                    f"current revision is {state.head_revision}"
                )
            change_set, operations = await self._revision_operations(
                session,
                revision,
            )
            replay: list[dict[str, Any]] = []
            for operation in operations:
                if operation.op_type in {
                    "create_relation",
                    "revise_relation",
                    "restore_relation",
                }:
                    edge = await session.get(
                        KnowledgeGraphEdgeRecord,
                        operation.target_id,
                    )
                    if edge is None:
                        raise GraphSchemaValidationError(
                            f"relation {operation.target_id!r} is missing"
                        )
                    replay.append(
                        {
                            "op": "restore_relation",
                            "snapshot": await self._edge_snapshot(session, edge),
                        }
                    )
                elif operation.op_type == "retract_relation":
                    before = operation.before_json or {}
                    replay.append(
                        {
                            "op": "retract_relation",
                            "edge_id": operation.target_id,
                            "dedupe_key": before.get("dedupe_key"),
                            "reason": f"redo revision {revision}",
                        }
                    )
                elif operation.op_type in {
                    "upsert_schema_item",
                    "deprecate_schema_item",
                }:
                    item = await session.get(
                        KnowledgeGraphSchemaItemRecord,
                        operation.target_id,
                    )
                    if item is None:
                        raise GraphSchemaValidationError(
                            f"schema item {operation.target_id!r} is missing"
                        )
                    if operation.op_type == "upsert_schema_item":
                        replay.append(
                            {
                                "op": "upsert_schema_item",
                                "kind": item.kind,
                                "key": item.key,
                                "expected_version": item.version,
                                "definition": operation.after_json["definition"],
                            }
                        )
                    else:
                        replay.append(
                            {
                                "op": "deprecate_schema_item",
                                "kind": item.kind,
                                "key": item.key,
                                "expected_version": item.version,
                            }
                        )
                elif operation.op_type in LIFECYCLE_OPERATION_MODELS:
                    after = dict(operation.after_json)
                    after.pop("revision", None)
                    replay.append(after)
                else:
                    raise GraphSchemaValidationError(
                        f"revision {revision} contains unsupported operation "
                        f"{operation.op_type!r}"
                    )
            await self._validate_operations_against_schema(session, replay)
            return await self._commit_local_operations(
                session,
                state=state,
                operations=replay,
                summary=f"重做 revision {revision}：{change_set.summary}",
                actor_id=actor_id,
                now=now,
                replays_revision=revision,
            )

    async def list_change_sets(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List newest changesets for history and approval inboxes."""

        limit = max(1, min(int(limit), 500))
        async with self._session_factory() as session:
            query = select(KnowledgeGraphChangeSetRecord)
            if status:
                query = query.where(KnowledgeGraphChangeSetRecord.status == status)
            result = await session.execute(
                query.order_by(
                    KnowledgeGraphChangeSetRecord.created_at.desc(),
                    KnowledgeGraphChangeSetRecord.id.desc(),
                ).limit(limit)
            )
            records = list(result.scalars().all())
            if not records:
                return []
            operation_result = await session.execute(
                select(KnowledgeGraphChangeOperationRecord)
                .where(
                    KnowledgeGraphChangeOperationRecord.change_set_id.in_(
                        [record.id for record in records]
                    )
                )
                .order_by(
                    KnowledgeGraphChangeOperationRecord.change_set_id,
                    KnowledgeGraphChangeOperationRecord.position,
                )
            )
            operations_by_change_set: dict[str, list[dict[str, Any]]] = {
                record.id: [] for record in records
            }
            for operation in operation_result.scalars().all():
                operations_by_change_set[operation.change_set_id].append(
                    operation.after_json
                )
            return [
                self._change_set_payload(
                    record,
                    operations=operations_by_change_set[record.id],
                )
                for record in records
            ]

    async def get_head_revision(self) -> int:
        """Return the current audited graph revision without mutating state."""

        async with self._session_factory() as session:
            state = await session.get(
                KnowledgeGraphStateRecord,
                self._STATE_KEY,
            )
            return state.head_revision if state is not None else 0

    async def get_schema(self) -> dict[str, Any]:
        """Return protected system definitions merged with custom items."""

        payload = system_schema_payload()
        for collection in (
            payload["entity_types"],
            payload["relation_types"],
            payload["property_definitions"],
        ):
            for item in collection:
                item.update(
                    {
                        "namespace": "system",
                        "status": "active",
                        "version": payload["version"],
                    }
                )
        async with self._session_factory() as session:
            result = await session.execute(
                select(KnowledgeGraphSchemaItemRecord).order_by(
                    KnowledgeGraphSchemaItemRecord.kind,
                    KnowledgeGraphSchemaItemRecord.key,
                )
            )
            for record in result.scalars().all():
                item = {
                    "key": record.key,
                    **dict(record.definition_json),
                    "namespace": record.namespace,
                    "protected": False,
                    "status": record.status,
                    "version": record.version,
                }
                if record.kind == "entity_type":
                    payload["entity_types"].append(item)
                elif record.kind == "relation_type":
                    payload["relation_types"].append(item)
                else:
                    payload["property_definitions"].append(item)
        payload["revision"] = await self.get_head_revision()
        return payload

    async def list_conflicts(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List detected graph conflicts for the adjudication inbox."""

        limit = max(1, min(int(limit), 500))
        async with self._session_factory() as session:
            query = select(KnowledgeGraphConflictRecord)
            if status:
                query = query.where(KnowledgeGraphConflictRecord.status == status)
            result = await session.execute(
                query.order_by(
                    KnowledgeGraphConflictRecord.detected_at.desc(),
                    KnowledgeGraphConflictRecord.id.desc(),
                ).limit(limit)
            )
            return [conflict_payload(record) for record in result.scalars().all()]

    async def record_conflict(
        self,
        *,
        conflict_type: str,
        subject_key: str,
        left: dict[str, Any],
        right: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        """Persist one open conflict for later human adjudication."""

        async with self._session_factory() as session:
            record = KnowledgeGraphConflictRecord(
                id=f"kgcf-{uuid.uuid4().hex[:12]}",
                conflict_type=conflict_type,
                status="open",
                subject_key=subject_key,
                left_json=left,
                right_json=right,
                detected_at=now,
                resolved_at=None,
                resolution=None,
                change_set_id=None,
            )
            session.add(record)
            await session.commit()
            return conflict_payload(record)

    async def list_evidence(
        self,
        target_kind: str,
        target_id: str,
    ) -> list[dict[str, Any]]:
        """List active evidence attachments for a node or edge."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(KnowledgeGraphEvidenceRecord)
                .where(
                    KnowledgeGraphEvidenceRecord.target_kind == target_kind,
                    KnowledgeGraphEvidenceRecord.target_id == target_id,
                    KnowledgeGraphEvidenceRecord.status == "active",
                )
                .order_by(KnowledgeGraphEvidenceRecord.created_at.desc())
            )
            return [evidence_payload(record) for record in result.scalars().all()]

    async def get_latest_layout(self, scope_key: str) -> dict[str, Any] | None:
        """Return the newest saved canvas layout for one neighborhood scope."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(KnowledgeGraphCanvasLayoutRecord)
                .where(KnowledgeGraphCanvasLayoutRecord.scope_key == scope_key)
                .order_by(KnowledgeGraphCanvasLayoutRecord.version.desc())
                .limit(1)
            )
            record = result.scalar_one_or_none()
            return layout_payload(record) if record is not None else None

    async def list_lineage(self, entity_id: str) -> list[dict[str, Any]]:
        """Return merge/split lineage rows touching one entity."""

        async with self._session_factory() as session:
            result = await session.execute(
                select(KnowledgeGraphEntityLineageRecord).order_by(
                    KnowledgeGraphEntityLineageRecord.created_at.desc()
                )
            )
            rows = []
            for record in result.scalars().all():
                touched = {
                    record.survivor_id,
                    *list(record.source_ids_json or []),
                    *list(record.result_ids_json or []),
                }
                if entity_id in touched:
                    rows.append(lineage_payload(record))
            return rows


__all__ = [
    "GraphChangeSetNotFound",
    "GraphEditError",
    "GraphProposalMismatch",
    "GraphRevisionConflict",
    "GraphSchemaValidationError",
    "KnowledgeGraphCommandService",
]
