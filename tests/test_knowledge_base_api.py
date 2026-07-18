"""Read-only KB journals API (doyoutrade.api.knowledge_base)."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from doyoutrade.api.knowledge_base import build_knowledge_router
from doyoutrade.persistence.db import (
    create_engine_and_session_factory,
    dispose_engine,
)
from doyoutrade.persistence.models import Base
from doyoutrade.persistence.repositories import SqlAlchemyKnowledgeGraphRepository


class KnowledgeGraphSchemaApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        app = FastAPI()
        app.include_router(build_knowledge_router(lambda: self.tmp))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_protected_system_schema(self) -> None:
        response = self.client.get("/knowledge/graph/schema")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        entity_types = {item["key"]: item for item in body["entity_types"]}
        relation_types = {item["key"]: item for item in body["relation_types"]}
        self.assertEqual(
            set(entity_types),
            {"cycle", "playbook", "role", "signal", "symbol", "theme"},
        )
        self.assertTrue(all(item["protected"] for item in entity_types.values()))
        self.assertEqual(
            relation_types["has_role"]["source_type"],
            "symbol",
        )
        self.assertEqual(
            relation_types["has_role"]["target_type"],
            "role",
        )
        self.assertTrue(
            all(item["protected"] for item in relation_types.values())
        )
        self.assertEqual(body["namespace"], "system")
        self.assertEqual(body["version"], 1)


class KnowledgeGraphEditingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.engine, session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{self.tmp / 'runtime.db'}"
        )

        async def _create_schema() -> None:
            async with self.engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)

        asyncio.run(_create_schema())
        repository = SqlAlchemyKnowledgeGraphRepository(session_factory)
        app = FastAPI()
        app.include_router(
            build_knowledge_router(
                lambda: self.tmp,
                knowledge_graph_repository=repository,
            )
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        import shutil

        self.client.close()
        asyncio.run(dispose_engine(self.engine))
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _operation(symbol: str = "300059", theme: str = "券商") -> dict:
        return {
            "op": "create_relation",
            "source": {
                "type": "symbol",
                "name": symbol,
                "display_name": symbol,
            },
            "relation": "belongs_to_theme",
            "target": {"type": "theme", "name": theme},
            "fact": f"{symbol} 属于 {theme}题材。",
            "confidence": 1,
        }

    def test_local_change_is_applied_and_queryable(self) -> None:
        response = self.client.post(
            "/knowledge/graph/changes",
            json={
                "operations": [self._operation()],
                "summary": "手工题材标记",
                "expected_revision": 0,
            },
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["revision"], 1)
        graph = self.client.get(
            "/knowledge/graph",
            params={"entity": "300059"},
        )
        self.assertEqual(graph.status_code, 200)
        self.assertEqual(graph.json()["edges"][0]["provenance"], "manual")

    def test_graph_mutations_reject_non_local_browser_origins(self) -> None:
        response = self.client.post(
            "/knowledge/graph/changes",
            headers={"Origin": "https://malicious.example"},
            json={
                "operations": [self._operation()],
                "summary": "跨站写入",
                "expected_revision": 0,
            },
        )

        self.assertEqual(response.status_code, 403)

    def test_manual_relation_revision_undo_and_redo_endpoints(self) -> None:
        created = self.client.post(
            "/knowledge/graph/changes",
            json={
                "operations": [self._operation()],
                "summary": "创建关系",
                "expected_revision": 0,
            },
        ).json()
        revised = self.client.post(
            "/knowledge/graph/changes",
            json={
                "operations": [
                    {
                        "op": "revise_relation",
                        "edge_id": created["edge_ids"][0],
                        "fact": "东方财富明确属于金融科技题材。",
                    }
                ],
                "summary": "修订关系",
                "expected_revision": 1,
            },
        )
        self.assertEqual(revised.status_code, 201, revised.text)
        self.assertEqual(revised.json()["revision"], 2)

        undone = self.client.post(
            "/knowledge/graph/revisions/2/undo",
            json={"expected_revision": 2},
        )
        self.assertEqual(undone.status_code, 200, undone.text)
        graph = self.client.get(
            "/knowledge/graph",
            params={"entity": "300059"},
        ).json()
        active = [edge for edge in graph["edges"] if edge["expired_at"] is None]
        self.assertEqual(active[0]["fact"], "300059 属于 券商题材。")

        redone = self.client.post(
            "/knowledge/graph/revisions/2/redo",
            json={"expected_revision": 3},
        )
        self.assertEqual(redone.status_code, 200, redone.text)
        graph = self.client.get(
            "/knowledge/graph",
            params={"entity": "300059"},
        ).json()
        active = [edge for edge in graph["edges"] if edge["expired_at"] is None]
        self.assertEqual(active[0]["fact"], "东方财富明确属于金融科技题材。")

    def test_custom_schema_create_update_and_deprecate_endpoints(self) -> None:
        created = self.client.put(
            "/knowledge/graph/schema/entity_type/custom.indicator",
            json={
                "definition": {"label": "技术指标", "parent_key": None},
                "expected_revision": 0,
                "expected_version": 0,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["operations"][0]["schema_version"], 1)

        schema = self.client.get("/knowledge/graph/schema").json()
        custom = {
            item["key"]: item
            for item in schema["entity_types"]
            if item["key"].startswith("custom.")
        }
        self.assertEqual(custom["custom.indicator"]["label"], "技术指标")
        self.assertFalse(custom["custom.indicator"]["protected"])

        updated = self.client.put(
            "/knowledge/graph/schema/entity_type/custom.indicator",
            json={
                "definition": {"label": "交易技术指标", "parent_key": None},
                "expected_revision": 1,
                "expected_version": 1,
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        deprecated = self.client.request(
            "DELETE",
            "/knowledge/graph/schema/entity_type/custom.indicator",
            json={"expected_revision": 2, "expected_version": 2},
        )
        self.assertEqual(deprecated.status_code, 200, deprecated.text)
        schema = self.client.get("/knowledge/graph/schema").json()
        item = next(
            value
            for value in schema["entity_types"]
            if value["key"] == "custom.indicator"
        )
        self.assertEqual(item["status"], "deprecated")
        self.assertEqual(item["version"], 3)

        protected = self.client.put(
            "/knowledge/graph/schema/entity_type/symbol",
            json={
                "definition": {"label": "覆盖系统类型"},
                "expected_revision": 3,
                "expected_version": 0,
            },
        )
        self.assertEqual(protected.status_code, 422)

    def test_agent_draft_requires_exact_one_time_human_approval(self) -> None:
        draft_response = self.client.post(
            "/knowledge/graph/change-drafts",
            json={
                "operations": [self._operation("600519", "白酒")],
                "summary": "Agent 建议补充题材",
                "actor_id": "agent-1",
            },
        )
        self.assertEqual(draft_response.status_code, 201, draft_response.text)
        draft = draft_response.json()
        self.assertEqual(
            self.client.get(
                "/knowledge/graph",
                params={"entity": "600519"},
            ).status_code,
            404,
        )

        always = self.client.post(
            f"/knowledge/graph/change-drafts/{draft['id']}/approve",
            json={
                "proposal_hash": draft["proposal_hash"],
                "resolver_id": "local-user",
                "approve_always": True,
            },
        )
        self.assertEqual(always.status_code, 400)

        approval = self.client.post(
            f"/knowledge/graph/change-drafts/{draft['id']}/approve",
            json={
                "proposal_hash": draft["proposal_hash"],
                "resolver_id": "local-user",
            },
        )
        self.assertEqual(approval.status_code, 200, approval.text)
        self.assertEqual(approval.json()["status"], "applied")
        self.assertEqual(
            self.client.get(
                "/knowledge/graph",
                params={"entity": "600519"},
            ).status_code,
            200,
        )
        second = self.client.post(
            f"/knowledge/graph/change-drafts/{draft['id']}/approve",
            json={
                "proposal_hash": draft["proposal_hash"],
                "resolver_id": "local-user",
            },
        )
        self.assertEqual(second.status_code, 409)


class KnowledgeJournalsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        # KB root is self.tmp; journals live under journal/.
        self.journal = self.tmp / "journal" / "2026"
        self.journal.mkdir(parents=True)
        (self.journal / "2026-05-30.md").write_text("# 复盘 2026-05-30\n大盘普涨。", encoding="utf-8")
        (self.journal / "2026-05-29.md").write_text("# 复盘 2026-05-29\n观望。", encoding="utf-8")
        # A non-markdown file in journal/ must never be served.
        (self.journal / "scratch.txt").write_text("secret", encoding="utf-8")
        # A sibling partition that must NOT be reachable.
        (self.tmp / "trades").mkdir()
        (self.tmp / "trades" / "broker.csv").write_text("private", encoding="utf-8")

        app = FastAPI()
        app.include_router(build_knowledge_router(lambda: self.tmp))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_journals_newest_first_md_only(self):
        resp = self.client.get("/knowledge/journals")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["root_exists"])
        paths = [it["path"] for it in body["items"]]
        # Only .md, newest-first (lexical desc ≈ date desc); scratch.txt excluded.
        self.assertEqual(paths, ["2026/2026-05-30.md", "2026/2026-05-29.md"])

    def test_read_journal_content(self):
        resp = self.client.get("/knowledge/journal", params={"path": "2026/2026-05-30.md"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("大盘普涨", body["content"])
        self.assertEqual(body["title"], "2026-05-30")

    def test_missing_journal_is_404(self):
        resp = self.client.get("/knowledge/journal", params={"path": "2026/2099-01-01.md"})
        self.assertEqual(resp.status_code, 404)

    def test_non_markdown_rejected(self):
        resp = self.client.get("/knowledge/journal", params={"path": "2026/scratch.txt"})
        self.assertEqual(resp.status_code, 400)

    def test_path_traversal_rejected(self):
        # Attempt to escape journal/ into the sibling trades/ partition.
        for evil in ("../trades/broker.csv", "../../trades/broker.csv", "/etc/passwd"):
            with self.subTest(path=evil):
                resp = self.client.get("/knowledge/journal", params={"path": evil})
                self.assertIn(resp.status_code, (400, 404))
                # Crucially never 200 — private data must not leak.
                self.assertNotEqual(resp.status_code, 200)

    def test_list_empty_when_journal_dir_absent(self):
        empty_root = Path(tempfile.mkdtemp())
        try:
            app = FastAPI()
            app.include_router(build_knowledge_router(lambda: empty_root))
            client = TestClient(app)
            body = client.get("/knowledge/journals").json()
            self.assertEqual(body, {"items": [], "root_exists": False})
        finally:
            import shutil

            shutil.rmtree(empty_root, ignore_errors=True)


class KnowledgeBrowserApiTests(unittest.TestCase):
    """Full-base browser endpoints: ``GET /knowledge/index`` + ``/knowledge/file``."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "cycles/2026-05").mkdir(parents=True)
        (self.tmp / "cycles/2026-05/_overview.md").write_text(
            "# 2026-05 周期总览\n主线内容。", encoding="utf-8"
        )
        (self.tmp / "cycles/2026-05/002580.SZ.md").write_text(
            "# 圣阳股份 — 见顶\n中期下跌。", encoding="utf-8"
        )
        # A weak-title file (no heading) — must be flagged.
        (self.tmp / "cycles/2026-05/noheading.md").write_text(
            "just prose, no heading\n", encoding="utf-8"
        )
        (self.tmp / "symbols").mkdir()
        (self.tmp / "symbols/roles.md").write_text("# 标的角色分类\n", encoding="utf-8")
        (self.tmp / "trades/2026-05").mkdir(parents=True)
        (self.tmp / "trades/2026-05/raw.csv").write_text(
            "code,price,qty\n600519,1800,100\n000001,15,200\n", encoding="utf-8"
        )

        app = FastAPI()
        app.include_router(build_knowledge_router(lambda: self.tmp))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_index_returns_structured_tree(self):
        body = self.client.get("/knowledge/index").json()
        self.assertTrue(body["root_exists"])
        names = [p["name"] for p in body["partitions"]]
        self.assertEqual(
            names, ["cycles", "symbols", "trades", "journal", "playbook", "backtests"]
        )
        cycles = next(p for p in body["partitions"] if p["name"] == "cycles")
        # 3 files: overview, stock note, weak-title note.
        self.assertEqual(cycles["file_count"], 3)
        may = cycles["groups"][0]
        overview = next(e for e in may["entries"] if e["is_overview"])
        self.assertEqual(overview["rel_path"], "2026-05/_overview.md")
        self.assertEqual(overview["suffix"], ".md")
        weak = next(e for e in may["entries"] if e["weak"])
        self.assertEqual(weak["rel_path"], "2026-05/noheading.md")
        self.assertIn("cycles/2026-05/noheading.md", body["weak_titles"])
        self.assertEqual(body["weak_title_count"], 1)

    def test_index_partition_scope(self):
        body = self.client.get(
            "/knowledge/index", params={"partition": "symbols"}
        ).json()
        self.assertEqual([p["name"] for p in body["partitions"]], ["symbols"])
        self.assertEqual(body["total_files"], 1)

    def test_index_unknown_partition_400(self):
        self.assertEqual(
            self.client.get("/knowledge/index", params={"partition": "secret"}).status_code,
            400,
        )

    def test_read_markdown_file(self):
        resp = self.client.get(
            "/knowledge/file", params={"partition": "cycles", "path": "2026-05/_overview.md"}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "markdown")
        self.assertIn("主线内容", body["content"])

    def test_read_csv_file_parsed_to_table(self):
        resp = self.client.get(
            "/knowledge/file", params={"partition": "trades", "path": "2026-05/raw.csv"}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["kind"], "csv")
        self.assertEqual(body["columns"], ["code", "price", "qty"])
        self.assertEqual(body["rows"], [["600519", "1800", "100"], ["000001", "15", "200"]])
        self.assertFalse(body["truncated"])

    def test_read_csv_strips_utf8_bom_on_first_header(self):
        # Broker exports ship a UTF-8 BOM on the first column; the UI table
        # header must not render the invisible \ufeff prefix.
        bom_csv = self.tmp / "trades/2026-06/bom.csv"
        bom_csv.parent.mkdir(parents=True, exist_ok=True)
        bom_csv.write_bytes("\ufeff发生日期,证券代码\n20260601,600519\n".encode("utf-8"))
        body = self.client.get(
            "/knowledge/file", params={"partition": "trades", "path": "2026-06/bom.csv"}
        ).json()
        self.assertEqual(body["columns"], ["发生日期", "证券代码"])
        self.assertNotIn("\ufeff", body["columns"][0])

    def test_file_unknown_partition_400(self):
        self.assertEqual(
            self.client.get(
                "/knowledge/file", params={"partition": "secret", "path": "x.md"}
            ).status_code,
            400,
        )

    def test_file_bad_suffix_400(self):
        self.assertEqual(
            self.client.get(
                "/knowledge/file", params={"partition": "cycles", "path": "a.exe"}
            ).status_code,
            400,
        )

    def test_file_missing_404(self):
        self.assertEqual(
            self.client.get(
                "/knowledge/file", params={"partition": "cycles", "path": "nope.md"}
            ).status_code,
            404,
        )

    def test_file_path_traversal_rejected(self):
        # Must not escape cycles/ into the trades partition.
        for evil in ("../trades/2026-05/raw.csv", "../../trades/2026-05/raw.csv"):
            with self.subTest(path=evil):
                resp = self.client.get(
                    "/knowledge/file", params={"partition": "cycles", "path": evil}
                )
                self.assertIn(resp.status_code, (400, 404))
                self.assertNotEqual(resp.status_code, 200)

    def test_csv_truncation_flag_when_over_cap(self):
        # Build a CSV with more than _MAX_CSV_ROWS rows and confirm truncation.
        from doyoutrade.api.knowledge_base import _MAX_CSV_ROWS

        big = self.tmp / "trades/2026-04/big.csv"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_text("code,v\n" + "\n".join(f"{i},1" for i in range(_MAX_CSV_ROWS + 50)))
        body = self.client.get(
            "/knowledge/file", params={"partition": "trades", "path": "2026-04/big.csv"}
        ).json()
        self.assertEqual(body["kind"], "csv")
        self.assertTrue(body["truncated"])
        self.assertEqual(len(body["rows"]), _MAX_CSV_ROWS)


