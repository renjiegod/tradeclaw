"""Tests for portfolio import (功能 6): vision extraction + broker CSV import."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from doyoutrade.models.base import MAX_IMAGE_BYTES, ModelAdapter, ModelRequest, ModelResponse
from doyoutrade.portfolio_import import image_extractor
from doyoutrade.portfolio_import.csv_import import analyze_trades_csv, import_trades_csv
from doyoutrade.portfolio_import.image_extractor import extract_positions_from_image

_PNG = b"\x89PNG\r\n\x1a\n" + b"fake"
_JPEG = b"\xff\xd8\xff\xe0" + b"fake"


class _StubAdapter(ModelAdapter):
    def __init__(self, text: str = "[]", exc: Exception | None = None) -> None:
        self.text = text
        self.exc = exc
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        return ModelResponse(text=self.text)


async def _fake_search_hit(**kwargs: Any) -> dict[str, Any]:
    return {"source": kwargs.get("source"), "items": [{"symbol": "600519.SH", "name": "贵州茅台"}]}


async def _fake_search_miss(**kwargs: Any) -> dict[str, Any]:
    return {"source": kwargs.get("source"), "items": []}


class ImageExtractorTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_ok_with_symbol_resolution(self) -> None:
        adapter = _StubAdapter(
            text='[{"name": "贵州茅台", "quantity": 100, "cost_price": 1650.5}]'
        )
        with mock.patch.object(
            image_extractor, "search_instrument_universe", _fake_search_hit
        ):
            result = self._run(
                extract_positions_from_image(_PNG, "image/png", adapter=adapter)
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["positions"]), 1)
        pos = result["positions"][0]
        self.assertEqual(pos["symbol"], "600519.SH")
        self.assertEqual(pos["quantity"], 100)
        self.assertEqual(result["unresolved"], [])
        # The image actually reached the model request.
        req = adapter.requests[0]
        self.assertIsNotNone(req.image_parts)
        self.assertEqual(req.image_parts[0].mime_type, "image/png")
        self.assertEqual(req.image_parts[0].data, _PNG)

    def test_explicit_symbol_skips_lookup(self) -> None:
        adapter = _StubAdapter(
            text='[{"name": "贵州茅台", "symbol": "600519", "quantity": 100}]'
        )

        async def _boom(**kwargs: Any):
            raise AssertionError("lookup must not be called when symbol present")

        with mock.patch.object(image_extractor, "search_instrument_universe", _boom):
            result = self._run(
                extract_positions_from_image(_PNG, "image/png", adapter=adapter)
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["positions"][0]["symbol"], "600519")

    def test_unresolved_name_kept_and_flagged(self) -> None:
        adapter = _StubAdapter(text='[{"name": "不存在的股票", "quantity": 200}]')
        with mock.patch.object(
            image_extractor, "search_instrument_universe", _fake_search_miss
        ):
            result = self._run(
                extract_positions_from_image(_PNG, "image/png", adapter=adapter)
            )
        self.assertEqual(result["status"], "ok")
        pos = result["positions"][0]
        self.assertEqual(pos["name"], "不存在的股票")
        self.assertTrue(pos["symbol_unresolved"])
        self.assertEqual(result["unresolved"][0]["reason"], "symbol_unresolved")

    def test_json_wrapped_in_prose_recovered(self) -> None:
        adapter = _StubAdapter(
            text='好的，识别结果如下：\n```json\n[{"name": "A", "symbol": "000001", "quantity": 1}]\n```'
        )
        result = self._run(
            extract_positions_from_image(_PNG, "image/png", adapter=adapter)
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["positions"][0]["symbol"], "000001")

    def test_parse_failed(self) -> None:
        adapter = _StubAdapter(text="this is definitely not json")
        result = self._run(
            extract_positions_from_image(_PNG, "image/png", adapter=adapter)
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "extract_parse_failed")
        self.assertIn("not json", result["raw_text"])
        self.assertLessEqual(len(result["raw_text"]), 500)

    def test_extract_empty(self) -> None:
        adapter = _StubAdapter(text="[]")
        result = self._run(
            extract_positions_from_image(_PNG, "image/png", adapter=adapter)
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "extract_empty")

    def test_mime_mismatch(self) -> None:
        adapter = _StubAdapter()
        result = self._run(
            extract_positions_from_image(_PNG, "image/jpeg", adapter=adapter)
        )
        self.assertEqual(result["error_code"], "image_mime_mismatch")
        self.assertEqual(result["sniffed_mime"], "image/png")
        self.assertEqual(adapter.requests, [])  # never reached the model

    def test_unrecognised_magic(self) -> None:
        adapter = _StubAdapter()
        result = self._run(
            extract_positions_from_image(b"not-an-image", "image/png", adapter=adapter)
        )
        self.assertEqual(result["error_code"], "image_mime_mismatch")

    def test_too_large(self) -> None:
        adapter = _StubAdapter()
        big = b"\x00" * (MAX_IMAGE_BYTES + 1)
        result = self._run(
            extract_positions_from_image(big, "image/png", adapter=adapter)
        )
        self.assertEqual(result["error_code"], "image_too_large")
        self.assertEqual(result["size_bytes"], MAX_IMAGE_BYTES + 1)

    def test_empty_image(self) -> None:
        adapter = _StubAdapter()
        result = self._run(
            extract_positions_from_image(b"", "image/png", adapter=adapter)
        )
        self.assertEqual(result["error_code"], "image_empty")

    def test_model_error(self) -> None:
        adapter = _StubAdapter(exc=RuntimeError("boom"))
        result = self._run(
            extract_positions_from_image(_JPEG, "image/jpeg", adapter=adapter)
        )
        self.assertEqual(result["error_code"], "model_error")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertIn("boom", result["message"])

    def test_sniff_variants(self) -> None:
        self.assertEqual(image_extractor.sniff_image_mime(_PNG), "image/png")
        self.assertEqual(image_extractor.sniff_image_mime(_JPEG), "image/jpeg")
        self.assertEqual(image_extractor.sniff_image_mime(b"GIF89a...."), "image/gif")
        self.assertEqual(
            image_extractor.sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPVP8 "),
            "image/webp",
        )
        self.assertIsNone(image_extractor.sniff_image_mime(b"hello world!"))


_CSV_HEADER = "成交日期,成交时间,证券代码,证券名称,买卖标志,成交价格,成交数量,成交金额\n"
_CSV_ROWS = (
    "2026-07-01,09:31:00,600519,贵州茅台,买入,1650.50,100,165050.00\n"
    "2026-07-02,10:00:00,600519,贵州茅台,卖出,1700.00,100,170000.00\n"
    "2026-06-30,14:55:00,000001,平安银行,买入,10.50,1000,10500.00\n"
)


class CsvImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"DOYOUTRADE_HOME": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _write_input(self, content: str) -> Path:
        path = self.root / "upload.csv"
        path.write_text(content, encoding="utf-8")
        return path

    def test_import_writes_monthly_files(self) -> None:
        path = self._write_input(_CSV_HEADER + _CSV_ROWS)
        result = import_trades_csv(path, broker="huatai")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["fills_total"], 3)
        self.assertEqual(result["duplicates_skipped"], 0)
        self.assertEqual(
            result["written"],
            {"trades/huatai/2026-06.csv": 1, "trades/huatai/2026-07.csv": 2},
        )
        june = self.root / "knowledge" / "trades" / "huatai" / "2026-06.csv"
        july = self.root / "knowledge" / "trades" / "huatai" / "2026-07.csv"
        self.assertTrue(june.is_file())
        self.assertTrue(july.is_file())
        text = july.read_text(encoding="utf-8")
        self.assertIn("date,time,symbol,name,side,price,qty,amount", text)
        self.assertIn("600519", text)
        self.assertTrue(result["attribution_readable"])
        self.assertIsNotNone(result["index_path"])
        # Index file actually exists.
        self.assertTrue(Path(result["index_path"]).is_file())

    def test_reimport_dedupes(self) -> None:
        path = self._write_input(_CSV_HEADER + _CSV_ROWS)
        first = import_trades_csv(path, broker="huatai")
        self.assertEqual(first["appended_total"], 3)
        second = import_trades_csv(path, broker="huatai")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["appended_total"], 0)
        self.assertEqual(second["duplicates_skipped"], 3)
        # File contents unchanged (header + 2 rows for July).
        july = self.root / "knowledge" / "trades" / "huatai" / "2026-07.csv"
        lines = [l for l in july.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)

    def test_partial_overlap_appends_only_new(self) -> None:
        path = self._write_input(_CSV_HEADER + _CSV_ROWS)
        import_trades_csv(path, broker="huatai")
        extra = _CSV_HEADER + _CSV_ROWS + (
            "2026-07-03,09:40:00,000002,万科A,买入,8.88,500,4440.00\n"
        )
        path2 = self.root / "upload2.csv"
        path2.write_text(extra, encoding="utf-8")
        result = import_trades_csv(path2, broker="huatai")
        self.assertEqual(result["appended_total"], 1)
        self.assertEqual(result["duplicates_skipped"], 3)

    def test_bytes_input(self) -> None:
        result = import_trades_csv(
            (_CSV_HEADER + _CSV_ROWS).encode("utf-8"), broker="国君"
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["appended_total"], 3)
        self.assertTrue(
            (self.root / "knowledge" / "trades" / "国君" / "2026-07.csv").is_file()
        )

    def test_zero_fills_is_structured_error(self) -> None:
        path = self._write_input("foo,bar\n1,2\n")
        result = import_trades_csv(path, broker="huatai")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "csv_no_fills")
        self.assertTrue(result["unparsed"])
        self.assertEqual(result["unparsed"][0]["reason"], "core_columns_unmapped")

    def test_unparsed_rows_surfaced_not_dropped(self) -> None:
        content = _CSV_HEADER + _CSV_ROWS + (
            "2026-07-05,,600000,浦发银行,红利入账,0,0,12.00\n"  # non-trade side
            "bad-date,,600000,浦发银行,买入,10,100,1000\n"  # bad date
        )
        path = self._write_input(content)
        result = import_trades_csv(path, broker="huatai")
        self.assertEqual(result["status"], "ok")
        reasons = {entry["reason"] for entry in result["unparsed"]}
        self.assertIn("non_trade_side", reasons)
        self.assertIn("bad_row_values", reasons)
        self.assertEqual(result["fills_total"], 3)

    def test_invalid_broker(self) -> None:
        result = import_trades_csv(b"x", broker="../evil")
        self.assertEqual(result["error_code"], "invalid_broker")
        result = import_trades_csv(b"x", broker="")
        self.assertEqual(result["error_code"], "invalid_broker")

    def test_missing_file(self) -> None:
        result = import_trades_csv(self.root / "nope.csv", broker="huatai")
        self.assertEqual(result["error_code"], "file_not_found")

    # ------------------------------------------------------------------
    # analyze_trades_csv (pure preview, zero writes)
    # ------------------------------------------------------------------

    def test_analyze_new_file_all_new_and_zero_write(self) -> None:
        result = analyze_trades_csv(
            (_CSV_HEADER + _CSV_ROWS).encode("utf-8"), broker="huatai"
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["broker"], "huatai")
        self.assertEqual(result["fills_total"], 3)
        self.assertEqual(result["new_count"], 3)
        self.assertEqual(result["duplicate_count"], 0)
        self.assertEqual(result["unparsed_count"], 0)
        self.assertFalse(result["records_truncated"])
        self.assertEqual(len(result["records"]), 3)
        rec = result["records"][0]
        for key in ("date", "time", "symbol", "name", "side", "price", "qty",
                    "amount", "month", "duplicate"):
            self.assertIn(key, rec)
        self.assertFalse(rec["duplicate"])
        # Zero writes: the KB tree (in fact anything under DOYOUTRADE_HOME
        # except our own input file) must not have been created.
        self.assertFalse((self.root / "knowledge").exists())

    def test_analyze_marks_duplicates_against_existing_files(self) -> None:
        path = self._write_input(_CSV_HEADER + _CSV_ROWS)
        import_trades_csv(path, broker="huatai")
        extra = _CSV_HEADER + _CSV_ROWS + (
            "2026-07-03,09:40:00,000002,万科A,买入,8.88,500,4440.00\n"
        )
        result = analyze_trades_csv(extra.encode("utf-8"), broker="huatai")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["duplicate_count"], 3)
        dup_flags = {r["symbol"]: r["duplicate"] for r in result["records"]}
        self.assertFalse(dup_flags["000002"])
        self.assertTrue(dup_flags["600519"])
        # Preview must not have appended anything.
        july = self.root / "knowledge" / "trades" / "huatai" / "2026-07.csv"
        lines = [l for l in july.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)  # header + 2 rows, unchanged

    def test_analyze_batch_internal_duplicate(self) -> None:
        row = "2026-07-01,09:31:00,600519,贵州茅台,买入,1650.50,100,165050.00\n"
        result = analyze_trades_csv(
            (_CSV_HEADER + row + row).encode("utf-8"), broker="huatai"
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual(
            [r["duplicate"] for r in result["records"]], [False, True]
        )

    def test_analyze_invalid_broker(self) -> None:
        result = analyze_trades_csv(b"x", broker="../evil")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "invalid_broker")

    def test_analyze_no_fills(self) -> None:
        result = analyze_trades_csv(b"foo,bar\n1,2\n", broker="huatai")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "csv_no_fills")
        self.assertEqual(result["unparsed_count"], 1)

    # ------------------------------------------------------------------
    # import_trades_csv dry_run
    # ------------------------------------------------------------------

    def test_dry_run_writes_nothing_and_counts_match_real_import(self) -> None:
        data = (_CSV_HEADER + _CSV_ROWS).encode("utf-8")
        dry = import_trades_csv(data, broker="huatai", dry_run=True)
        self.assertEqual(dry["status"], "ok")
        self.assertTrue(dry["dry_run"])
        self.assertIsNone(dry["review"])
        self.assertIsNone(dry["index_path"])
        self.assertFalse((self.root / "knowledge" / "trades").exists())

        real = import_trades_csv(data, broker="huatai")
        self.assertFalse(real["dry_run"])
        self.assertEqual(dry["written"], real["written"])
        self.assertEqual(dry["appended_total"], real["appended_total"])
        self.assertEqual(dry["duplicates_skipped"], real["duplicates_skipped"])

    def test_dry_run_batch_dedupe_matches_real_semantics(self) -> None:
        row = "2026-07-01,09:31:00,600519,贵州茅台,买入,1650.50,100,165050.00\n"
        data = (_CSV_HEADER + row + row).encode("utf-8")
        dry = import_trades_csv(data, broker="huatai", dry_run=True)
        self.assertEqual(dry["appended_total"], 1)
        self.assertEqual(dry["duplicates_skipped"], 1)

    def test_dry_run_against_existing_files(self) -> None:
        data = (_CSV_HEADER + _CSV_ROWS).encode("utf-8")
        import_trades_csv(data, broker="huatai")
        dry = import_trades_csv(data, broker="huatai", dry_run=True)
        self.assertEqual(dry["appended_total"], 0)
        self.assertEqual(dry["duplicates_skipped"], 3)

    # ------------------------------------------------------------------
    # review block (复盘融合)
    # ------------------------------------------------------------------

    def test_real_import_review_block(self) -> None:
        result = import_trades_csv(
            (_CSV_HEADER + _CSV_ROWS).encode("utf-8"), broker="huatai"
        )
        self.assertEqual(result["status"], "ok")
        review = result["review"]
        self.assertIsInstance(review, dict)
        self.assertEqual(review["affected_months"], ["2026-06", "2026-07"])
        self.assertIsNone(review["attribution_error"])
        summary = review["attribution_summary"]
        self.assertIsInstance(summary, dict)
        # One flat 600519 round-trip (buy 100 @1650.50, sell 100 @1700.00).
        self.assertEqual(summary["round_trips"], 1)
        self.assertEqual(summary["open_positions"], 1)  # 000001 still open

    def test_review_attribution_failure_is_visible(self) -> None:
        from doyoutrade.portfolio_import import csv_import as mod

        def _boom(**kwargs: Any):
            raise RuntimeError("attribution exploded")

        with mock.patch.object(mod, "read_trade_attribution", _boom):
            result = import_trades_csv(
                (_CSV_HEADER + _CSV_ROWS).encode("utf-8"), broker="huatai"
            )
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["attribution_readable"])
        review = result["review"]
        self.assertIsNone(review["attribution_summary"])
        self.assertIn("RuntimeError", review["attribution_error"])
        self.assertIn("attribution exploded", review["attribution_error"])
        self.assertEqual(result["attribution_error"], review["attribution_error"])


class PortfolioImportToolTests(unittest.TestCase):
    """Contract-level tests for the assistant tools (unwired / happy CSV path)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"DOYOUTRADE_HOME": self._tmp.name})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_image_tool_unwired(self) -> None:
        from doyoutrade.tools.portfolio_import import ImportPositionsFromImageTool

        tool = ImportPositionsFromImageTool()
        result = asyncio.run(tool.execute(file_path="/tmp/x.png"))
        self.assertTrue(result.is_error)
        self.assertIn("portfolio_import_unwired", result.text)

    def test_image_tool_unknown_arguments_rejected(self) -> None:
        from doyoutrade.tools.portfolio_import import ImportPositionsFromImageTool

        tool = ImportPositionsFromImageTool(model_adapter=_StubAdapter())
        result = asyncio.run(tool.execute(file_path="/tmp/x.png", bogus=1))
        self.assertTrue(result.is_error)
        self.assertIn("bogus", result.text)

    def test_image_tool_sandbox_violation(self) -> None:
        from doyoutrade.tools.portfolio_import import ImportPositionsFromImageTool

        tool = ImportPositionsFromImageTool(model_adapter=_StubAdapter())
        result = asyncio.run(tool.execute(file_path="/definitely/outside/sandbox.png"))
        self.assertTrue(result.is_error)
        self.assertIn("sandbox_violation", result.text)

    def test_image_tool_happy_path_in_sandbox(self) -> None:
        from doyoutrade.tools._sandbox import register_knowledge_sandbox
        from doyoutrade.tools.portfolio_import import ImportPositionsFromImageTool

        kb = register_knowledge_sandbox()
        image_path = kb / "uploads" / "positions.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(_PNG)

        adapter = _StubAdapter(
            text='[{"name": "A", "symbol": "000001", "quantity": 1}]'
        )
        tool = ImportPositionsFromImageTool(model_adapter=adapter)
        result = asyncio.run(tool.execute(file_path=str(image_path)))
        self.assertFalse(result.is_error, result.text)
        self.assertIn("000001", result.text)

    def test_csv_tool_happy_path(self) -> None:
        from doyoutrade.tools._sandbox import register_knowledge_sandbox
        from doyoutrade.tools.portfolio_import import ImportTradesCsvTool

        kb = register_knowledge_sandbox()
        csv_path = kb / "uploads" / "statement.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(_CSV_HEADER + _CSV_ROWS, encoding="utf-8")

        tool = ImportTradesCsvTool()
        result = asyncio.run(tool.execute(file_path=str(csv_path), broker="huatai"))
        self.assertFalse(result.is_error, result.text)
        self.assertIn("duplicates_skipped", result.text)
        # 复盘融合: the JSON payload carries the review block.
        self.assertIn("affected_months", result.text)
        self.assertIn("attribution_summary", result.text)
        self.assertIn("归因复盘", result.text)
        self.assertTrue(
            (self.root / "knowledge" / "trades" / "huatai" / "2026-07.csv").is_file()
        )

    def test_csv_tool_dry_run(self) -> None:
        from doyoutrade.tools._sandbox import register_knowledge_sandbox
        from doyoutrade.tools.portfolio_import import ImportTradesCsvTool

        kb = register_knowledge_sandbox()
        csv_path = kb / "uploads" / "statement.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(_CSV_HEADER + _CSV_ROWS, encoding="utf-8")

        tool = ImportTradesCsvTool()
        result = asyncio.run(
            tool.execute(file_path=str(csv_path), broker="huatai", dry_run=True)
        )
        self.assertFalse(result.is_error, result.text)
        self.assertIn("预演", result.text)
        self.assertFalse(
            (self.root / "knowledge" / "trades" / "huatai").exists()
        )

    def test_csv_tool_dry_run_string_coercion(self) -> None:
        from doyoutrade.tools._sandbox import register_knowledge_sandbox
        from doyoutrade.tools.portfolio_import import ImportTradesCsvTool

        kb = register_knowledge_sandbox()
        csv_path = kb / "uploads" / "statement.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(_CSV_HEADER + _CSV_ROWS, encoding="utf-8")

        tool = ImportTradesCsvTool()
        result = asyncio.run(
            tool.execute(file_path=str(csv_path), broker="huatai", dry_run="true")
        )
        self.assertFalse(result.is_error, result.text)
        self.assertIn("预演", result.text)
        self.assertFalse((self.root / "knowledge" / "trades" / "huatai").exists())
        # A non-boolean value is a structured coercion error, not a crash.
        bad = asyncio.run(
            tool.execute(file_path=str(csv_path), broker="huatai", dry_run="maybe")
        )
        self.assertTrue(bad.is_error)
        self.assertIn("invalid_dry_run_json", bad.text)

    def test_csv_tool_no_fills(self) -> None:
        from doyoutrade.tools._sandbox import register_knowledge_sandbox
        from doyoutrade.tools.portfolio_import import ImportTradesCsvTool

        kb = register_knowledge_sandbox()
        csv_path = kb / "uploads" / "bad.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text("foo,bar\n1,2\n", encoding="utf-8")

        tool = ImportTradesCsvTool()
        result = asyncio.run(tool.execute(file_path=str(csv_path), broker="huatai"))
        self.assertTrue(result.is_error)
        self.assertIn("csv_no_fills", result.text)


if __name__ == "__main__":
    unittest.main()
