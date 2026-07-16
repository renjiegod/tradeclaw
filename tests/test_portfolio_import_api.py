"""API tests for the /portfolio/imports routes (broker CSV import surface)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from doyoutrade.api.portfolio_import_routes import (
    MAX_STATEMENT_CSV_BYTES,
    build_portfolio_import_router,
)

_CSV_HEADER = "成交日期,成交时间,证券代码,证券名称,买卖标志,成交价格,成交数量,成交金额\n"
_CSV_ROWS = (
    "2026-07-01,09:31:00,600519,贵州茅台,买入,1650.50,100,165050.00\n"
    "2026-07-02,10:00:00,600519,贵州茅台,卖出,1700.00,100,170000.00\n"
    "2026-06-30,14:55:00,000001,平安银行,买入,10.50,1000,10500.00\n"
)
_CSV_BYTES = (_CSV_HEADER + _CSV_ROWS).encode("utf-8")


class PortfolioImportApiTests(unittest.TestCase):
    def setUp(self) -> None:
        # KB root is <tmp>/knowledge via DOYOUTRADE_HOME (knowledge_root()
        # reads the env per call, so the router picks it up per request).
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"DOYOUTRADE_HOME": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

        app = FastAPI()
        app.include_router(build_portfolio_import_router())
        self.client = TestClient(app)

    def _upload(self, url: str, data: bytes = _CSV_BYTES, **form: str):
        return self.client.post(
            url,
            files={"file": ("statement.csv", data, "text/csv")},
            data=form,
        )

    # ------------------------------------------------------------------
    # GET /portfolio/imports/brokers
    # ------------------------------------------------------------------

    def test_brokers_fresh_kb_static_suggestions_only(self) -> None:
        resp = self.client.get("/portfolio/imports/brokers")
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]
        brokers = {it["broker"]: it for it in items}
        for slug in ("huatai", "guojun", "yinhe", "dongcai", "zhongxin"):
            self.assertIn(slug, brokers)
            self.assertFalse(brokers[slug]["existing"])
        self.assertEqual(brokers["huatai"]["display_name"], "华泰证券")

    def test_brokers_existing_dirs_flagged_and_merged(self) -> None:
        (self.root / "knowledge" / "trades" / "huatai").mkdir(parents=True)
        (self.root / "knowledge" / "trades" / "老虎证券").mkdir(parents=True)
        items = self.client.get("/portfolio/imports/brokers").json()["items"]
        brokers = {it["broker"]: it for it in items}
        self.assertTrue(brokers["huatai"]["existing"])
        self.assertEqual(brokers["huatai"]["display_name"], "华泰证券")
        self.assertTrue(brokers["老虎证券"]["existing"])
        self.assertEqual(brokers["老虎证券"]["display_name"], "老虎证券")
        self.assertFalse(brokers["guojun"]["existing"])
        # No duplicate rows for a broker that is both existing and suggested.
        self.assertEqual(len([b for b in items if b["broker"] == "huatai"]), 1)

    # ------------------------------------------------------------------
    # POST /portfolio/imports/csv/parse
    # ------------------------------------------------------------------

    def test_parse_happy_path_zero_write(self) -> None:
        resp = self._upload("/portfolio/imports/csv/parse", broker="huatai")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["fills_total"], 3)
        self.assertEqual(body["new_count"], 3)
        self.assertEqual(body["duplicate_count"], 0)
        self.assertEqual(len(body["records"]), 3)
        self.assertFalse(body["records_truncated"])
        # Preview must not create the trades partition.
        self.assertFalse((self.root / "knowledge" / "trades").exists())

    def test_parse_marks_duplicates_after_commit(self) -> None:
        self._upload("/portfolio/imports/csv/commit", broker="huatai")
        resp = self._upload("/portfolio/imports/csv/parse", broker="huatai")
        body = resp.json()
        self.assertEqual(body["new_count"], 0)
        self.assertEqual(body["duplicate_count"], 3)
        self.assertTrue(all(r["duplicate"] for r in body["records"]))

    def test_parse_invalid_broker_400(self) -> None:
        resp = self._upload("/portfolio/imports/csv/parse", broker="../evil")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["error_code"], "invalid_broker")

    def test_parse_no_fills_400(self) -> None:
        resp = self._upload(
            "/portfolio/imports/csv/parse", data=b"foo,bar\n1,2\n", broker="huatai"
        )
        self.assertEqual(resp.status_code, 400)
        detail = resp.json()["detail"]
        self.assertEqual(detail["error_code"], "csv_no_fills")
        self.assertTrue(detail["unparsed"])

    def test_parse_file_too_large_400(self) -> None:
        big = b"x" * (MAX_STATEMENT_CSV_BYTES + 1)
        resp = self._upload("/portfolio/imports/csv/parse", data=big, broker="huatai")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["error_code"], "file_too_large")

    def test_parse_empty_file_400(self) -> None:
        resp = self._upload("/portfolio/imports/csv/parse", data=b"", broker="huatai")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["error_code"], "empty_file")

    # ------------------------------------------------------------------
    # POST /portfolio/imports/csv/commit
    # ------------------------------------------------------------------

    def test_commit_happy_path_writes_and_reviews(self) -> None:
        resp = self._upload("/portfolio/imports/csv/commit", broker="huatai")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["appended_total"], 3)
        self.assertFalse(body["dry_run"])
        review = body["review"]
        self.assertEqual(review["affected_months"], ["2026-06", "2026-07"])
        self.assertIsNone(review["attribution_error"])
        self.assertEqual(review["attribution_summary"]["round_trips"], 1)
        self.assertTrue(
            (self.root / "knowledge" / "trades" / "huatai" / "2026-07.csv").is_file()
        )

    def test_commit_dry_run_semantics(self) -> None:
        resp = self._upload(
            "/portfolio/imports/csv/commit", broker="huatai", dry_run="true"
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body["dry_run"])
        self.assertIsNone(body["review"])
        self.assertEqual(body["appended_total"], 3)
        self.assertEqual(body["duplicates_skipped"], 0)
        # Rehearsal writes nothing.
        self.assertFalse((self.root / "knowledge" / "trades").exists())

    def test_commit_invalid_broker_400(self) -> None:
        resp = self._upload("/portfolio/imports/csv/commit", broker="bad/../broker")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["error_code"], "invalid_broker")

    def test_commit_file_too_large_400(self) -> None:
        big = b"x" * (MAX_STATEMENT_CSV_BYTES + 1)
        resp = self._upload("/portfolio/imports/csv/commit", data=big, broker="huatai")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["error_code"], "file_too_large")


if __name__ == "__main__":
    unittest.main()
