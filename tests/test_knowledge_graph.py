"""知识图谱：确定性投影 / bi-temporal 失效 / 查询渲染 / in-process 工具。

覆盖的契约：

- ``build_deterministic_projection``：roles / sentiment / trades /
  decision_signals 四来源 → 节点与边意图；脏行进 ``warnings``（不静默）。
- ``SqlAlchemyKnowledgeGraphRepository.apply_projection``：幂等（重放
  unchanged）、内容变更 expire+insert、``state_key`` 单值状态组失效
  （角色变更史保留为 expired 边）。
- ``sync_deterministic_projection``：content_hash 水位（未变跳过 /
  force 重放）。
- ``find_nodes`` / ``neighborhood`` / ``render_neighborhood_markdown``：
  名称解析、多跳、历史回溯、截断提示。
- ``KnowledgeGraphTool``：unwired / missing_entity / entity_not_found
  结构化错误 + query / sync 正常路径。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from doyoutrade.knowledge.graph import (
    NODE_CYCLE,
    NODE_ROLE,
    NODE_SYMBOL,
    REL_HAS_ROLE,
    build_deterministic_projection,
    render_neighborhood_markdown,
    sync_deterministic_projection,
)
from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.models import (
    Base,
    DecisionSignalRecord,
    KnowledgeGraphEdgeRecord,
    KnowledgeGraphSourceStateRecord,
)
from doyoutrade.persistence.repositories import (
    KnowledgeGraphEdgeSpec,
    KnowledgeGraphNodeSpec,
    SqlAlchemyKnowledgeGraphRepository,
)


def _write_kb(root: Path, *, role: str = "龙头", note: str | None = "券商情绪核心") -> None:
    (root / "symbols").mkdir(parents=True, exist_ok=True)
    (root / "cycles" / "2026-03").mkdir(parents=True, exist_ok=True)
    (root / "trades" / "华泰").mkdir(parents=True, exist_ok=True)
    (root / "symbols" / "roles.jsonl").write_text(
        json.dumps(
            {
                "symbol": "300059",
                "name": "东方财富",
                "role": role,
                "note": note,
                "strategy_hint": "打板",
                "updated_at": "2026-03-10T09:00:00",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "cycles" / "2026-03" / "_sentiment.jsonl").write_text(
        json.dumps(
            {
                "date": "2026-03-10",
                "label": "高潮",
                "limit_up_count": 120,
                "limit_down_count": 3,
                "broken_board_count": 10,
                "broken_board_rate": 0.08,
                "max_streak": 7,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "trades" / "华泰" / "2026-03.csv").write_text(
        "成交日期,证券代码,证券名称,操作,成交价格,成交数量\n"
        "2026-03-09,300059,东方财富,买入,20.00,1000\n"
        "2026-03-11,300059,东方财富,卖出,22.50,1000\n",
        encoding="utf-8",
    )


class ProjectionBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.kb = Path(self.tempdir.name) / "knowledge"
        _write_kb(self.kb)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_projects_all_deterministic_sources(self) -> None:
        projection = build_deterministic_projection(
            self.kb,
            decision_signal_rows=[
                {
                    "id": "dsig-1",
                    "symbol": "300059",
                    "action": "buy",
                    "source": "assistant",
                    "confidence": 0.8,
                    "horizon": "5d",
                    "reason": "首板确认",
                    "status": "active",
                    "created_at": datetime(2026, 3, 10, 1, 0),
                    "outcomes": [{"horizon": "5d", "outcome": "hit", "return_pct": 12.5,
                                  "anchor_date": "2026-03-10"}],
                }
            ],
        )
        node_keys = {(n.node_type, n.name) for n in projection.nodes}
        self.assertIn((NODE_SYMBOL, "300059"), node_keys)
        self.assertIn((NODE_ROLE, "龙头"), node_keys)
        self.assertIn((NODE_CYCLE, "2026-03"), node_keys)
        self.assertIn(("signal", "dsig-1"), node_keys)
        relations = sorted(e.relation for e in projection.edges)
        self.assertEqual(relations, ["has_role", "signals", "traded_in"])
        self.assertEqual(projection.warnings, [])
        # 角色边必须携带单值状态组键（失效语义的载体）与来源。
        role_edge = next(e for e in projection.edges if e.relation == REL_HAS_ROLE)
        self.assertEqual(role_edge.state_key, "role|300059")
        self.assertEqual(role_edge.dedupe_key, "role|300059|龙头")
        self.assertIn("hit", next(e for e in projection.edges if e.relation == "signals").fact)
        # 四个来源都要有 content_hash 水位。
        self.assertEqual(len(projection.source_hashes), 4)

    def test_dirty_rows_surface_as_warnings_not_silently_dropped(self) -> None:
        (self.kb / "symbols" / "roles.jsonl").write_text(
            json.dumps({"symbol": "300059", "role": ""}) + "\n", encoding="utf-8"
        )
        projection = build_deterministic_projection(self.kb)
        reasons = [w["reason"] for w in projection.warnings]
        self.assertIn("role_row_missing_symbol_or_role", reasons)
        self.assertEqual([e for e in projection.edges if e.relation == REL_HAS_ROLE], [])

    def test_signal_watermark_hash_covers_every_projected_field(self) -> None:
        base = {
            "id": "dsig-hash",
            "symbol": "300059",
            "action": "buy",
            "source": "assistant",
            "confidence": 0.8,
            "horizon": "5d",
            "reason": "首板确认",
            "status": "active",
            "created_at": datetime(2026, 3, 10, 1, 0),
            "outcomes": [],
        }
        baseline = build_deterministic_projection(
            self.kb, decision_signal_rows=[base]
        ).source_hashes["db:decision_signals"]

        changes = {
            "source": "manual",
            "confidence": 0.6,
            "horizon": "10d",
            "reason": "理由已修订",
            "created_at": datetime(2026, 3, 11, 1, 0),
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                changed = build_deterministic_projection(
                    self.kb,
                    decision_signal_rows=[{**base, field: value}],
                ).source_hashes["db:decision_signals"]
                self.assertNotEqual(changed, baseline)


class KnowledgeGraphRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyKnowledgeGraphRepository(self.session_factory)
        self.kb = Path(self.tempdir.name) / "knowledge"
        _write_kb(self.kb)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_apply_projection_is_idempotent(self) -> None:
        now = datetime(2026, 7, 17, 10, 0)
        projection = build_deterministic_projection(self.kb)
        first = await self.repo.apply_projection(projection.nodes, projection.edges, now=now)
        self.assertEqual(first["edges_created"], 2)  # has_role + traded_in
        self.assertGreaterEqual(first["nodes_created"], 3)
        replay = await self.repo.apply_projection(
            projection.nodes, projection.edges, now=datetime(2026, 7, 17, 10, 5)
        )
        self.assertEqual(replay["edges_created"], 0)
        self.assertEqual(replay["edges_unchanged"], 2)
        self.assertEqual(replay["edges_expired"], 0)

    async def test_state_key_change_expires_old_edge_and_keeps_history(self) -> None:
        p1 = build_deterministic_projection(self.kb)
        await self.repo.apply_projection(p1.nodes, p1.edges, now=datetime(2026, 7, 17, 10, 0))
        _write_kb(self.kb, role="杂毛", note="退潮补跌")
        p2 = build_deterministic_projection(self.kb)
        stats = await self.repo.apply_projection(
            p2.nodes, p2.edges, now=datetime(2026, 7, 17, 11, 0)
        )
        self.assertEqual(stats["edges_expired"], 1)
        self.assertEqual(stats["edges_created"], 1)
        matches = await self.repo.find_nodes("300059")
        nodes, edges = await self.repo.neighborhood(
            matches[0].id, hops=1, include_expired=True
        )
        role_edges = [e for e in edges if e.relation == REL_HAS_ROLE]
        active = [e for e in role_edges if e.expired_at is None]
        expired = [e for e in role_edges if e.expired_at is not None]
        self.assertEqual(len(active), 1)
        self.assertIn("杂毛", active[0].fact)
        self.assertEqual(len(expired), 1)
        self.assertIn("龙头", expired[0].fact)
        self.assertEqual(expired[0].expired_at, datetime(2026, 7, 17, 11, 0))

    async def test_edge_missing_endpoint_node_raises(self) -> None:
        edge = KnowledgeGraphEdgeSpec(
            src=("symbol", "300059"),
            dst=("role", "龙头"),
            relation="has_role",
            fact="x",
            dedupe_key="role|300059|龙头",
        )
        with self.assertRaises(ValueError) as ctx:
            await self.repo.apply_projection(
                [KnowledgeGraphNodeSpec(node_type="symbol", name="300059")],
                [edge],
                now=datetime(2026, 7, 17, 10, 0),
            )
        self.assertIn("not part of this projection batch", str(ctx.exception))

    async def test_sync_watermarks_skip_and_force(self) -> None:
        first = await sync_deterministic_projection(
            self.repo, self.kb, now=datetime(2026, 7, 17, 10, 0)
        )
        self.assertFalse(first["skipped"])
        second = await sync_deterministic_projection(
            self.repo, self.kb, now=datetime(2026, 7, 17, 10, 5)
        )
        self.assertTrue(second["skipped"])
        forced = await sync_deterministic_projection(
            self.repo, self.kb, now=datetime(2026, 7, 17, 10, 6), force=True
        )
        self.assertFalse(forced["skipped"])
        self.assertEqual(forced["apply"]["edges_created"], 0)
        self.assertEqual(forced["apply"]["edges_expired"], 0)

    async def test_sync_projects_decision_signals_from_db(self) -> None:
        async with self.session_factory() as session:
            session.add(
                DecisionSignalRecord(
                    id="dsig-t1",
                    source="assistant",
                    symbol="300059",
                    action="buy",
                    horizon="5d",
                    reason="首板确认",
                    status="active",
                    dedupe_key="t|300059|buy|5d",
                    created_at=datetime(2026, 3, 10, 1, 0),
                    updated_at=datetime(2026, 3, 10, 1, 0),
                )
            )
            await session.commit()
        result = await sync_deterministic_projection(
            self.repo, self.kb, now=datetime(2026, 7, 17, 10, 0)
        )
        self.assertFalse(result["skipped"])
        matches = await self.repo.find_nodes("dsig-t1")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].node_type, "signal")

    async def test_sync_expires_facts_removed_from_their_source_snapshot(self) -> None:
        await sync_deterministic_projection(
            self.repo, self.kb, now=datetime(2026, 7, 17, 10, 0)
        )
        (self.kb / "symbols" / "roles.jsonl").write_text("", encoding="utf-8")
        (self.kb / "trades" / "华泰" / "2026-03.csv").write_text(
            "成交日期,证券代码,证券名称,操作,成交价格,成交数量\n",
            encoding="utf-8",
        )

        result = await sync_deterministic_projection(
            self.repo, self.kb, now=datetime(2026, 7, 17, 11, 0)
        )

        self.assertFalse(result["skipped"])
        self.assertEqual(result["apply"]["edges_expired"], 2)
        matches = await self.repo.find_nodes("300059")
        _, edges = await self.repo.neighborhood(
            matches[0].id, hops=1, include_expired=True
        )
        self.assertEqual([edge for edge in edges if edge.expired_at is None], [])
        self.assertEqual(
            {edge.relation for edge in edges if edge.expired_at is not None},
            {"has_role", "traded_in"},
        )

    async def test_database_rejects_duplicate_active_dedupe_key(self) -> None:
        projection = build_deterministic_projection(self.kb)
        await self.repo.apply_projection(
            projection.nodes, projection.edges, now=datetime(2026, 7, 17, 10, 0)
        )

        async with self.session_factory() as session:
            result = await session.execute(
                select(KnowledgeGraphEdgeRecord).where(
                    KnowledgeGraphEdgeRecord.expired_at.is_(None)
                )
            )
            existing = result.scalars().first()
            self.assertIsNotNone(existing)
            session.add(
                KnowledgeGraphEdgeRecord(
                    id="kge-duplicate-active",
                    src_id=existing.src_id,
                    dst_id=existing.dst_id,
                    relation=existing.relation,
                    fact=existing.fact,
                    attrs=existing.attrs,
                    dedupe_key=existing.dedupe_key,
                    state_key=existing.state_key,
                    provenance=existing.provenance,
                    confidence=existing.confidence,
                    source_ref=existing.source_ref,
                    valid_at=existing.valid_at,
                    invalid_at=existing.invalid_at,
                    created_at=datetime(2026, 7, 17, 10, 1),
                    expired_at=None,
                )
            )
            with self.assertRaises(IntegrityError):
                await session.commit()

    async def test_projection_rolls_back_when_watermark_write_fails(self) -> None:
        projection = build_deterministic_projection(self.kb)

        def _fail_watermark(*_args) -> None:
            raise RuntimeError("injected watermark failure")

        event.listen(
            KnowledgeGraphSourceStateRecord,
            "before_insert",
            _fail_watermark,
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "watermark failure"):
                await self.repo.apply_projection(
                    projection.nodes,
                    projection.edges,
                    now=datetime(2026, 7, 17, 10, 0),
                    reconcile_source_keys=set(projection.source_hashes),
                    source_hashes=projection.source_hashes,
                )
        finally:
            event.remove(
                KnowledgeGraphSourceStateRecord,
                "before_insert",
                _fail_watermark,
            )

        self.assertEqual(
            await self.repo.counts(),
            {"nodes": 0, "active_edges": 0, "expired_edges": 0},
        )

    async def test_find_nodes_exact_match_ranks_before_fuzzy(self) -> None:
        p = build_deterministic_projection(self.kb)
        await self.repo.apply_projection(p.nodes, p.edges, now=datetime(2026, 7, 17, 10, 0))
        by_name = await self.repo.find_nodes("东方财富")
        self.assertEqual(by_name[0].name, "300059")
        by_fuzzy = await self.repo.find_nodes("东方")
        self.assertTrue(any(n.name == "300059" for n in by_fuzzy))
        with self.assertRaises(ValueError):
            await self.repo.find_nodes("   ")

    async def test_render_neighborhood_markdown_shape(self) -> None:
        p = build_deterministic_projection(self.kb)
        await self.repo.apply_projection(p.nodes, p.edges, now=datetime(2026, 7, 17, 10, 0))
        matches = await self.repo.find_nodes("300059")
        nodes, edges = await self.repo.neighborhood(matches[0].id, hops=2)
        md = render_neighborhood_markdown(matches[0], nodes, edges, truncated=True)
        self.assertIn("# 知识图谱：东方财富(300059)（symbol）", md)
        self.assertIn("## has_role", md)
        self.assertIn("kb:symbols/roles.jsonl", md)
        self.assertIn("邻域边数超出上限", md)  # 截断必须明示


class KnowledgeGraphToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyKnowledgeGraphRepository(self.session_factory)
        self.kb = Path(self.tempdir.name) / "knowledge"
        _write_kb(self.kb)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    def _tool(self, repo=None):
        from doyoutrade.tools.knowledge_graph import KnowledgeGraphTool

        return KnowledgeGraphTool(knowledge_graph_repository=repo)

    async def test_unwired_runtime_returns_structured_error(self) -> None:
        result = await self._tool(repo=None).execute(action="query", entity="300059")
        self.assertTrue(result.is_error)
        self.assertIn("knowledge_graph_unwired", result.text)

    async def test_unknown_kwargs_rejected(self) -> None:
        result = await self._tool(self.repo).execute(entities="300059")
        self.assertTrue(result.is_error)
        self.assertIn("unknown", result.text.lower())

    async def test_query_requires_entity(self) -> None:
        result = await self._tool(self.repo).execute(action="query")
        self.assertTrue(result.is_error)
        self.assertIn("missing_entity", result.text)

    async def test_query_unknown_entity_hints_sync(self) -> None:
        result = await self._tool(self.repo).execute(entity="不存在的实体")
        self.assertTrue(result.is_error)
        self.assertIn("entity_not_found", result.text)
        self.assertIn("sync", result.text)

    async def test_query_happy_path_after_system_sync(self) -> None:
        await sync_deterministic_projection(
            self.repo,
            self.kb,
            now=datetime(2026, 7, 17, 10, 0),
        )
        tool = self._tool(self.repo)
        queried = await tool.execute(entity="东方财富", hops=2, include_expired=True)
        self.assertFalse(queried.is_error)
        self.assertIn("has_role", queried.text)
        self.assertIn("traded_in", queried.text)
        self.assertIn("kb:trades", queried.text)

    async def test_agent_cannot_directly_sync_the_graph(self) -> None:
        result = await self._tool(self.repo).execute(action="sync")

        self.assertTrue(result.is_error)
        self.assertIn("validation_error", result.text)

    async def test_agent_propose_creates_pending_change_without_mutation(self) -> None:
        result = await self._tool(self.repo).execute(
            action="propose",
            summary="补充东方财富题材",
            operations=[
                {
                    "op": "create_relation",
                    "source": {
                        "type": "symbol",
                        "name": "300059",
                        "display_name": "东方财富",
                    },
                    "relation": "belongs_to_theme",
                    "target": {"type": "theme", "name": "券商"},
                    "fact": "东方财富属于券商题材。",
                    "confidence": 1.0,
                }
            ],
            session_id="agent-session-1",
        )

        self.assertFalse(result.is_error)
        self.assertIn("待人工审批", result.text)
        self.assertEqual((await self.repo.counts())["active_edges"], 0)
        from doyoutrade.knowledge.editing import KnowledgeGraphCommandService

        pending = await KnowledgeGraphCommandService(
            self.session_factory
        ).list_change_sets(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["actor_type"], "agent")

    async def test_query_hops_out_of_range_rejected(self) -> None:
        result = await self._tool(self.repo).execute(entity="300059", hops=9)
        self.assertTrue(result.is_error)
        self.assertIn("validation_error", result.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
