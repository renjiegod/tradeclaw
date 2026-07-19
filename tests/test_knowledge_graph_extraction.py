"""知识图谱 LLM 抽取：解析校验 / 实体解析 / 幂等落库 / 水位 / 执行器软失败。

覆盖的契约：

- ``_parse_extraction_json``：严格 JSON → ``{...}`` 子串回退 → 不可解析
  返回 None（不做 json-repair）。
- ``extract_graph_candidates``：模型异常 → ``model_error``；坏输出 →
  ``extract_parse_failed``；坏候选行进 ``warnings`` 不静默。
- ``resolve_and_apply_candidates``：股票名经图内节点精确解析；未解析实体
  的边跳过并带 ``symbol_unresolved`` warning；``provenance='llm'`` 落库
  幂等；fact 变化 expire+insert。
- ``extract_and_apply``：content_hash 水位（同文本零模型成本跳过 /
  force 绕过）。
- ``DailyReviewExecutor._extract_kg_candidates``：未装配 → skipped；
  异常 → 结构化 error，永不 raise。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from doyoutrade.knowledge.graph_extraction import (
    _parse_extraction_json,
    _validate_candidate,
    build_extraction_system_prompt,
    build_extraction_vocabulary,
    extract_and_apply,
    extract_graph_candidates,
    resolve_and_apply_candidates,
)
from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.models import Base
from doyoutrade.persistence.repositories import (
    KnowledgeGraphNodeSpec,
    SqlAlchemyKnowledgeGraphRepository,
)


class FakeAdapter:
    """Canned-response adapter matching the ``generate(request).text`` shape."""

    def __init__(self, text: str | None = None, error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list = []

    def generate(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


def _edges_json(*edges: dict) -> str:
    return json.dumps({"edges": list(edges)}, ensure_ascii=False)


_GOOD_EDGE = {
    "src_type": "symbol", "src": "300059", "relation": "leads_theme",
    "dst_type": "theme", "dst": "券商",
    "fact": "东方财富是本轮券商行情的龙头", "confidence": 0.9,
    "valid_from": "2026-03-10", "valid_to": None,
}


class ParseAndValidateTests(unittest.TestCase):
    def test_parse_strict_then_substring_fallback(self) -> None:
        body = _edges_json(_GOOD_EDGE)
        self.assertIsNotNone(_parse_extraction_json(body))
        fenced = f"好的，抽取结果如下：\n```json\n{body}\n```"
        self.assertIsNotNone(_parse_extraction_json(fenced))
        self.assertIsNone(_parse_extraction_json("完全不是 JSON"))
        self.assertIsNone(_parse_extraction_json('{"not_edges": []}'))

    def test_validate_rejects_bad_candidates_with_reasons(self) -> None:
        cases = {
            "unknown_relation": {**_GOOD_EDGE, "relation": "made_up"},
            "relation_type_mismatch": {**_GOOD_EDGE, "dst_type": "cycle"},
            "missing_src_dst_or_fact": {**_GOOD_EDGE, "fact": " "},
            "bad_confidence": {**_GOOD_EDGE, "confidence": 1.5},
            "bad_cycle_format": {
                "src_type": "theme", "src": "券商", "relation": "observed_in",
                "dst_type": "cycle", "dst": "2026年3月",
                "fact": "券商活跃于三月", "confidence": 0.8,
            },
            "self_edge": {
                "src_type": "symbol", "src": "300059", "relation": "linked_with",
                "dst_type": "symbol", "dst": "300059",
                "fact": "自己联动自己", "confidence": 0.8,
            },
        }
        for expected_reason, raw in cases.items():
            candidate, warning = _validate_candidate(raw, 0)
            self.assertIsNone(candidate, expected_reason)
            self.assertEqual(warning["reason"], expected_reason)

    def test_validate_accepts_good_candidate(self) -> None:
        candidate, warning = _validate_candidate(_GOOD_EDGE, 0)
        self.assertIsNone(warning)
        self.assertEqual(candidate["relation"], "leads_theme")
        self.assertEqual(candidate["valid_from"], datetime(2026, 3, 10))
        self.assertIsNone(candidate["valid_to"])


_CUSTOM_SCHEMA = {
    "entity_types": [
        {
            "key": "custom.sector",
            "label": "行业",
            "namespace": "custom",
            "status": "active",
        },
        {
            "key": "custom.retired",
            "label": "作废类型",
            "namespace": "custom",
            "status": "deprecated",
        },
    ],
    "relation_types": [
        {
            "key": "custom.rivals",
            "label": "同业竞对",
            "namespace": "custom",
            "status": "active",
            "source_type": "symbol",
            "target_type": "custom.sector",
        },
        {
            "key": "custom.dangling",
            "label": "悬空关系",
            "namespace": "custom",
            "status": "active",
            "source_type": "symbol",
            "target_type": "custom.unknown",
        },
    ],
}


class ExtractionVocabularyTests(unittest.TestCase):
    def test_system_default_vocabulary_matches_hardcoded(self) -> None:
        vocab = build_extraction_vocabulary(None)
        self.assertEqual(
            sorted(vocab.relations),
            ["belongs_to_theme", "leads_theme", "linked_with",
             "observed_in", "uses_playbook"],
        )
        self.assertIn("symbol", vocab.node_types)
        self.assertNotIn("custom.sector", vocab.node_types)

    def test_custom_schema_extends_vocabulary(self) -> None:
        vocab = build_extraction_vocabulary(_CUSTOM_SCHEMA)
        # active custom entity + relation join the vocabulary…
        self.assertIn("custom.sector", vocab.node_types)
        self.assertEqual(vocab.relations["custom.rivals"], ("symbol", "custom.sector"))
        # …deprecated entity and relations with an unknown endpoint are skipped.
        self.assertNotIn("custom.retired", vocab.node_types)
        self.assertNotIn("custom.dangling", vocab.relations)

    def test_custom_labels_appear_in_prompt(self) -> None:
        vocab = build_extraction_vocabulary(_CUSTOM_SCHEMA)
        prompt = build_extraction_system_prompt("2026-07-19", vocab)
        self.assertIn("行业", prompt)
        self.assertIn("同业竞对", prompt)
        self.assertIn("custom.rivals", prompt)
        # 系统词表仍在
        self.assertIn("leads_theme", prompt)

    def test_custom_relation_candidate_validates_with_vocabulary(self) -> None:
        vocab = build_extraction_vocabulary(_CUSTOM_SCHEMA)
        raw = {
            "src_type": "symbol", "src": "300059", "relation": "custom.rivals",
            "dst_type": "custom.sector", "dst": "互联网金融",
            "fact": "东方财富归属互联网金融行业", "confidence": 0.8,
            "valid_from": None, "valid_to": None,
        }
        # 系统词表下未知关系
        rejected, warning = _validate_candidate(raw, 0)
        self.assertIsNone(rejected)
        self.assertEqual(warning["reason"], "unknown_relation")
        # 自定义词表下通过
        candidate, warning = _validate_candidate(raw, 0, vocab)
        self.assertIsNotNone(candidate)
        self.assertIsNone(warning)
        self.assertEqual(candidate["relation"], "custom.rivals")


class ExtractCandidatesTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_error_is_structured(self) -> None:
        result = await extract_graph_candidates(
            FakeAdapter(error=RuntimeError("boom")), "复盘文本", reference_date="2026-07-17"
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "model_error")

    async def test_unparseable_output_is_structured(self) -> None:
        result = await extract_graph_candidates(
            FakeAdapter(text="我无法输出 JSON"), "复盘文本", reference_date="2026-07-17"
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "extract_parse_failed")

    async def test_empty_text_skips_model_call(self) -> None:
        adapter = FakeAdapter(text="{}")
        result = await extract_graph_candidates(adapter, "  ", reference_date="2026-07-17")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(adapter.calls, [])

    async def test_bad_rows_become_warnings(self) -> None:
        body = _edges_json(_GOOD_EDGE, {**_GOOD_EDGE, "relation": "nonsense"}, "not-a-dict")
        result = await extract_graph_candidates(
            FakeAdapter(text=body), "复盘文本", reference_date="2026-07-17"
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["candidates"]), 1)
        reasons = {w["reason"] for w in result["warnings"]}
        self.assertEqual(reasons, {"unknown_relation", "candidate_not_object"})


class _RepoTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyKnowledgeGraphRepository(self.session_factory)

    async def asyncTearDown(self) -> None:
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def _seed_symbol_node(self) -> None:
        await self.repo.apply_projection(
            [KnowledgeGraphNodeSpec(node_type="symbol", name="300059",
                                    display_name="东方财富")],
            [],
            now=datetime(2026, 7, 17, 9, 0),
        )


class ResolveAndApplyTests(_RepoTestCase):
    async def test_symbol_name_resolves_via_existing_graph_node(self) -> None:
        await self._seed_symbol_node()
        candidate, _ = _validate_candidate({**_GOOD_EDGE, "src": "东方财富"}, 0)
        result = await resolve_and_apply_candidates(
            self.repo, [candidate], now=datetime(2026, 7, 17, 10, 0),
            source_ref="kb:journal/2026/2026-07-17.md",
        )
        self.assertEqual(result["apply"]["edges_created"], 1)
        self.assertEqual(result["warnings"], [])
        matches = await self.repo.find_nodes("300059")
        nodes, edges = await self.repo.neighborhood(matches[0].id, hops=1)
        llm_edges = [e for e in edges if e.provenance == "llm"]
        self.assertEqual(len(llm_edges), 1)
        self.assertEqual(llm_edges[0].dedupe_key, "llm|leads_theme|300059|券商")
        self.assertEqual(llm_edges[0].confidence, 0.9)
        self.assertEqual(llm_edges[0].source_ref, "kb:journal/2026/2026-07-17.md")

    async def test_unresolved_symbol_drops_edge_with_warning(self) -> None:
        candidate, _ = _validate_candidate({**_GOOD_EDGE, "src": "不存在的公司"}, 0)
        result = await resolve_and_apply_candidates(
            self.repo, [candidate], now=datetime(2026, 7, 17, 10, 0),
            source_ref="kb:x.md",
        )
        self.assertEqual(result["edges_submitted"], 0)
        self.assertEqual(result["warnings"][0]["reason"], "symbol_unresolved")

    async def test_reapply_is_idempotent_and_fact_change_expires(self) -> None:
        await self._seed_symbol_node()
        candidate, _ = _validate_candidate(_GOOD_EDGE, 0)
        first = await resolve_and_apply_candidates(
            self.repo, [candidate], now=datetime(2026, 7, 17, 10, 0), source_ref="kb:a.md"
        )
        self.assertEqual(first["apply"]["edges_created"], 1)
        replay = await resolve_and_apply_candidates(
            self.repo, [candidate], now=datetime(2026, 7, 17, 10, 5), source_ref="kb:a.md"
        )
        self.assertEqual(replay["apply"]["edges_unchanged"], 1)
        self.assertEqual(replay["apply"]["edges_created"], 0)
        changed, _ = _validate_candidate(
            {**_GOOD_EDGE, "fact": "东方财富已从券商龙头位置退下", "confidence": 0.7}, 0
        )
        third = await resolve_and_apply_candidates(
            self.repo, [changed], now=datetime(2026, 7, 18, 10, 0), source_ref="kb:b.md"
        )
        self.assertEqual(third["apply"]["edges_expired"], 1)
        self.assertEqual(third["apply"]["edges_created"], 1)

    async def test_batch_duplicate_keeps_higher_confidence(self) -> None:
        await self._seed_symbol_node()
        low, _ = _validate_candidate({**_GOOD_EDGE, "confidence": 0.5}, 0)
        high, _ = _validate_candidate(_GOOD_EDGE, 1)
        result = await resolve_and_apply_candidates(
            self.repo, [low, high], now=datetime(2026, 7, 17, 10, 0), source_ref="kb:a.md"
        )
        self.assertEqual(result["edges_submitted"], 1)
        matches = await self.repo.find_nodes("券商")
        _, edges = await self.repo.neighborhood(matches[0].id, hops=1)
        self.assertEqual(edges[0].confidence, 0.9)


class ExtractAndApplyWatermarkTests(_RepoTestCase):
    async def test_same_text_skips_second_model_call(self) -> None:
        await self._seed_symbol_node()
        adapter = FakeAdapter(text=_edges_json(_GOOD_EDGE))
        kwargs = dict(
            reference_date="2026-07-17",
            source_ref="kb:journal/2026/2026-07-17.md",
        )
        first = await extract_and_apply(
            self.repo, adapter, "今日复盘……", now=datetime(2026, 7, 17, 10, 0), **kwargs
        )
        self.assertEqual(first["status"], "ok")
        self.assertFalse(first["skipped"])
        self.assertEqual(first["candidate_count"], 1)
        self.assertEqual(len(adapter.calls), 1)

        second = await extract_and_apply(
            self.repo, adapter, "今日复盘……", now=datetime(2026, 7, 17, 11, 0), **kwargs
        )
        self.assertTrue(second["skipped"])
        self.assertEqual(len(adapter.calls), 1)  # 零模型成本

        forced = await extract_and_apply(
            self.repo, adapter, "今日复盘……", now=datetime(2026, 7, 17, 12, 0),
            force=True, **kwargs
        )
        self.assertFalse(forced["skipped"])
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(forced["apply"]["edges_unchanged"], 1)

    async def test_model_error_does_not_advance_watermark(self) -> None:
        adapter = FakeAdapter(error=RuntimeError("boom"))
        result = await extract_and_apply(
            self.repo, adapter, "今日复盘……", now=datetime(2026, 7, 17, 10, 0),
            reference_date="2026-07-17", source_ref="kb:j.md",
        )
        self.assertEqual(result["status"], "error")
        self.assertIsNone(await self.repo.get_source_state("kb:j.md"))

    async def test_empty_reextract_expires_prior_source_facts(self) -> None:
        await self._seed_symbol_node()
        source_ref = "kb:journal/2026/2026-07-17.md"
        first = await extract_and_apply(
            self.repo,
            FakeAdapter(text=_edges_json(_GOOD_EDGE)),
            "东方财富是本轮券商龙头。",
            now=datetime(2026, 7, 17, 10, 0),
            reference_date="2026-07-17",
            source_ref=source_ref,
        )
        self.assertEqual(first["apply"]["edges_created"], 1)

        second = await extract_and_apply(
            self.repo,
            FakeAdapter(text=_edges_json()),
            "今日没有可确认的图谱事实。",
            now=datetime(2026, 7, 18, 10, 0),
            reference_date="2026-07-18",
            source_ref=source_ref,
        )

        self.assertEqual(second["apply"]["edges_expired"], 1)
        matches = await self.repo.find_nodes("300059")
        _, edges = await self.repo.neighborhood(
            matches[0].id, hops=1, include_expired=True
        )
        self.assertEqual([edge for edge in edges if edge.expired_at is None], [])
        self.assertEqual(len(edges), 1)


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict = {}

    def set_attribute(self, key, value) -> None:
        self.attributes[key] = value


class DailyReviewHookTests(_RepoTestCase):
    def _executor(self, **overrides):
        from doyoutrade.assistant.cron_executors.daily_review import DailyReviewExecutor

        async def _factory(route_name):
            return overrides.get("adapter") or FakeAdapter(text=_edges_json(_GOOD_EDGE))

        kwargs = dict(
            assistant_service=SimpleNamespace(agent_repo=None),
            cron_job_repository=None,
            statement_provider=lambda *a, **k: {},
            knowledge_graph_repository=overrides.get(
                "kg_repo", self.repo if not overrides.get("unwired") else None
            ),
            model_adapter_factory=None if overrides.get("unwired") else _factory,
            instrument_catalog_repository=None,
        )
        return DailyReviewExecutor(**kwargs)

    async def test_unwired_returns_structured_skip(self) -> None:
        executor = self._executor(unwired=True)
        span = _FakeSpan()
        result = await executor._extract_kg_candidates(
            job_id="cron-1", agent_id=None, asof=date(2026, 7, 17),
            reply_text="复盘", journal_path="journal/2026/2026-07-17.md", span=span,
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(span.attributes["daily_review.kg_extract_status"], "skipped")

    async def test_happy_path_applies_edges(self) -> None:
        await self._seed_symbol_node()
        executor = self._executor()
        span = _FakeSpan()
        result = await executor._extract_kg_candidates(
            job_id="cron-1", agent_id=None, asof=date(2026, 7, 17),
            reply_text="东方财富是本轮券商龙头。",
            journal_path="journal/2026/2026-07-17.md", span=span,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(span.attributes["daily_review.kg_extract_status"], "ok")
        self.assertEqual(span.attributes["daily_review.kg_candidate_edge_count"], 1)
        counts = await self.repo.counts()
        self.assertEqual(counts["active_edges"], 1)

    async def test_adapter_crash_is_soft_error(self) -> None:
        executor = self._executor(adapter=FakeAdapter(error=RuntimeError("boom")))
        span = _FakeSpan()
        result = await executor._extract_kg_candidates(
            job_id="cron-1", agent_id=None, asof=date(2026, 7, 17),
            reply_text="复盘", journal_path="journal/2026/2026-07-17.md", span=span,
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "model_error")
        self.assertEqual(span.attributes["daily_review.kg_extract_status"], "error")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