class KnowledgeSentimentTimelineApiTests(unittest.TestCase):
    """``GET /knowledge/sentiment-timeline`` — merged 情绪周期 timeline."""

    def setUp(self) -> None:
        import json

        self._json = json
        self.tmp = Path(tempfile.mkdtemp())
        app = FastAPI()
        # The router reads the sentiment logs from the same root it was built
        # with (a temp dir here), so no DOYOUTRADE_HOME juggling is needed.
        app.include_router(build_knowledge_router(lambda: self.tmp))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_log(self, month: str, rows: list[dict]) -> None:
        p = self.tmp / "cycles" / month / "_sentiment.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "".join(self._json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )

    def test_empty_when_no_logs(self):
        resp = self.client.get("/knowledge/sentiment-timeline")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"items": []})

    def test_merge_multiple_months_sorted(self):
        self._write_log("2026-06", [
            {"date": "2026-06-30", "label": "退潮/低迷", "limit_up_count": 22,
             "limit_down_count": 30, "broken_board_count": 40,
             "broken_board_rate": 0.5, "max_streak": 2},
        ])
        self._write_log("2026-07", [
            {"date": "2026-07-03", "label": "分歧加剧", "limit_up_count": 108,
             "limit_down_count": 19, "broken_board_count": 52,
             "broken_board_rate": 0.325, "max_streak": 4},
            {"date": "2026-07-01", "label": "发酵/活跃", "limit_up_count": 70,
             "limit_down_count": 10, "broken_board_count": 20,
             "broken_board_rate": 0.22, "max_streak": 5},
        ])
        body = self.client.get(
            "/knowledge/sentiment-timeline", params={"months": 3}
        ).json()
        dates = [it["date"] for it in body["items"]]
        self.assertEqual(dates, ["2026-06-30", "2026-07-01", "2026-07-03"])
        july3 = body["items"][-1]
        self.assertEqual(july3["label"], "分歧加剧")
        self.assertEqual(july3["broken_board_rate"], 0.325)
        self.assertEqual(july3["max_streak"], 4)

    def test_months_window_drops_older(self):
        self._write_log("2026-05", [{"date": "2026-05-15", "label": "中性"}])
        self._write_log("2026-07", [{"date": "2026-07-03", "label": "分歧加剧"}])
        body = self.client.get(
            "/knowledge/sentiment-timeline", params={"months": 1}
        ).json()
        self.assertEqual([it["date"] for it in body["items"]], ["2026-07-03"])

    def test_bad_line_tolerated(self):
        p = self.tmp / "cycles" / "2026-07" / "_sentiment.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            self._json.dumps({"date": "2026-07-03", "label": "分歧加剧"}) + "\n"
            + "not-json\n",
            encoding="utf-8",
        )
        resp = self.client.get("/knowledge/sentiment-timeline")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([it["date"] for it in resp.json()["items"]], ["2026-07-03"])

    def test_months_out_of_range_rejected(self):
        # ge=1 / le=60 guard on the query param.
        self.assertEqual(
            self.client.get(
                "/knowledge/sentiment-timeline", params={"months": 0}
            ).status_code,
            422,
        )


