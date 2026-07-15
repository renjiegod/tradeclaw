"""个股角色卡 structured store + API (doyoutrade.knowledge.roles + /knowledge/symbol-roles).

Covers the read layer (last-wins de-dup, bad-line tolerance, empty/absent,
sort), the server-side upsert helper (create / same-symbol replace semantics,
None passthrough), and the ``GET /knowledge/symbol-roles`` endpoint.

Tests use a temp ``DOYOUTRADE_HOME`` (so ``knowledge_root()`` points at a
throwaway dir) for the library-level cases, and a direct temp-root resolver for
the API cases — mirroring the existing KB test style.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from doyoutrade.api.knowledge_base import build_knowledge_router
from doyoutrade.knowledge.roles import read_symbol_roles, upsert_symbol_role


class SymbolRolesReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, lines: list[str]) -> None:
        p = self.tmp / "symbols" / "roles.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(l + "\n" for l in lines), encoding="utf-8")

    def _write_rows(self, rows: list[dict]) -> None:
        self._write([json.dumps(r, ensure_ascii=False) for r in rows])

    def test_empty_when_no_file(self):
        self.assertEqual(read_symbol_roles(root=self.tmp), {"items": []})

    def test_empty_when_dir_but_no_file(self):
        (self.tmp / "symbols").mkdir()
        self.assertEqual(read_symbol_roles(root=self.tmp), {"items": []})

    def test_last_wins_dedup_by_symbol(self):
        # Same symbol appears twice; the later line supersedes the earlier one.
        self._write_rows([
            {"symbol": "600519.SH", "name": "贵州茅台", "role": "中军",
             "note": "老", "strategy_hint": "低吸", "updated_at": "2026-07-01"},
            {"symbol": "600519.SH", "name": "贵州茅台", "role": "龙头",
             "note": "新", "strategy_hint": "追涨", "updated_at": "2026-07-03"},
        ])
        items = read_symbol_roles(root=self.tmp)["items"]
        self.assertEqual(len(items), 1)
        card = items[0]
        self.assertEqual(card["role"], "龙头")
        self.assertEqual(card["note"], "新")
        self.assertEqual(card["updated_at"], "2026-07-03")

    def test_sort_updated_at_desc_then_symbol(self):
        self._write_rows([
            {"symbol": "000001.SZ", "role": "杂毛", "updated_at": "2026-07-01"},
            {"symbol": "600519.SH", "role": "龙头", "updated_at": "2026-07-03"},
            {"symbol": "300750.SZ", "role": "中军", "updated_at": "2026-07-02"},
        ])
        dates = [it["symbol"] for it in read_symbol_roles(root=self.tmp)["items"]]
        # updated_at descending: 07-03, 07-02, 07-01.
        self.assertEqual(dates, ["600519.SH", "300750.SZ", "000001.SZ"])

    def test_missing_updated_at_sorts_last_then_symbol_asc(self):
        self._write_rows([
            {"symbol": "BBB", "role": "杂毛"},               # no updated_at
            {"symbol": "AAA", "role": "杂毛"},               # no updated_at
            {"symbol": "CCC", "role": "龙头", "updated_at": "2026-07-03"},
        ])
        order = [it["symbol"] for it in read_symbol_roles(root=self.tmp)["items"]]
        # CCC has updated_at -> first; the two None-updated_at rows follow,
        # tie-broken by symbol ascending.
        self.assertEqual(order, ["CCC", "AAA", "BBB"])

    def test_missing_values_stay_none(self):
        self._write_rows([{"symbol": "600519.SH", "role": "龙头"}])
        card = read_symbol_roles(root=self.tmp)["items"][0]
        self.assertIsNone(card["name"])
        self.assertIsNone(card["note"])
        self.assertIsNone(card["strategy_hint"])
        self.assertIsNone(card["updated_at"])
        self.assertEqual(card["role"], "龙头")

    def test_bad_lines_tolerated(self):
        self._write([
            json.dumps({"symbol": "600519.SH", "role": "龙头", "updated_at": "2026-07-03"}),
            "not-json-at-all",
            json.dumps([1, 2, 3]),                     # valid json, not an object
            json.dumps({"role": "杂毛"}),               # object, no symbol
            "",                                          # blank line
            json.dumps({"symbol": "000001.SZ", "role": "中军", "updated_at": "2026-07-02"}),
        ])
        items = read_symbol_roles(root=self.tmp)["items"]
        self.assertEqual([it["symbol"] for it in items], ["600519.SH", "000001.SZ"])


class SymbolRoleUpsertTests(unittest.TestCase):
    """Server-side ``upsert_symbol_role`` — uses a temp DOYOUTRADE_HOME."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self._prev_home = os.environ.get("DOYOUTRADE_HOME")
        os.environ["DOYOUTRADE_HOME"] = str(self.tmp)
        # knowledge_root() -> <DOYOUTRADE_HOME>/knowledge
        self.kb_root = self.tmp / "knowledge"

    def tearDown(self) -> None:
        import shutil

        if self._prev_home is None:
            os.environ.pop("DOYOUTRADE_HOME", None)
        else:
            os.environ["DOYOUTRADE_HOME"] = self._prev_home
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_new_card(self):
        result = upsert_symbol_role(
            "600519.SH", "龙头", name="贵州茅台", note="主线",
            strategy_hint="追涨", updated_at="2026-07-03",
        )
        self.assertTrue(result["upserted"])
        self.assertFalse(result["replaced"])
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["path"], "symbols/roles.jsonl")
        items = read_symbol_roles(root=self.kb_root)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["role"], "龙头")
        self.assertEqual(items[0]["name"], "贵州茅台")

    def test_same_symbol_replaces_in_place(self):
        upsert_symbol_role("600519.SH", "中军", updated_at="2026-07-01")
        result = upsert_symbol_role("600519.SH", "龙头", updated_at="2026-07-03")
        self.assertTrue(result["replaced"])
        self.assertEqual(result["row_count"], 1)  # stale row dropped, fresh kept
        items = read_symbol_roles(root=self.kb_root)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["role"], "龙头")
        self.assertEqual(items[0]["updated_at"], "2026-07-03")

    def test_distinct_symbols_accumulate(self):
        upsert_symbol_role("600519.SH", "龙头", updated_at="2026-07-03")
        result = upsert_symbol_role("000001.SZ", "杂毛", updated_at="2026-07-02")
        self.assertFalse(result["replaced"])
        self.assertEqual(result["row_count"], 2)
        symbols = {it["symbol"] for it in read_symbol_roles(root=self.kb_root)["items"]}
        self.assertEqual(symbols, {"600519.SH", "000001.SZ"})

    def test_omitted_fields_stored_none(self):
        upsert_symbol_role("600519.SH", "龙头")
        card = read_symbol_roles(root=self.kb_root)["items"][0]
        self.assertIsNone(card["name"])
        self.assertIsNone(card["updated_at"])

    def test_upsert_drops_malformed_preexisting_line(self):
        # Seed a file with one good line and one hand-edit-broken line.
        p = self.kb_root / "symbols" / "roles.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"symbol": "000001.SZ", "role": "杂毛"}) + "\n"
            + "broken-line\n",
            encoding="utf-8",
        )
        result = upsert_symbol_role("600519.SH", "龙头", updated_at="2026-07-03")
        self.assertEqual(result["dropped"], 1)
        # Both valid symbols survive; the broken line is gone (self-healed).
        symbols = {it["symbol"] for it in read_symbol_roles(root=self.kb_root)["items"]}
        self.assertEqual(symbols, {"000001.SZ", "600519.SH"})


