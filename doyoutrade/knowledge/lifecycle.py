"""Entity lifecycle, conflict, evidence, and canvas layout graph ops."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, or_, select

from doyoutrade.persistence.models import (
    KnowledgeGraphCanvasLayoutRecord,
    KnowledgeGraphConflictRecord,
    KnowledgeGraphEdgeRecord,
    KnowledgeGraphEntityLineageRecord,
    KnowledgeGraphEvidenceRecord,
    KnowledgeGraphNodeRecord,
)


class LifecycleValidationError(ValueError):
    """Raised when a lifecycle operation payload is invalid."""


class _CreateEntityOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["create_entity"]
    type: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=1024)
    attrs: dict[str, Any] | None = None


class _UpdateEntityOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["update_entity"]
    entity_id: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=1024)
    attrs: dict[str, Any] | None = None
    type: str | None = Field(default=None, min_length=1, max_length=32)


class _RetireEntityOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["retire_entity"]
    entity_id: str = Field(min_length=1, max_length=64)
    reason: str = Field(default="", max_length=2000)


class _RestoreEntityOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["restore_entity"]
    snapshot: dict[str, Any]


class _MergeEntitiesOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["merge_entities"]
    survivor_id: str = Field(min_length=1, max_length=64)
    merge_ids: list[str] = Field(min_length=1)
    reason: str = Field(default="", max_length=2000)


class _SplitPart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=1024)
    attrs: dict[str, Any] | None = None
    edge_ids: list[str] = Field(default_factory=list)


class _SplitEntityOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["split_entity"]
    source_id: str = Field(min_length=1, max_length=64)
    parts: list[_SplitPart] = Field(min_length=1)
    reason: str = Field(default="", max_length=2000)


class _OverrideRelationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["override_relation"]
    edge_id: str | None = Field(default=None, max_length=64)
    dedupe_key: str | None = Field(default=None, max_length=255)
    fact: str = Field(min_length=1, max_length=10000)
    attrs: dict[str, Any] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    valid_at: datetime | None = None
    invalid_at: datetime | None = None


class _ResolveConflictOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["resolve_conflict"]
    conflict_id: str = Field(min_length=1, max_length=64)
    decision: Literal["keep_left", "keep_right", "override", "dismiss"]
    override: dict[str, Any] | None = None


class _AttachEvidenceOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["attach_evidence"]
    target_kind: Literal["node", "edge"]
    target_id: str = Field(min_length=1, max_length=64)
    kind: Literal["kb_ref", "url", "quote", "file"]
    uri: str = Field(min_length=1, max_length=4000)
    excerpt: str = Field(default="", max_length=10000)
    attrs: dict[str, Any] | None = None


class _DetachEvidenceOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["detach_evidence"]
    evidence_id: str = Field(min_length=1, max_length=64)


class _RestoreEvidenceOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["restore_evidence"]
    snapshot: dict[str, Any]


class _SaveLayoutOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    op: Literal["save_layout"]
    scope_key: str = Field(min_length=1, max_length=128)
    positions: dict[str, dict[str, Any]]
    locked_ids: list[str] = Field(default_factory=list)
    highlight_ids: list[str] = Field(default_factory=list)


LIFECYCLE_OPERATION_MODELS: dict[str, type[BaseModel]] = {
    "create_entity": _CreateEntityOperation,
    "update_entity": _UpdateEntityOperation,
    "retire_entity": _RetireEntityOperation,
    "restore_entity": _RestoreEntityOperation,
    "merge_entities": _MergeEntitiesOperation,
    "split_entity": _SplitEntityOperation,
    "override_relation": _OverrideRelationOperation,
    "resolve_conflict": _ResolveConflictOperation,
    "attach_evidence": _AttachEvidenceOperation,
    "detach_evidence": _DetachEvidenceOperation,
    "restore_evidence": _RestoreEvidenceOperation,
    "save_layout": _SaveLayoutOperation,
}


def normalize_lifecycle_operation(
    raw: dict[str, Any],
    *,
    index: int,
) -> dict[str, Any]:
    """Validate and normalize one lifecycle operation payload."""

    operation_type = raw.get("op")
    model = LIFECYCLE_OPERATION_MODELS.get(str(operation_type))
    if model is None:
        raise LifecycleValidationError(
            f"operations[{index}] uses unknown operation {operation_type!r}"
        )
    try:
        operation = model.model_validate(raw)
    except ValidationError as exc:
        raise LifecycleValidationError(
            f"operations[{index}] is invalid: {exc.errors(include_url=False)}"
        ) from exc
    if isinstance(operation, _UpdateEntityOperation):
        if not (operation.model_fields_set - {"op", "entity_id"}):
            raise LifecycleValidationError(
                f"operations[{index}] update_entity has no changes"
            )
    if isinstance(operation, _AttachEvidenceOperation):
        if operation.kind == "kb_ref":
            uri = operation.uri
            if not uri.startswith("kb:") or ".." in uri or uri.startswith("kb:/"):
                raise LifecycleValidationError(
                    f"operations[{index}] kb_ref uri must be a relative kb: path"
                )
    if isinstance(operation, _OverrideRelationOperation):
        if not operation.edge_id and not operation.dedupe_key:
            raise LifecycleValidationError(
                f"operations[{index}] override_relation requires edge_id or dedupe_key"
            )
    return operation.model_dump(mode="json", exclude_unset=True)


def node_snapshot(node: KnowledgeGraphNodeRecord) -> dict[str, Any]:
    """Return a reversible node snapshot."""

    return {
        "entity_id": node.id,
        "type": node.node_type,
        "name": node.name,
        "display_name": node.display_name,
        "attrs": node.attrs,
        "status": node.status,
        "retired_at": node.retired_at.isoformat() if node.retired_at else None,
        "redirect_to_id": node.redirect_to_id,
    }


async def resolve_redirect(
    session: Any,
    node: KnowledgeGraphNodeRecord,
    *,
    max_hops: int = 8,
) -> KnowledgeGraphNodeRecord:
    """Follow merge redirects to the surviving active entity."""

    current = node
    seen = {current.id}
    for _ in range(max_hops):
        if current.status != "merged" or not current.redirect_to_id:
            return current
        nxt = await session.get(KnowledgeGraphNodeRecord, current.redirect_to_id)
        if nxt is None or nxt.id in seen:
            return current
        seen.add(nxt.id)
        current = nxt
    return current


async def require_active_entity(
    session: Any,
    entity_id: str,
) -> KnowledgeGraphNodeRecord:
    """Load an active entity or raise."""

    node = await session.get(KnowledgeGraphNodeRecord, entity_id)
    if node is None:
        raise LifecycleValidationError(f"entity {entity_id!r} does not exist")
    if node.status != "active":
        raise LifecycleValidationError(
            f"entity {entity_id!r} is {node.status!r}, expected active"
        )
    return node


def evidence_payload(record: KnowledgeGraphEvidenceRecord) -> dict[str, Any]:
    """Serialize one evidence row."""

    return {
        "id": record.id,
        "target_kind": record.target_kind,
        "target_id": record.target_id,
        "kind": record.kind,
        "uri": record.uri,
        "excerpt": record.excerpt,
        "attrs": record.attrs,
        "status": record.status,
        "change_set_id": record.change_set_id,
        "created_at": record.created_at.isoformat(),
        "detached_at": (
            record.detached_at.isoformat() if record.detached_at else None
        ),
    }


def conflict_payload(record: KnowledgeGraphConflictRecord) -> dict[str, Any]:
    """Serialize one conflict row."""

    return {
        "id": record.id,
        "conflict_type": record.conflict_type,
        "status": record.status,
        "subject_key": record.subject_key,
        "left": record.left_json,
        "right": record.right_json,
        "detected_at": record.detected_at.isoformat(),
        "resolved_at": (
            record.resolved_at.isoformat() if record.resolved_at else None
        ),
        "resolution": record.resolution,
        "change_set_id": record.change_set_id,
    }


def lineage_payload(record: KnowledgeGraphEntityLineageRecord) -> dict[str, Any]:
    """Serialize one lineage row."""

    return {
        "id": record.id,
        "kind": record.kind,
        "survivor_id": record.survivor_id,
        "source_ids": list(record.source_ids_json),
        "result_ids": list(record.result_ids_json),
        "change_set_id": record.change_set_id,
        "revision": record.revision,
        "reason": record.reason,
        "created_at": record.created_at.isoformat(),
    }


def layout_payload(record: KnowledgeGraphCanvasLayoutRecord) -> dict[str, Any]:
    """Serialize one canvas layout row."""

    return {
        "id": record.id,
        "scope_key": record.scope_key,
        "version": record.version,
        "positions": dict(record.positions_json),
        "locked_ids": list(record.locked_ids_json),
        "highlight_ids": list(record.highlight_ids_json),
        "actor_id": record.actor_id,
        "change_set_id": record.change_set_id,
        "created_at": record.created_at.isoformat(),
    }


async def apply_lifecycle_operation(
    session: Any,
    *,
    operation_record: Any,
    change_set_id: str,
    revision: int,
    now: datetime,
    insert_manual_edge,
    edge_snapshot,
) -> str:
    """Apply one lifecycle op; return the primary affected id."""

    payload = dict(operation_record.after_json)
    operation_type = operation_record.op_type

    if operation_type == "create_entity":
        result = await session.execute(
            select(KnowledgeGraphNodeRecord).where(
                KnowledgeGraphNodeRecord.node_type == payload["type"],
                KnowledgeGraphNodeRecord.name == payload["name"],
            )
        )
        existing = result.scalar_one_or_none()
        if existing is not None and existing.status == "active":
            raise LifecycleValidationError(
                f"entity {payload['type']}/{payload['name']} already exists"
            )
        if existing is not None:
            operation_record.before_json = node_snapshot(existing)
            existing.status = "active"
            existing.retired_at = None
            existing.redirect_to_id = None
            existing.display_name = payload.get("display_name")
            existing.attrs = payload.get("attrs")
            existing.updated_at = now
            node = existing
        else:
            operation_record.before_json = None
            node = KnowledgeGraphNodeRecord(
                id=f"kgn-{uuid.uuid4().hex[:12]}",
                node_type=payload["type"],
                name=payload["name"],
                display_name=payload.get("display_name"),
                attrs=payload.get("attrs"),
                status="active",
                retired_at=None,
                redirect_to_id=None,
                created_at=now,
                updated_at=now,
            )
            session.add(node)
        await session.flush()
        operation_record.target_id = node.id
        operation_record.after_json = {
            **payload,
            "entity_id": node.id,
            "revision": revision,
        }
        return node.id

    if operation_type == "update_entity":
        node = await require_active_entity(session, payload["entity_id"])
        operation_record.before_json = node_snapshot(node)
        if payload.get("type") and payload["type"] != node.node_type:
            clash = await session.execute(
                select(KnowledgeGraphNodeRecord).where(
                    KnowledgeGraphNodeRecord.node_type == payload["type"],
                    KnowledgeGraphNodeRecord.name == node.name,
                    KnowledgeGraphNodeRecord.id != node.id,
                )
            )
            if clash.scalar_one_or_none() is not None:
                raise LifecycleValidationError(
                    f"entity type rename collides on {payload['type']}/{node.name}"
                )
            node.node_type = payload["type"]
        if "display_name" in payload:
            node.display_name = payload["display_name"]
        if "attrs" in payload:
            node.attrs = payload["attrs"]
        node.updated_at = now
        await session.flush()
        operation_record.target_id = node.id
        operation_record.after_json = {
            **payload,
            "revision": revision,
            "snapshot": node_snapshot(node),
        }
        return node.id

    if operation_type == "retire_entity":
        node = await require_active_entity(session, payload["entity_id"])
        operation_record.before_json = node_snapshot(node)
        node.status = "retired"
        node.retired_at = now
        node.updated_at = now
        await session.flush()
        operation_record.target_id = node.id
        operation_record.after_json = {**payload, "revision": revision}
        return node.id

    if operation_type == "restore_entity":
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("entity_id"):
            raise LifecycleValidationError("restore_entity requires a snapshot")
        node = await session.get(KnowledgeGraphNodeRecord, snapshot["entity_id"])
        if node is None:
            node = KnowledgeGraphNodeRecord(
                id=snapshot["entity_id"],
                node_type=snapshot["type"],
                name=snapshot["name"],
                display_name=snapshot.get("display_name"),
                attrs=snapshot.get("attrs"),
                status=snapshot.get("status") or "active",
                retired_at=None,
                redirect_to_id=snapshot.get("redirect_to_id"),
                created_at=now,
                updated_at=now,
            )
            session.add(node)
            operation_record.before_json = None
        else:
            operation_record.before_json = node_snapshot(node)
            node.node_type = snapshot["type"]
            node.name = snapshot["name"]
            node.display_name = snapshot.get("display_name")
            node.attrs = snapshot.get("attrs")
            node.status = snapshot.get("status") or "active"
            node.redirect_to_id = snapshot.get("redirect_to_id")
            if node.status == "active":
                node.retired_at = None
            node.updated_at = now
        await session.flush()
        operation_record.target_id = node.id
        operation_record.after_json = {
            **payload,
            "entity_id": node.id,
            "revision": revision,
        }
        return node.id

    if operation_type == "merge_entities":
        survivor = await require_active_entity(session, payload["survivor_id"])
        merge_ids = [mid for mid in payload["merge_ids"] if mid != survivor.id]
        if not merge_ids:
            raise LifecycleValidationError("merge_entities requires merge_ids")
        losers = []
        for merge_id in merge_ids:
            losers.append(await require_active_entity(session, merge_id))
        before = {
            "survivor": node_snapshot(survivor),
            "losers": [node_snapshot(item) for item in losers],
            "edges": [],
        }
        loser_ids = {item.id for item in losers}
        edge_result = await session.execute(
            select(KnowledgeGraphEdgeRecord).where(
                KnowledgeGraphEdgeRecord.expired_at.is_(None),
                or_(
                    KnowledgeGraphEdgeRecord.src_id.in_(sorted(loser_ids)),
                    KnowledgeGraphEdgeRecord.dst_id.in_(sorted(loser_ids)),
                ),
            )
        )
        edges = list(edge_result.scalars().all())
        planned = []
        planned_dedupe: set[str] = set()
        for edge in edges:
            snap = await edge_snapshot(session, edge)
            before["edges"].append(snap)
            new_src = survivor.id if edge.src_id in loser_ids else edge.src_id
            new_dst = survivor.id if edge.dst_id in loser_ids else edge.dst_id
            if new_src == new_dst:
                continue
            if edge.dedupe_key in planned_dedupe:
                raise LifecycleValidationError(
                    f"merge collides on dedupe_key {edge.dedupe_key!r}"
                )
            active = await session.execute(
                select(KnowledgeGraphEdgeRecord).where(
                    KnowledgeGraphEdgeRecord.dedupe_key == edge.dedupe_key,
                    KnowledgeGraphEdgeRecord.expired_at.is_(None),
                    KnowledgeGraphEdgeRecord.id != edge.id,
                )
            )
            other = active.scalars().first()
            if other is not None and (
                other.src_id not in loser_ids and other.dst_id not in loser_ids
            ):
                raise LifecycleValidationError(
                    f"merge collides on active dedupe_key {edge.dedupe_key!r}"
                )
            planned_dedupe.add(edge.dedupe_key)
            planned.append((edge, new_src, new_dst))

        for edge, new_src, new_dst in planned:
            edge.expired_at = now
            await session.flush()
            session.add(
                KnowledgeGraphEdgeRecord(
                    id=f"kge-{uuid.uuid4().hex[:12]}",
                    src_id=new_src,
                    dst_id=new_dst,
                    relation=edge.relation,
                    fact=edge.fact,
                    attrs=edge.attrs,
                    dedupe_key=edge.dedupe_key,
                    state_key=edge.state_key,
                    provenance="manual",
                    confidence=edge.confidence,
                    source_key=f"manual:change-set/{change_set_id}",
                    source_ref=f"manual:change-set/{change_set_id}",
                    valid_at=edge.valid_at,
                    invalid_at=edge.invalid_at,
                    created_at=now,
                    expired_at=None,
                )
            )
        for loser in losers:
            loser.status = "merged"
            loser.redirect_to_id = survivor.id
            loser.retired_at = now
            loser.updated_at = now
        lineage = KnowledgeGraphEntityLineageRecord(
            id=f"kgel-{uuid.uuid4().hex[:12]}",
            kind="merge",
            survivor_id=survivor.id,
            source_ids_json=[survivor.id, *merge_ids],
            result_ids_json=[survivor.id],
            change_set_id=change_set_id,
            revision=revision,
            reason=str(payload.get("reason") or ""),
            created_at=now,
        )
        session.add(lineage)
        await session.flush()
        operation_record.before_json = before
        operation_record.target_id = survivor.id
        operation_record.after_json = {
            **payload,
            "lineage_id": lineage.id,
            "revision": revision,
        }
        return survivor.id

    if operation_type == "split_entity":
        source = await require_active_entity(session, payload["source_id"])
        before = {"source": node_snapshot(source), "parts": [], "edges": []}
        result_ids: list[str] = []
        moved_edge_ids: set[str] = set()
        for part in payload["parts"]:
            clash = await session.execute(
                select(KnowledgeGraphNodeRecord).where(
                    KnowledgeGraphNodeRecord.node_type == part["type"],
                    KnowledgeGraphNodeRecord.name == part["name"],
                )
            )
            if clash.scalar_one_or_none() is not None:
                raise LifecycleValidationError(
                    f"split part collides on {part['type']}/{part['name']}"
                )
            node = KnowledgeGraphNodeRecord(
                id=f"kgn-{uuid.uuid4().hex[:12]}",
                node_type=part["type"],
                name=part["name"],
                display_name=part.get("display_name"),
                attrs=part.get("attrs"),
                status="active",
                retired_at=None,
                redirect_to_id=None,
                created_at=now,
                updated_at=now,
            )
            session.add(node)
            await session.flush()
            result_ids.append(node.id)
            before["parts"].append(node_snapshot(node))
            for edge_id in part.get("edge_ids") or []:
                if edge_id in moved_edge_ids:
                    raise LifecycleValidationError(
                        f"split edge {edge_id!r} assigned twice"
                    )
                edge = await session.get(KnowledgeGraphEdgeRecord, edge_id)
                if edge is None or edge.expired_at is not None:
                    raise LifecycleValidationError(
                        f"split edge {edge_id!r} is missing or inactive"
                    )
                if source.id not in {edge.src_id, edge.dst_id}:
                    raise LifecycleValidationError(
                        f"split edge {edge_id!r} is not attached to source"
                    )
                before["edges"].append(await edge_snapshot(session, edge))
                new_src = node.id if edge.src_id == source.id else edge.src_id
                new_dst = node.id if edge.dst_id == source.id else edge.dst_id
                edge.expired_at = now
                await session.flush()
                session.add(
                    KnowledgeGraphEdgeRecord(
                        id=f"kge-{uuid.uuid4().hex[:12]}",
                        src_id=new_src,
                        dst_id=new_dst,
                        relation=edge.relation,
                        fact=edge.fact,
                        attrs=edge.attrs,
                        dedupe_key=edge.dedupe_key,
                        state_key=edge.state_key,
                        provenance="manual",
                        confidence=edge.confidence,
                        source_key=f"manual:change-set/{change_set_id}",
                        source_ref=f"manual:change-set/{change_set_id}",
                        valid_at=edge.valid_at,
                        invalid_at=edge.invalid_at,
                        created_at=now,
                        expired_at=None,
                    )
                )
                moved_edge_ids.add(edge_id)
        remaining = await session.execute(
            select(func.count(KnowledgeGraphEdgeRecord.id)).where(
                KnowledgeGraphEdgeRecord.expired_at.is_(None),
                or_(
                    KnowledgeGraphEdgeRecord.src_id == source.id,
                    KnowledgeGraphEdgeRecord.dst_id == source.id,
                ),
            )
        )
        if int(remaining.scalar_one()) == 0:
            source.status = "retired"
            source.retired_at = now
            source.updated_at = now
        lineage = KnowledgeGraphEntityLineageRecord(
            id=f"kgel-{uuid.uuid4().hex[:12]}",
            kind="split",
            survivor_id=None,
            source_ids_json=[source.id],
            result_ids_json=result_ids,
            change_set_id=change_set_id,
            revision=revision,
            reason=str(payload.get("reason") or ""),
            created_at=now,
        )
        session.add(lineage)
        await session.flush()
        operation_record.before_json = before
        operation_record.target_id = source.id
        operation_record.after_json = {
            **payload,
            "result_ids": result_ids,
            "lineage_id": lineage.id,
            "revision": revision,
        }
        return source.id

    if operation_type == "override_relation":
        edge = None
        if payload.get("edge_id"):
            edge = await session.get(KnowledgeGraphEdgeRecord, payload["edge_id"])
        if edge is None and payload.get("dedupe_key"):
            result = await session.execute(
                select(KnowledgeGraphEdgeRecord).where(
                    KnowledgeGraphEdgeRecord.dedupe_key == payload["dedupe_key"],
                    KnowledgeGraphEdgeRecord.expired_at.is_(None),
                )
            )
            edge = result.scalars().first()
        if edge is None or edge.expired_at is not None:
            raise LifecycleValidationError("override_relation target edge not found")
        before = await edge_snapshot(session, edge)
        operation_record.before_json = before
        edge.expired_at = now
        await session.flush()
        revised = {
            **before,
            "fact": payload["fact"],
            "attrs": payload.get("attrs", before.get("attrs")),
            "confidence": payload.get("confidence", before.get("confidence")),
            "valid_at": payload.get("valid_at", before.get("valid_at")),
            "invalid_at": payload.get("invalid_at", before.get("invalid_at")),
            "dedupe_key": edge.dedupe_key,
            "source_key": f"manual:change-set/{change_set_id}",
        }
        new_edge = await insert_manual_edge(
            session,
            revised,
            change_set_id=change_set_id,
            position=operation_record.position,
            now=now,
        )
        operation_record.target_id = new_edge.id
        operation_record.after_json = {
            **payload,
            "edge_id": new_edge.id,
            "dedupe_key": new_edge.dedupe_key,
            "revision": revision,
        }
        return new_edge.id

    if operation_type == "resolve_conflict":
        from doyoutrade.persistence.models import KnowledgeGraphChangeOperationRecord

        conflict = await session.get(
            KnowledgeGraphConflictRecord,
            payload["conflict_id"],
        )
        if conflict is None:
            raise LifecycleValidationError(
                f"conflict {payload['conflict_id']!r} does not exist"
            )
        if conflict.status != "open":
            raise LifecycleValidationError(
                f"conflict {conflict.id!r} is already {conflict.status!r}"
            )
        operation_record.before_json = conflict_payload(conflict)
        decision = payload["decision"]
        if decision == "override":
            override = payload.get("override")
            if not isinstance(override, dict):
                raise LifecycleValidationError(
                    "resolve_conflict override decision requires override payload"
                )
            nested = KnowledgeGraphChangeOperationRecord(
                id=f"kgop-{uuid.uuid4().hex[:12]}",
                change_set_id=change_set_id,
                position=operation_record.position,
                op_type="override_relation",
                target_id=None,
                before_json=None,
                after_json={"op": "override_relation", **override},
            )
            await apply_lifecycle_operation(
                session,
                operation_record=nested,
                change_set_id=change_set_id,
                revision=revision,
                now=now,
                insert_manual_edge=insert_manual_edge,
                edge_snapshot=edge_snapshot,
            )
            conflict.status = "resolved"
        elif decision == "dismiss":
            conflict.status = "dismissed"
        else:
            conflict.status = "resolved"
        conflict.resolved_at = now
        conflict.resolution = {
            "decision": decision,
            "override": payload.get("override"),
        }
        conflict.change_set_id = change_set_id
        await session.flush()
        operation_record.target_id = conflict.id
        operation_record.after_json = {
            **payload,
            "status": conflict.status,
            "revision": revision,
        }
        return conflict.id

    if operation_type == "attach_evidence":
        if payload["target_kind"] == "node":
            target = await session.get(
                KnowledgeGraphNodeRecord,
                payload["target_id"],
            )
            if target is None:
                raise LifecycleValidationError(
                    f"evidence target node {payload['target_id']!r} missing"
                )
        else:
            target = await session.get(
                KnowledgeGraphEdgeRecord,
                payload["target_id"],
            )
            if target is None:
                raise LifecycleValidationError(
                    f"evidence target edge {payload['target_id']!r} missing"
                )
        record = KnowledgeGraphEvidenceRecord(
            id=f"kgev-{uuid.uuid4().hex[:12]}",
            target_kind=payload["target_kind"],
            target_id=payload["target_id"],
            kind=payload["kind"],
            uri=payload["uri"],
            excerpt=str(payload.get("excerpt") or ""),
            attrs=payload.get("attrs"),
            status="active",
            change_set_id=change_set_id,
            created_at=now,
            detached_at=None,
        )
        session.add(record)
        await session.flush()
        operation_record.before_json = None
        operation_record.target_id = record.id
        operation_record.after_json = {
            **payload,
            "evidence_id": record.id,
            "revision": revision,
        }
        return record.id

    if operation_type == "detach_evidence":
        record = await session.get(
            KnowledgeGraphEvidenceRecord,
            payload["evidence_id"],
        )
        if record is None or record.status != "active":
            raise LifecycleValidationError(
                f"evidence {payload['evidence_id']!r} is missing or detached"
            )
        operation_record.before_json = evidence_payload(record)
        record.status = "detached"
        record.detached_at = now
        await session.flush()
        operation_record.target_id = record.id
        operation_record.after_json = {**payload, "revision": revision}
        return record.id

    if operation_type == "restore_evidence":
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("id"):
            raise LifecycleValidationError("restore_evidence requires a snapshot")
        record = await session.get(KnowledgeGraphEvidenceRecord, snapshot["id"])
        if record is None:
            record = KnowledgeGraphEvidenceRecord(
                id=snapshot["id"],
                target_kind=snapshot["target_kind"],
                target_id=snapshot["target_id"],
                kind=snapshot["kind"],
                uri=snapshot["uri"],
                excerpt=snapshot.get("excerpt") or "",
                attrs=snapshot.get("attrs"),
                status="active",
                change_set_id=change_set_id,
                created_at=now,
                detached_at=None,
            )
            session.add(record)
            operation_record.before_json = None
        else:
            operation_record.before_json = evidence_payload(record)
            record.status = "active"
            record.detached_at = None
            record.uri = snapshot["uri"]
            record.excerpt = snapshot.get("excerpt") or ""
            record.attrs = snapshot.get("attrs")
        await session.flush()
        operation_record.target_id = record.id
        operation_record.after_json = {
            **payload,
            "evidence_id": record.id,
            "revision": revision,
        }
        return record.id

    if operation_type == "save_layout":
        version_result = await session.execute(
            select(func.max(KnowledgeGraphCanvasLayoutRecord.version)).where(
                KnowledgeGraphCanvasLayoutRecord.scope_key == payload["scope_key"]
            )
        )
        current_version = version_result.scalar_one()
        next_version = int(current_version or 0) + 1
        record = KnowledgeGraphCanvasLayoutRecord(
            id=f"kgcl-{uuid.uuid4().hex[:12]}",
            scope_key=payload["scope_key"],
            version=next_version,
            positions_json=payload["positions"],
            locked_ids_json=list(payload.get("locked_ids") or []),
            highlight_ids_json=list(payload.get("highlight_ids") or []),
            actor_id="local-user",
            change_set_id=change_set_id,
            created_at=now,
        )
        session.add(record)
        await session.flush()
        operation_record.before_json = {
            "scope_key": payload["scope_key"],
            "version": current_version,
        }
        operation_record.target_id = record.id
        operation_record.after_json = {
            **payload,
            "layout_id": record.id,
            "version": next_version,
            "revision": revision,
        }
        return record.id

    raise LifecycleValidationError(f"unsupported lifecycle op {operation_type!r}")


__all__ = [
    "LIFECYCLE_OPERATION_MODELS",
    "LifecycleValidationError",
    "apply_lifecycle_operation",
    "conflict_payload",
    "evidence_payload",
    "layout_payload",
    "lineage_payload",
    "node_snapshot",
    "normalize_lifecycle_operation",
    "require_active_entity",
    "resolve_redirect",
]