class KnowledgeTradeAttributionApiTests(unittest.TestCase):
    """``GET /knowledge/trade-attribution`` — FIFO round-trip P&L feed."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        app = FastAPI()
        # Router reads trades/ from the same root it was built with (temp dir).
        app.include_router(build_knowledge_router(lambda: self.tmp))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_trades(self, rel: str, header: str, rows: list[str]) -> None:
        p = self.tmp / "trades" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")

    def test_empty_when_no_trades(self):
        resp = self.client.get("/knowledge/trade-attribution")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["round_trips"], [])
        self.assertEqual(body["by_symbol"], [])
        self.assertEqual(body["summary"]["round_trips"], 0)
        self.assertEqual(body["summary"]["total_realized_pnl"], "0")

    def test_round_trip_feed(self):
        self._write_trades(
            "huatai/2026-05.csv",
            "成交日期,成交时间,证券代码,证券名称,买卖标志,成交价格,成交数量,成交金额",
            [
                "2026-05-06,09:35:00,600519,贵州茅台,买入,1800,100,180000",
                "2026-05-08,14:20:00,600519,贵州茅台,卖出,1900,100,190000",
            ],
        )
        body = self.client.get("/knowledge/trade-attribution").json()
        self.assertEqual(len(body["round_trips"]), 1)
        rt = body["round_trips"][0]
        self.assertEqual(rt["symbol"], "600519")
        self.assertEqual(rt["realized_pnl"], "10000")
        self.assertEqual(body["summary"]["win_count"], 1)
        self.assertEqual(body["summary"]["win_rate"], 1.0)

    def test_months_window_param(self):
        self._write_trades(
            "trades.csv",
            "成交日期,成交时间,证券代码,证券名称,买卖标志,成交价格,成交数量,成交金额",
            [
                "2026-03-01,09:31:00,600519,贵州茅台,买入,1800,10,18000",
                "2026-03-02,09:31:00,600519,贵州茅台,卖出,1900,10,19000",
                "2026-07-01,09:31:00,000001,平安银行,买入,10,100,1000",
                "2026-07-02,09:31:00,000001,平安银行,卖出,11,100,1100",
            ],
        )
        body = self.client.get(
            "/knowledge/trade-attribution", params={"months": 1}
        ).json()
        self.assertEqual([rt["symbol"] for rt in body["round_trips"]], ["000001"])

    def test_months_out_of_range_rejected(self):
        self.assertEqual(
            self.client.get(
                "/knowledge/trade-attribution", params={"months": 0}
            ).status_code,
            422,
        )

    def test_unparsed_file_surfaced(self):
        self._write_trades(
            "mystery/2026-05.csv",
            "日期,代码,备注",
            ["2026-05-01,600519,随便"],
        )
        body = self.client.get("/knowledge/trade-attribution").json()
        self.assertEqual(body["round_trips"], [])
        reasons = [u["reason"] for u in body["unparsed"]]
        self.assertIn("core_columns_unmapped", reasons)


class KnowledgePlaybookApiTests(unittest.TestCase):
    """``GET /knowledge/playbook`` — 打板模式库 / 战法总结 feed."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        app = FastAPI()
        # Router reads playbook/ from the same root it was built with (temp dir).
        app.include_router(build_knowledge_router(lambda: self.tmp))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel: str, content: str, *, mtime: float | None = None) -> Path:
        import os

        p = self.tmp / "playbook" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_empty_when_no_playbook_dir(self):
        resp = self.client.get("/knowledge/playbook")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"items": []})

    def test_empty_when_dir_present_but_no_files(self):
        (self.tmp / "playbook").mkdir(parents=True)
        self.assertEqual(self.client.get("/knowledge/playbook").json(), {"items": []})

    def test_frontmatter_parsed_and_sorted_by_mtime_desc(self):
        # Two well-formed playbooks with distinct mtimes; newest first.
        self._write(
            "first-board.md",
            "---\n"
            "pattern: 首板低吸\n"
            "stage: 发酵\n"
            "tags: [打板, 低吸, 龙头]\n"
            "summary: 发酵期首板龙头次日低吸\n"
            "---\n"
            "# 首板低吸战法\n正文\n",
            mtime=1_000_000.0,
        )
        self._write(
            "second-board.md",
            "---\n"
            "pattern: 二板打板\n"
            "stage: 高潮\n"
            "tags: [打板]\n"
            "summary: 高潮期二板接力\n"
            "---\n"
            "# 二板接力战法\n正文\n",
            mtime=2_000_000.0,
        )
        body = self.client.get("/knowledge/playbook").json()
        paths = [it["path"] for it in body["items"]]
        # Newest mtime (second-board) first.
        self.assertEqual(paths, ["second-board.md", "first-board.md"])
        first = next(it for it in body["items"] if it["path"] == "first-board.md")
        self.assertEqual(first["pattern"], "首板低吸")
        self.assertEqual(first["stage"], "发酵")
        self.assertEqual(first["tags"], ["打板", "低吸", "龙头"])
        self.assertEqual(first["summary"], "发酵期首板龙头次日低吸")
        # Title: summary front-matter wins (same rule as the index extractor).
        self.assertEqual(first["title"], "发酵期首板龙头次日低吸")
        # mtime surfaced as ISO Z.
        self.assertTrue(first["updated_at"].endswith("Z"))

    def test_title_from_heading_when_no_summary(self):
        self._write(
            "no-summary.md",
            "---\npattern: 龙回头\nstage: 退潮\n---\n# 龙回头战法 — 退潮修复\n正文\n",
        )
        item = self.client.get("/knowledge/playbook").json()["items"][0]
        self.assertEqual(item["title"], "龙回头战法 — 退潮修复")
        self.assertEqual(item["pattern"], "龙回头")
        self.assertEqual(item["tags"], [])
        self.assertIsNone(item["summary"])

    def test_no_frontmatter_defaults_to_none_and_empty_tags(self):
        self._write("plain.md", "# 纯正文战法\n没有 front-matter\n")
        item = self.client.get("/knowledge/playbook").json()["items"][0]
        self.assertEqual(item["title"], "纯正文战法")
        self.assertIsNone(item["pattern"])
        self.assertIsNone(item["stage"])
        self.assertIsNone(item["summary"])
        self.assertEqual(item["tags"], [])

    def test_bad_frontmatter_tolerated_fields_omitted(self):
        # Broken YAML in the front-matter must not crash the feed; the file
        # still surfaces (title/path/mtime), only its structured fields fall
        # back to the defaults (loud-skip, not a silent drop).
        self._write(
            "broken.md",
            "---\npattern: [unclosed\nstage: bad: yaml: colons\n---\n# 坏front-matter战法\n",
        )
        # A good sibling so we also confirm the feed keeps working around it.
        self._write("ok.md", "---\npattern: 好战法\n---\n# 好战法标题\n")
        body = self.client.get("/knowledge/playbook").json()
        broken = next(it for it in body["items"] if it["path"] == "broken.md")
        # Title comes from the # heading (extractor is independent of the FM parse).
        self.assertEqual(broken["title"], "坏front-matter战法")
        self.assertIsNone(broken["pattern"])
        self.assertIsNone(broken["stage"])
        self.assertEqual(broken["tags"], [])
        ok = next(it for it in body["items"] if it["path"] == "ok.md")
        self.assertEqual(ok["pattern"], "好战法")

    def test_tags_scalar_wrapped_into_list(self):
        self._write("scalar-tags.md", "---\ntags: 打板\n---\n# 单标签战法\n")
        item = self.client.get("/knowledge/playbook").json()["items"][0]
        self.assertEqual(item["tags"], ["打板"])

    def test_nested_subdir_playbook_included(self):
        # rglob picks up nested files; path is playbook-relative.
        self._write("archived/2025/old.md", "---\npattern: 老战法\n---\n# 归档战法\n")
        item = self.client.get("/knowledge/playbook").json()["items"][0]
        self.assertEqual(item["path"], "archived/2025/old.md")
        self.assertEqual(item["pattern"], "老战法")


if __name__ == "__main__":
    unittest.main()
