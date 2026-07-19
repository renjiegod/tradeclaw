"""Manual graph changes and Agent approval workflow."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import event, func, select, text

from doyoutrade.knowledge.editing import (
    GraphProposalMismatch,
    GraphRevisionConflict,
    GraphSchemaValidationError,
    KnowledgeGraphCommandService,
)
from doyoutrade.persistence.db import (
    create_engine_and_session_factory,
    dispose_engine,
)
from doyoutrade.persistence.models import (
    Base,
    KnowledgeGraphChangeOperationRecord,
    KnowledgeGraphChangeSetRecord,
    KnowledgeGraphEdgeRecord,
)
from doyoutrade.persistence.repositories import (
    KnowledgeGraphEdgeSpec,
    KnowledgeGraphNodeSpec,
    SqlAlchemyKnowledgeGraphRepository,
)


def _relation_operation(
    *,
    source_name: str = "300059",
    relation: str = "belongs_to_theme",
    target_type: str = "theme",
    target_name: str = "券商",
) -> dict:
    return {
        "op": "create_relation",
        "source": {
            "type": "symbol",
            "name": source_name,
            "display_name": "东方财富",
        },
        "relation": relation,
        "target": {"type": target_type, "name": target_name},
        "fact": "东方财富属于券商题材。",
        "confidence": 1.0,
    }


class KnowledgeGraphCommandServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.repository = SqlAlchemyKnowledgeGraphRepository(self.session_factory)
        self.service = KnowledgeGraphCommandService(self.session_factory)
        self.now = datetime(2026, 7, 18, 1, 0)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_local_user_applies_audited_manual_relation(self) -> None:
        result = await self.service.apply_local_change(
            [_relation_operation()],
            summary="手工标记东方财富所属题材",
            expected_revision=0,
            actor_id="local-user",
            now=self.now,
        )

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["actor_type"], "local_user")
        matches = await self.repository.find_nodes("300059")
        _, edges = await self.repository.neighborhood(matches[0].id, hops=1)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].provenance, "manual")
        self.assertEqual(edges[0].relation, "belongs_to_theme")
        history = await self.service.list_change_sets()
        self.assertEqual(history[0]["id"], result["id"])
        self.assertEqual(history[0]["status"], "applied")

    async def test_agent_draft_respects_fk_order_with_foreign_keys_on(self) -> None:
        """Postgres-style FK: change_set must exist before operations.

        SQLite hides this unless ``PRAGMA foreign_keys=ON``. Regression for the
        Agent propose path that previously INSERT operations before the parent
        change_set was flushed (``kg_change_operations_change_set_id_fkey``).
        """

        @event.listens_for(self.engine.sync_engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        async with self.engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))

        ops = [
            _relation_operation(
                source_name=f"300{i:03d}",
                target_name=f"题材{i}",
            )
            for i in range(30)
        ]
        draft = await self.service.create_agent_draft(
            ops,
            summary="批量 Agent 关系草案（FK 回归）",
            actor_id="agent-1",
            now=self.now,
        )
        self.assertEqual(draft["status"], "pending")
        self.assertEqual(len(draft["operations"]), 30)

        async with self.session_factory() as session:
            cs_count = await session.execute(
                select(func.count()).select_from(KnowledgeGraphChangeSetRecord)
            )
            op_count = await session.execute(
                select(func.count()).select_from(KnowledgeGraphChangeOperationRecord)
            )
            self.assertEqual(int(cs_count.scalar_one()), 1)
            self.assertEqual(int(op_count.scalar_one()), 30)
            orphan = await session.execute(
                select(KnowledgeGraphChangeOperationRecord).where(
                    KnowledgeGraphChangeOperationRecord.change_set_id != draft["id"]
                )
            )
            self.assertEqual(list(orphan.scalars().all()), [])

    async def test_local_change_respects_fk_order_with_foreign_keys_on(self) -> None:
        """Local audited edits must flush change_set before operations on PG."""

        @event.listens_for(self.engine.sync_engine, "connect")
        def _enable_fk(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        async with self.engine.begin() as conn:
            await conn.execute(text("PRAGMA foreign_keys=ON"))

        result = await self.service.apply_local_change(
            [
                _relation_operation(source_name="300001", target_name="题材A"),
                _relation_operation(source_name="300002", target_name="题材B"),
            ],
            summary="本地批量手工关系（FK 回归）",
            expected_revision=0,
            actor_id="local-user",
            now=self.now,
        )
        self.assertEqual(result["status"], "applied")
        self.assertEqual((await self.repository.counts())["active_edges"], 2)

    async def test_agent_draft_does_not_mutate_until_one_time_approval(self) -> None:
        draft = await self.service.create_agent_draft(
            [_relation_operation()],
            summary="Agent 建议补充题材关系",
            actor_id="agent-1",
            now=self.now,
        )

        self.assertEqual(draft["status"], "pending")
        self.assertEqual((await self.repository.counts())["active_edges"], 0)

        applied = await self.service.approve_draft(
            draft["id"],
            proposal_hash=draft["proposal_hash"],
            resolver_id="local-user",
            decision_source="web",
            now=datetime(2026, 7, 18, 1, 1),
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual((await self.repository.counts())["active_edges"], 1)

        with self.assertRaises(GraphRevisionConflict):
            await self.service.approve_draft(
                draft["id"],
                proposal_hash=draft["proposal_hash"],
                resolver_id="local-user",
                decision_source="web",
                now=datetime(2026, 7, 18, 1, 2),
            )

    async def test_stale_agent_draft_cannot_apply_over_new_revision(self) -> None:
        draft = await self.service.create_agent_draft(
            [_relation_operation()],
            summary="Agent 草案",
            actor_id="agent-1",
            now=self.now,
        )
        await self.service.apply_local_change(
            [
                _relation_operation(
                    source_name="600519",
                    target_name="白酒",
                )
            ],
            summary="本地用户并发修改",
            expected_revision=0,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 1),
        )

        with self.assertRaises(GraphRevisionConflict):
            await self.service.approve_draft(
                draft["id"],
                proposal_hash=draft["proposal_hash"],
                resolver_id="local-user",
                decision_source="web",
                now=datetime(2026, 7, 18, 1, 2),
            )
        self.assertEqual((await self.repository.counts())["active_edges"], 1)

    async def test_system_projection_also_invalidates_agent_draft(self) -> None:
        draft = await self.service.create_agent_draft(
            [_relation_operation()],
            summary="Agent 草案",
            actor_id="agent-1",
            now=self.now,
        )
        await self.repository.apply_projection(
            [KnowledgeGraphNodeSpec(node_type="cycle", name="2026-07")],
            [],
            now=datetime(2026, 7, 18, 1, 1),
            reconcile_source_keys={"kb:test"},
            source_hashes={"kb:test": "digest-1"},
        )

        with self.assertRaises(GraphRevisionConflict):
            await self.service.approve_draft(
                draft["id"],
                proposal_hash=draft["proposal_hash"],
                resolver_id="local-user",
                decision_source="web",
                now=datetime(2026, 7, 18, 1, 2),
            )
        history = await self.service.list_change_sets()
        self.assertEqual(history[0]["actor_type"], "system")
        self.assertEqual(history[0]["revision"], 1)

    async def test_approval_rejects_changed_proposal_hash(self) -> None:
        draft = await self.service.create_agent_draft(
            [_relation_operation()],
            summary="Agent 草案",
            actor_id="agent-1",
            now=self.now,
        )

        with self.assertRaises(GraphProposalMismatch):
            await self.service.approve_draft(
                draft["id"],
                proposal_hash="tampered",
                resolver_id="local-user",
                decision_source="web",
                now=datetime(2026, 7, 18, 1, 1),
            )
        self.assertEqual((await self.repository.counts())["active_edges"], 0)

    async def test_rejected_agent_draft_never_mutates_graph(self) -> None:
        draft = await self.service.create_agent_draft(
            [_relation_operation()],
            summary="Agent 草案",
            actor_id="agent-1",
            now=self.now,
        )

        rejected = await self.service.reject_draft(
            draft["id"],
            proposal_hash=draft["proposal_hash"],
            resolver_id="local-user",
            decision_source="web",
            reason="事实依据不足",
            now=datetime(2026, 7, 18, 1, 1),
        )

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual((await self.repository.counts())["active_edges"], 0)
        with self.assertRaises(GraphRevisionConflict):
            await self.service.approve_draft(
                draft["id"],
                proposal_hash=draft["proposal_hash"],
                resolver_id="local-user",
                decision_source="web",
                now=datetime(2026, 7, 18, 1, 2),
            )

    async def test_relation_endpoints_must_match_protected_schema(self) -> None:
        with self.assertRaises(GraphSchemaValidationError):
            await self.service.apply_local_change(
                [
                    _relation_operation(
                        relation="has_role",
                        target_type="theme",
                    )
                ],
                summary="非法端点",
                expected_revision=0,
                actor_id="local-user",
                now=self.now,
            )
        self.assertEqual((await self.repository.counts())["active_edges"], 0)

    async def test_manual_relation_revise_retract_undo_and_redo(self) -> None:
        created = await self.service.apply_local_change(
            [_relation_operation()],
            summary="新增题材关系",
            expected_revision=0,
            actor_id="local-user",
            now=self.now,
        )
        original_edge_id = created["edge_ids"][0]

        revised = await self.service.apply_local_change(
            [
                {
                    "op": "revise_relation",
                    "edge_id": original_edge_id,
                    "fact": "东方财富明确属于金融科技题材。",
                    "confidence": 0.9,
                }
            ],
            summary="修订事实",
            expected_revision=1,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 1),
        )
        revised_edge_id = revised["edge_ids"][0]
        self.assertNotEqual(revised_edge_id, original_edge_id)
        matches = await self.repository.find_nodes("300059")
        _, edges = await self.repository.neighborhood(
            matches[0].id,
            hops=1,
            include_expired=True,
        )
        self.assertEqual(
            [edge.fact for edge in edges if edge.expired_at is None],
            ["东方财富明确属于金融科技题材。"],
        )

        undone = await self.service.undo_revision(
            2,
            expected_revision=2,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 2),
        )
        self.assertEqual(undone["revision"], 3)
        _, edges = await self.repository.neighborhood(
            matches[0].id,
            hops=1,
            include_expired=True,
        )
        self.assertEqual(
            [edge.fact for edge in edges if edge.expired_at is None],
            ["东方财富属于券商题材。"],
        )

        redone = await self.service.redo_revision(
            2,
            expected_revision=3,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 3),
        )
        self.assertEqual(redone["revision"], 4)
        _, edges = await self.repository.neighborhood(
            matches[0].id,
            hops=1,
            include_expired=True,
        )
        active = [edge for edge in edges if edge.expired_at is None]
        self.assertEqual(
            [edge.fact for edge in active],
            ["东方财富明确属于金融科技题材。"],
        )

        retracted = await self.service.apply_local_change(
            [
                {
                    "op": "retract_relation",
                    "edge_id": active[0].id,
                    "reason": "该关系已不成立",
                }
            ],
            summary="失效关系",
            expected_revision=4,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 4),
        )
        self.assertEqual(retracted["revision"], 5)
        self.assertEqual((await self.repository.counts())["active_edges"], 0)

        await self.service.undo_revision(
            5,
            expected_revision=5,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 5),
        )
        self.assertEqual((await self.repository.counts())["active_edges"], 1)

        async with self.session_factory() as session:
            rows = await session.execute(
                select(KnowledgeGraphEdgeRecord).where(
                    KnowledgeGraphEdgeRecord.dedupe_key == active[0].dedupe_key
                )
            )
            versions = list(rows.scalars().all())
        self.assertGreaterEqual(len(versions), 5)
        self.assertEqual(
            len([edge for edge in versions if edge.expired_at is None]),
            1,
        )

    async def test_automatic_relation_cannot_be_revised_or_retracted(self) -> None:
        await self.repository.apply_projection(
            [
                KnowledgeGraphNodeSpec(node_type="symbol", name="300059"),
                KnowledgeGraphNodeSpec(node_type="role", name="龙头"),
            ],
            [
                # A deterministic source fact must be corrected at its source or
                # via a future override operation, never rewritten in place.
                KnowledgeGraphEdgeSpec(
                    src=("symbol", "300059"),
                    dst=("role", "龙头"),
                    relation="has_role",
                    fact="东方财富当前角色：龙头",
                    dedupe_key="role|300059|龙头",
                    provenance="deterministic",
                    source_key="kb:symbols/roles.jsonl",
                    source_ref="kb:symbols/roles.jsonl",
                )
            ],
            now=self.now,
        )
        matches = await self.repository.find_nodes("300059")
        _, edges = await self.repository.neighborhood(matches[0].id, hops=1)

        with self.assertRaises(GraphSchemaValidationError):
            await self.service.apply_local_change(
                [
                    {
                        "op": "retract_relation",
                        "edge_id": edges[0].id,
                        "reason": "试图直接删除自动事实",
                    }
                ],
                summary="非法修改",
                expected_revision=0,
                actor_id="local-user",
                now=datetime(2026, 7, 18, 1, 1),
            )

    async def test_custom_schema_crud_and_relation_validation(self) -> None:
        entity_type = await self.service.apply_local_change(
            [
                {
                    "op": "upsert_schema_item",
                    "kind": "entity_type",
                    "key": "custom.indicator",
                    "expected_version": 0,
                    "definition": {
                        "label": "技术指标",
                        "parent_key": None,
                    },
                }
            ],
            summary="新增指标实体类型",
            expected_revision=0,
            actor_id="local-user",
            now=self.now,
        )
        self.assertEqual(entity_type["revision"], 1)
        await self.service.apply_local_change(
            [
                {
                    "op": "upsert_schema_item",
                    "kind": "relation_type",
                    "key": "custom.uses_indicator",
                    "expected_version": 0,
                    "definition": {
                        "label": "使用指标",
                        "source_type": "symbol",
                        "target_type": "custom.indicator",
                        "symmetric": False,
                        "transitive": False,
                    },
                }
            ],
            summary="新增指标关系",
            expected_revision=1,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 1),
        )
        schema = await self.service.get_schema()
        self.assertIn(
            "custom.indicator",
            {item["key"] for item in schema["entity_types"]},
        )
        self.assertIn(
            "custom.uses_indicator",
            {item["key"] for item in schema["relation_types"]},
        )

        created = await self.service.apply_local_change(
            [
                {
                    "op": "create_relation",
                    "source": {"type": "symbol", "name": "300059"},
                    "relation": "custom.uses_indicator",
                    "target": {
                        "type": "custom.indicator",
                        "name": "MACD",
                    },
                    "fact": "东方财富的复盘使用 MACD 指标。",
                }
            ],
            summary="写入自定义关系",
            expected_revision=2,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 2),
        )
        self.assertEqual(created["revision"], 3)

        updated = await self.service.apply_local_change(
            [
                {
                    "op": "upsert_schema_item",
                    "kind": "relation_type",
                    "key": "custom.uses_indicator",
                    "expected_version": 1,
                    "definition": {
                        "label": "复盘使用指标",
                        "source_type": "symbol",
                        "target_type": "custom.indicator",
                        "symmetric": False,
                        "transitive": False,
                    },
                }
            ],
            summary="修改关系标签",
            expected_revision=3,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 3),
        )
        self.assertEqual(
            updated["operations"][0]["schema_version"],
            2,
        )
        await self.service.apply_local_change(
            [
                {
                    "op": "deprecate_schema_item",
                    "kind": "relation_type",
                    "key": "custom.uses_indicator",
                    "expected_version": 2,
                }
            ],
            summary="弃用关系",
            expected_revision=4,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 4),
        )
        with self.assertRaises(GraphSchemaValidationError):
            await self.service.apply_local_change(
                [
                    {
                        "op": "create_relation",
                        "source": {"type": "symbol", "name": "600519"},
                        "relation": "custom.uses_indicator",
                        "target": {
                            "type": "custom.indicator",
                            "name": "RSI",
                        },
                        "fact": "贵州茅台使用 RSI 指标。",
                    }
                ],
                summary="使用已弃用关系",
                expected_revision=5,
                actor_id="local-user",
                now=datetime(2026, 7, 18, 1, 5),
            )
        await self.service.undo_revision(
            5,
            expected_revision=5,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 6),
        )
        relation = next(
            item
            for item in (await self.service.get_schema())["relation_types"]
            if item["key"] == "custom.uses_indicator"
        )
        self.assertEqual(relation["status"], "active")
        await self.service.redo_revision(
            5,
            expected_revision=6,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 7),
        )
        relation = next(
            item
            for item in (await self.service.get_schema())["relation_types"]
            if item["key"] == "custom.uses_indicator"
        )
        self.assertEqual(relation["status"], "deprecated")

    async def test_system_schema_and_custom_inheritance_cycles_are_rejected(
        self,
    ) -> None:
        with self.assertRaises(GraphSchemaValidationError):
            await self.service.apply_local_change(
                [
                    {
                        "op": "upsert_schema_item",
                        "kind": "entity_type",
                        "key": "symbol",
                        "expected_version": 0,
                        "definition": {"label": "覆盖系统股票类型"},
                    }
                ],
                summary="非法覆盖",
                expected_revision=0,
                actor_id="local-user",
                now=self.now,
            )

        await self.service.apply_local_change(
            [
                {
                    "op": "upsert_schema_item",
                    "kind": "entity_type",
                    "key": "custom.a",
                    "expected_version": 0,
                    "definition": {"label": "A", "parent_key": None},
                },
                {
                    "op": "upsert_schema_item",
                    "kind": "entity_type",
                    "key": "custom.b",
                    "expected_version": 0,
                    "definition": {"label": "B", "parent_key": "custom.a"},
                },
            ],
            summary="创建继承层级",
            expected_revision=0,
            actor_id="local-user",
            now=self.now,
        )
        with self.assertRaises(GraphSchemaValidationError):
            await self.service.apply_local_change(
                [
                    {
                        "op": "upsert_schema_item",
                        "kind": "entity_type",
                        "key": "custom.a",
                        "expected_version": 1,
                        "definition": {
                            "label": "A",
                            "parent_key": "custom.b",
                        },
                    }
                ],
                summary="制造继承环",
                expected_revision=1,
                actor_id="local-user",
                now=datetime(2026, 7, 18, 1, 1),
            )

    async def test_agent_custom_schema_draft_requires_approval(self) -> None:
        draft = await self.service.create_agent_draft(
            [
                {
                    "op": "upsert_schema_item",
                    "kind": "entity_type",
                    "key": "custom.pattern",
                    "expected_version": 0,
                    "definition": {"label": "形态", "parent_key": None},
                }
            ],
            summary="Agent 建议新增形态类型",
            actor_id="agent-1",
            now=self.now,
        )
        self.assertNotIn(
            "custom.pattern",
            {item["key"] for item in (await self.service.get_schema())["entity_types"]},
        )

        await self.service.approve_draft(
            draft["id"],
            proposal_hash=draft["proposal_hash"],
            resolver_id="local-user",
            decision_source="web",
            now=datetime(2026, 7, 18, 1, 1),
        )
        self.assertIn(
            "custom.pattern",
            {item["key"] for item in (await self.service.get_schema())["entity_types"]},
        )

    async def test_create_update_retire_entity_and_undo(self) -> None:
        created = await self.service.apply_local_change(
            [
                {
                    "op": "create_entity",
                    "type": "theme",
                    "name": "AI算力",
                    "display_name": "AI算力",
                    "attrs": {"note": "手工建实体"},
                }
            ],
            summary="创建主题实体",
            expected_revision=0,
            actor_id="local-user",
            now=self.now,
        )
        self.assertEqual(created["revision"], 1)
        matches = await self.repository.find_nodes("AI算力")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].status, "active")
        entity_id = matches[0].id

        await self.service.apply_local_change(
            [
                {
                    "op": "update_entity",
                    "entity_id": entity_id,
                    "display_name": "AI算力（修订）",
                    "attrs": {"note": "已改"},
                }
            ],
            summary="更新主题实体",
            expected_revision=1,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 1),
        )
        updated = await self.repository.find_nodes("AI算力")
        self.assertEqual(updated[0].display_name, "AI算力（修订）")

        await self.service.apply_local_change(
            [{"op": "retire_entity", "entity_id": entity_id, "reason": "过时"}],
            summary="退役主题实体",
            expected_revision=2,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 2),
        )
        self.assertEqual(await self.repository.find_nodes("AI算力"), [])

        undone = await self.service.undo_revision(
            3,
            expected_revision=3,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 3),
        )
        self.assertEqual(undone["revision"], 4)
        restored = await self.repository.find_nodes("AI算力")
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].status, "active")

    async def test_merge_entities_redirects_lookup(self) -> None:
        await self.service.apply_local_change(
            [
                {
                    "op": "create_entity",
                    "type": "theme",
                    "name": "券商A",
                },
                {
                    "op": "create_entity",
                    "type": "theme",
                    "name": "券商B",
                },
            ],
            summary="准备合并实体",
            expected_revision=0,
            actor_id="local-user",
            now=self.now,
        )
        left = (await self.repository.find_nodes("券商A"))[0]
        right = (await self.repository.find_nodes("券商B"))[0]
        await self.service.apply_local_change(
            [
                {
                    "op": "merge_entities",
                    "survivor_id": left.id,
                    "merge_ids": [right.id],
                    "reason": "同题材别名",
                }
            ],
            summary="合并题材",
            expected_revision=1,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 1),
        )
        redirected = await self.repository.find_nodes("券商B")
        self.assertEqual(len(redirected), 1)
        self.assertEqual(redirected[0].id, left.id)
        lineage = await self.service.list_lineage(left.id)
        self.assertEqual(lineage[0]["kind"], "merge")

    async def test_override_relation_replaces_automatic_fact(self) -> None:
        await self.repository.apply_projection(
            [
                KnowledgeGraphNodeSpec("symbol", "300059", display_name="东方财富"),
                KnowledgeGraphNodeSpec("theme", "券商"),
            ],
            [
                KnowledgeGraphEdgeSpec(
                    src=("symbol", "300059"),
                    dst=("theme", "券商"),
                    relation="belongs_to_theme",
                    fact="投影：东方财富属于券商。",
                    dedupe_key="det|300059|theme|券商",
                    provenance="deterministic",
                    source_key="db:roles",
                    source_ref="db:roles/1",
                )
            ],
            now=self.now,
        )
        matches = await self.repository.find_nodes("300059")
        _, edges = await self.repository.neighborhood(matches[0].id, hops=1)
        self.assertEqual(edges[0].provenance, "deterministic")

        result = await self.service.apply_local_change(
            [
                {
                    "op": "override_relation",
                    "edge_id": edges[0].id,
                    "fact": "人工覆盖：东方财富属于券商龙头题材。",
                    "confidence": 1.0,
                }
            ],
            summary="覆盖自动关系",
            expected_revision=0,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 1),
        )
        self.assertEqual(result["revision"], 1)
        _, after = await self.repository.neighborhood(matches[0].id, hops=1)
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0].provenance, "manual")
        self.assertIn("人工覆盖", after[0].fact)

    async def test_attach_detach_evidence_and_undo(self) -> None:
        await self.service.apply_local_change(
            [
                {
                    "op": "create_entity",
                    "type": "theme",
                    "name": "机器人",
                }
            ],
            summary="建实体",
            expected_revision=0,
            actor_id="local-user",
            now=self.now,
        )
        entity_id = (await self.repository.find_nodes("机器人"))[0].id
        attached = await self.service.apply_local_change(
            [
                {
                    "op": "attach_evidence",
                    "target_kind": "node",
                    "target_id": entity_id,
                    "kind": "kb_ref",
                    "uri": "kb:journals/2026-07-18.md",
                    "excerpt": "机器人题材启动",
                }
            ],
            summary="绑定溯源",
            expected_revision=1,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 1),
        )
        evidence = await self.service.list_evidence("node", entity_id)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["status"], "active")
        evidence_id = evidence[0]["id"]

        await self.service.apply_local_change(
            [{"op": "detach_evidence", "evidence_id": evidence_id}],
            summary="解除溯源",
            expected_revision=2,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 2),
        )
        self.assertEqual(await self.service.list_evidence("node", entity_id), [])

        await self.service.undo_revision(
            3,
            expected_revision=3,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 3),
        )
        restored = await self.service.list_evidence("node", entity_id)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["id"], evidence_id)
        self.assertEqual(attached["revision"], 2)

    async def test_save_layout_versions_and_conflict_dismiss(self) -> None:
        await self.service.apply_local_change(
            [
                {
                    "op": "create_entity",
                    "type": "theme",
                    "name": "布局主题",
                }
            ],
            summary="建实体",
            expected_revision=0,
            actor_id="local-user",
            now=self.now,
        )
        entity_id = (await self.repository.find_nodes("布局主题"))[0].id
        await self.service.apply_local_change(
            [
                {
                    "op": "save_layout",
                    "scope_key": entity_id,
                    "positions": {entity_id: {"x": 10, "y": 20}},
                    "locked_ids": [entity_id],
                    "highlight_ids": [],
                }
            ],
            summary="保存布局 v1",
            expected_revision=1,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 1),
        )
        await self.service.apply_local_change(
            [
                {
                    "op": "save_layout",
                    "scope_key": entity_id,
                    "positions": {entity_id: {"x": 30, "y": 40}},
                    "locked_ids": [],
                    "highlight_ids": [entity_id],
                }
            ],
            summary="保存布局 v2",
            expected_revision=2,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 2),
        )
        layout = await self.service.get_latest_layout(entity_id)
        self.assertEqual(layout["version"], 2)
        self.assertEqual(layout["positions"][entity_id]["x"], 30)

        conflict = await self.service.record_conflict(
            conflict_type="manual_vs_auto",
            subject_key="demo-conflict",
            left={"fact": "a"},
            right={"fact": "b"},
            now=datetime(2026, 7, 18, 1, 3),
        )
        resolved = await self.service.apply_local_change(
            [
                {
                    "op": "resolve_conflict",
                    "conflict_id": conflict["id"],
                    "decision": "dismiss",
                }
            ],
            summary="忽略冲突",
            expected_revision=3,
            actor_id="local-user",
            now=datetime(2026, 7, 18, 1, 4),
        )
        self.assertEqual(resolved["revision"], 4)
        open_items = await self.service.list_conflicts(status="open")
        self.assertEqual(open_items, [])
        dismissed = await self.service.list_conflicts(status="dismissed")
        self.assertEqual(len(dismissed), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