class SymbolRolesApiTests(unittest.TestCase):
    """``GET /knowledge/symbol-roles`` — structured role-card endpoint."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        app = FastAPI()
        app.include_router(build_knowledge_router(lambda: self.tmp))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, lines: list[str]) -> None:
        p = self.tmp / "symbols" / "roles.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(l + "\n" for l in lines), encoding="utf-8")

    def test_empty_when_no_file(self):
        resp = self.client.get("/knowledge/symbol-roles")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"items": []})

    def test_multiple_roles_sorted_and_shaped(self):
        self._write([
            json.dumps({"symbol": "600519.SH", "name": "贵州茅台", "role": "龙头",
                        "note": "主线", "strategy_hint": "追涨",
                        "updated_at": "2026-07-03"}, ensure_ascii=False),
            json.dumps({"symbol": "000001.SZ", "name": "平安银行", "role": "杂毛",
                        "note": None, "strategy_hint": None,
                        "updated_at": "2026-07-01"}, ensure_ascii=False),
        ])
        body = self.client.get("/knowledge/symbol-roles").json()
        self.assertEqual([it["symbol"] for it in body["items"]],
                         ["600519.SH", "000001.SZ"])
        top = body["items"][0]
        self.assertEqual(
            set(top.keys()),
            {"symbol", "name", "role", "note", "strategy_hint", "updated_at"},
        )
        self.assertEqual(top["role"], "龙头")

    def test_dedup_last_wins_via_api(self):
        self._write([
            json.dumps({"symbol": "600519.SH", "role": "中军", "updated_at": "2026-07-01"}),
            json.dumps({"symbol": "600519.SH", "role": "龙头", "updated_at": "2026-07-03"}),
        ])
        body = self.client.get("/knowledge/symbol-roles").json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["role"], "龙头")

    def test_bad_line_tolerated_via_api(self):
        self._write([
            json.dumps({"symbol": "600519.SH", "role": "龙头", "updated_at": "2026-07-03"}),
            "not-json",
        ])
        body = self.client.get("/knowledge/symbol-roles").json()
        self.assertEqual([it["symbol"] for it in body["items"]], ["600519.SH"])


if __name__ == "__main__":
    unittest.main()
