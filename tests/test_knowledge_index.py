"""Tests for the knowledge-base index layer.

Covers the three surfaces that share ``doyoutrade.knowledge.index``:

* the generator (``build_knowledge_index`` / ``render_index_markdown`` /
  ``write_index_file``) — partition walking, title extraction, grouping &
  sort order, UTF-8 truncation safety, no-silent-drop on read failure,
  empty / missing KB handling;
* the in-process ``KnowledgeIndexTool`` — kwargs contract
  (``unknown_arguments``), ``unknown_partition``, happy path (scoped +
  full), ``knowledge_root_missing`` soft-error;
* the ``doyoutrade-cli knowledge index`` command — default print,
  ``--refresh`` writes ``_index.md``, ``--partition`` scope.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner

from doyoutrade.knowledge import (
    build_knowledge_index,
    render_index_markdown,
    write_index_file,
)
from doyoutrade.tools.knowledge_index import KnowledgeIndexTool


def _write(root: Path, rel: str, content: str = "") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


class IndexGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        # Build a representative mini KB covering every partition + strategy.
        _write(self.tmp, "cycles/2026-05/_overview.md", "# 2026-05 周期总览\n...")
        _write(self.tmp, "cycles/2026-05/002580.SZ.md", "# 圣阳股份 — 见顶\n")
        _write(self.tmp, "cycles/2026-04/target.md", "# 中天科技\n")
        _write(self.tmp, "symbols/roles.md", "# 标的角色分类\n")
        _write(self.tmp, "symbols/playbook.md", "# 决策卡\n")
        _write(self.tmp, "trades/2026-04/raw.csv", "code,price\n")
        _write(self.tmp, "trades/2026-05/raw.csv", "code,price\n")
        _write(self.tmp, "journal/2026/2026-05-30.md", "# 2026-05-30 复盘\n")
        _write(self.tmp, "playbook/first-board.md", "# 首板打板战法\n")
        _write(self.tmp, "playbook/_overview.md", "# 打板模式库总览\n")
        _write(
            self.tmp,
            "backtests/oversold-sd-a7/v0001-10stocks.md",
            "# v0001 样本外\n",
        )
        _write(self.tmp, "backtests/oversold-sd-a7/v0001-10stocks.csv", "code,ret\n")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_walks_all_partitions_with_counts(self) -> None:
        idx = build_knowledge_index(self.tmp)
        names = [p.name for p in idx.partitions]
        self.assertEqual(
            names, ["cycles", "symbols", "trades", "journal", "playbook", "backtests"]
        )
        by_name = {p.name: p for p in idx.partitions}
        self.assertEqual(by_name["cycles"].file_count, 3)
        self.assertEqual(by_name["symbols"].file_count, 2)
        self.assertEqual(by_name["trades"].file_count, 2)
        self.assertEqual(by_name["journal"].file_count, 1)
        self.assertEqual(by_name["playbook"].file_count, 2)
        self.assertEqual(by_name["backtests"].file_count, 2)
        self.assertEqual(idx.total_files, 12)
        self.assertEqual(idx.skipped, ())
        self.assertTrue(idx.root_exists)

    def test_playbook_flat_strategy_overview_first(self) -> None:
        # playbook/ is a flat partition (like symbols/): one group, with the
        # _overview.md entry flagged ⭐ and sorted first.
        idx = build_knowledge_index(self.tmp)
        playbook = next(p for p in idx.partitions if p.name == "playbook")
        self.assertEqual(len(playbook.groups), 1)
        self.assertTrue(playbook.groups[0].entries[0].is_overview)
        titles = {e.title for e in playbook.groups[0].entries}
        self.assertIn("首板打板战法", titles)

    def test_cycles_grouped_by_month_newest_first(self) -> None:
        idx = build_knowledge_index(self.tmp)
        cycles = next(p for p in idx.partitions if p.name == "cycles")
        group_names = [g.name for g in cycles.groups]
        self.assertEqual(group_names, ["2026-05", "2026-04"])

    def test_overview_entry_flagged_and_sorted_first(self) -> None:
        idx = build_knowledge_index(self.tmp)
        cycles = next(p for p in idx.partitions if p.name == "cycles")
        may = next(g for g in cycles.groups if g.name == "2026-05")
        # _overview.md flagged + sorted before the per-stock note.
        self.assertTrue(may.entries[0].is_overview)
        self.assertEqual(may.entries[0].rel_path, "2026-05/_overview.md")

    def test_title_from_first_heading(self) -> None:
        idx = build_knowledge_index(self.tmp)
        cycles = next(p for p in idx.partitions if p.name == "cycles")
        may = next(g for g in cycles.groups if g.name == "2026-05")
        stock = next(e for e in may.entries if e.rel_path.endswith("002580.SZ.md"))
        self.assertIn("圣阳股份", stock.title)

    def test_title_from_yaml_frontmatter_summary(self) -> None:
        _write(
            self.tmp,
            "cycles/2026-03/x.md",
            "---\nsummary: 自定义摘要 line\n---\n# Something else\n",
        )
        idx = build_knowledge_index(self.tmp)
        cycles = next(p for p in idx.partitions if p.name == "cycles")
        x = next(
            e for g in cycles.groups for e in g.entries if e.rel_path.endswith("x.md")
        )
        self.assertEqual(x.title, "自定义摘要 line")

    def test_csv_listed_by_name_not_parsed(self) -> None:
        idx = build_knowledge_index(self.tmp)
        trades = next(p for p in idx.partitions if p.name == "trades")
        apr = next(g for g in trades.groups if g.name == "2026-04")
        entry = apr.entries[0]
        # Title is the filename stem, never the CSV content.
        self.assertEqual(entry.title, "raw")

    def test_symbols_flat_strategy_single_group(self) -> None:
        idx = build_knowledge_index(self.tmp)
        symbols = next(p for p in idx.partitions if p.name == "symbols")
        self.assertEqual(len(symbols.groups), 1)
        titles = {e.title for e in symbols.groups[0].entries}
        self.assertIn("标的角色分类", titles)

    def test_backtests_grouped_by_strategy_dir(self) -> None:
        idx = build_knowledge_index(self.tmp)
        backtests = next(p for p in idx.partitions if p.name == "backtests")
        self.assertEqual([g.name for g in backtests.groups], ["oversold-sd-a7"])

    def test_render_markdown_contains_header_and_entries(self) -> None:
        idx = build_knowledge_index(self.tmp)
        md = render_index_markdown(idx)
        self.assertIn("知识库索引", md)
        self.assertIn("## cycles/", md)
        self.assertIn("002580.SZ.md", md)
        self.assertIn("⭐", md)  # overview marker

    def test_render_markdown_empty_kb_message(self) -> None:
        empty = Path(tempfile.mkdtemp())
        try:
            idx = build_knowledge_index(empty)
            md = render_index_markdown(idx)
            self.assertIn("知识库为空", md)
        finally:
            import shutil

            shutil.rmtree(empty, ignore_errors=True)

    def test_render_markdown_missing_root(self) -> None:
        idx = build_knowledge_index(self.tmp / "does-not-exist")
        self.assertFalse(idx.root_exists)
        md = render_index_markdown(idx)
        self.assertIn("不存在", md)

    def test_write_index_file_persists_markdown(self) -> None:
        idx = build_knowledge_index(self.tmp)
        path = write_index_file(idx)
        self.assertEqual(path, self.tmp / "_index.md")
        self.assertTrue(path.is_file())
        content = path.read_text(encoding="utf-8")
        self.assertIn("知识库索引", content)

    def test_utf8_truncation_does_not_produce_mojibake(self) -> None:
        # A markdown file whose 2 KB title-peek cuts mid multi-byte char must
        # not fall back to latin-1 mojibake — the heading is at the top and
        # must decode cleanly.
        cjk = "阶" * 700  # 3 bytes each -> ~2100 bytes, straddles the 2048 cut
        _write(self.tmp, "cycles/2026-02/big.md", f"# 阶段判别 {cjk}\nbody\n")
        idx = build_knowledge_index(self.tmp)
        cycles = next(p for p in idx.partitions if p.name == "cycles")
        big = next(
            e for g in cycles.groups for e in g.entries if e.rel_path.endswith("big.md")
        )
        self.assertIn("阶段判别", big.title)
        self.assertNotIn("é", big.title)  # no latin-1 mojibake

    def test_unreadable_file_collected_not_silently_dropped(self) -> None:
        # Make a file unreadable (no read permission) — it must surface in
        # ``skipped``, not vanish silently (AGENTS.md §错误可见性).
        secret = _write(self.tmp, "cycles/2026-01/secret.md", "# secret\n")
        secret.chmod(0o000)
        try:
            idx = build_knowledge_index(self.tmp)
            skipped_rel = [s[0] for s in idx.skipped]
            self.assertTrue(any("secret.md" in r for r in skipped_rel))
            md = render_index_markdown(idx)
            self.assertIn("跳过的文件", md)
            self.assertIn("secret.md", md)
        finally:
            secret.chmod(0o644)

    def test_hidden_files_skipped(self) -> None:
        _write(self.tmp, "cycles/2026-05/.DS_Store", "junk")
        idx = build_knowledge_index(self.tmp)
        all_rel = [
            e.rel_path for p in idx.partitions for g in p.groups for e in g.entries
        ]
        self.assertFalse(any(".DS_Store" in r for r in all_rel))

    def test_heading_less_markdown_flagged_as_weak(self) -> None:
        # A markdown file with no `# ` heading and no `summary:` degrades the
        # map (title falls back to bare stem). It must be flagged weak + ⚠️.
        _write(self.tmp, "cycles/2026-01/noheading.md", "just some prose\nno heading\n")
        idx = build_knowledge_index(self.tmp)
        cycles = next(p for p in idx.partitions if p.name == "cycles")
        weak = next(
            e for g in cycles.groups for e in g.entries if e.rel_path.endswith("noheading.md")
        )
        self.assertTrue(weak.weak)
        self.assertEqual(weak.title, "noheading")  # fell back to stem
        self.assertIn("cycles/2026-01/noheading.md", idx.weak_titles)
        md = render_index_markdown(idx)
        self.assertIn("⚠️", md)
        self.assertIn("弱标题", md)

    def test_heading_markdown_not_weak(self) -> None:
        _write(self.tmp, "cycles/2026-01/withheading.md", "# A real title\nbody\n")
        idx = build_knowledge_index(self.tmp)
        cycles = next(p for p in idx.partitions if p.name == "cycles")
        entry = next(
            e for g in cycles.groups for e in g.entries if e.rel_path.endswith("withheading.md")
        )
        self.assertFalse(entry.weak)
        self.assertEqual(entry.title, "A real title")

    def test_csv_never_weak(self) -> None:
        # Already in setUp via trades/2026-04/raw.csv — assert it isn't flagged.
        idx = build_knowledge_index(self.tmp)
        trades = next(p for p in idx.partitions if p.name == "trades")
        weak_csvs = [
            e for g in trades.groups for e in g.entries if e.weak
        ]
        self.assertEqual(weak_csvs, [])


class KnowledgeIndexToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        _write(self.tmp, "symbols/roles.md", "# 标的角色分类\n")
        _write(self.tmp, "cycles/2026-05/_overview.md", "# 2026-05 周期总览\n")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _run(self, **kwargs):
        tool = KnowledgeIndexTool()
        with mock.patch(
            "doyoutrade.tools._sandbox.knowledge_root", return_value=self.tmp
        ):
            return await tool.execute(**kwargs)

    async def test_rejects_unknown_kwarg(self) -> None:
        result = await self._run(foo="bar")
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_arguments]", result.text)
        self.assertIn("partition", result.text)

    async def test_rejects_unknown_partition(self) -> None:
        result = await self._run(partition="nonsense")
        self.assertTrue(result.is_error)
        self.assertIn("[error:unknown_partition]", result.text)

    async def test_happy_path_full(self) -> None:
        result = await self._run()
        self.assertFalse(result.is_error)
        self.assertIn("知识库索引", result.text)
        self.assertIn("## cycles/", result.text)
        self.assertIn("## symbols/", result.text)

    async def test_happy_path_scoped_partition(self) -> None:
        result = await self._run(partition="symbols")
        self.assertFalse(result.is_error)
        self.assertIn("## symbols/", result.text)
        self.assertNotIn("## cycles/", result.text)

    async def test_partition_enum_enforced_by_schema(self) -> None:
        # additionalProperties: False + enum on partition.
        tool = KnowledgeIndexTool()
        self.assertFalse(tool.parameters["additionalProperties"])
        self.assertEqual(
            set(tool.parameters["properties"].keys()), {"partition"}
        )

    async def test_missing_root_returns_soft_error_guidance(self) -> None:
        tool = KnowledgeIndexTool()
        missing = self.tmp / "no-such-dir"
        with mock.patch(
            "doyoutrade.tools._sandbox.knowledge_root", return_value=missing
        ):
            result = await tool.execute()
        # Soft error: guidance, not is_error=True (fresh env is not a failure).
        self.assertFalse(result.is_error)
        self.assertIn("knowledge_root_missing", result.text)


class KnowledgeCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        _write(self.tmp, "cycles/2026-05/_overview.md", "# 2026-05 周期总览\n")
        _write(self.tmp, "symbols/roles.md", "# 标的角色分类\n")
        self._patches = mock.patch.multiple(
            "doyoutrade.tools._sandbox",
            knowledge_root=mock.Mock(return_value=self.tmp),
        )
        self._patches.start()

    def tearDown(self) -> None:
        self._patches.stop()
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args: str) -> object:
        import json as _json

        from doyoutrade.cli.commands.knowledge import knowledge

        runner = CliRunner()
        result = runner.invoke(knowledge, ["index", *args])
        # Click result exit_code 0 == success.
        self.assertEqual(
            result.exit_code,
            0,
            msg=f"CLI failed: {result.output}\n{result.exception}",
        )
        return _json.loads(result.output)

    def test_index_default_prints_envelope(self) -> None:
        data = self._run()
        self.assertTrue(data["ok"])
        self.assertIn("index_markdown", data["data"])
        self.assertEqual(data["data"]["total_files"], 2)
        self.assertFalse(data["data"].get("index_path"))

    def test_index_partition_scope(self) -> None:
        data = self._run("--partition", "symbols")
        self.assertTrue(data["ok"])
        parts = data["data"]["partitions"]
        self.assertEqual([p["name"] for p in parts], ["symbols"])
        self.assertEqual(data["data"]["total_files"], 1)

    def test_index_refresh_writes_file(self) -> None:
        data = self._run("--refresh")
        self.assertTrue(data["ok"])
        index_path = data["data"]["index_path"]
        self.assertEqual(Path(index_path), self.tmp / "_index.md")
        self.assertTrue((self.tmp / "_index.md").is_file())
        content = (self.tmp / "_index.md").read_text(encoding="utf-8")
        self.assertIn("知识库索引", content)


class ToolRegistryRegistrationTests(unittest.TestCase):
    """The tool must be registered on the default agent tool surface."""

    def test_knowledge_index_in_default_registry(self) -> None:
        from doyoutrade.tools import build_default_tool_registry

        registry = build_default_tool_registry()
        names = {t.name for t in registry._tools.values()} if hasattr(registry, "_tools") else set()
        # Fallback: OperationRegistry may store tools differently; introspect via get.
        tool = registry.get("knowledge_index")
        self.assertIsNotNone(tool, "knowledge_index must be registered")
        self.assertEqual(tool.name, "knowledge_index")


if __name__ == "__main__":
    unittest.main()
