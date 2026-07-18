"""强势股时间线 CSV → 知识图谱确定性投影。

契约：

- 读 ``cycles/_strong_timeline.csv``（优先）或 ``cycles/强势股时间线.csv``。
- 每行投影至多三条边：``has_role`` / ``belongs_to_theme`` / ``traded_in``（活跃周期）。
- 启动/高点/退潮等事件进边 ``attrs``，不新增 ontology。
- 脏行进 ``warnings``，不静默丢弃。
- 时间线 ``has_role`` 不用 ``role|<symbol>`` state_key，避免与 roles.jsonl 抢单值状态组。
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from doyoutrade.knowledge.graph import (
    NODE_CYCLE,
    NODE_ROLE,
    NODE_SYMBOL,
    NODE_THEME,
    REL_BELONGS_TO_THEME,
    REL_HAS_ROLE,
    REL_TRADED_IN,
    SOURCE_TIMELINE,
    build_deterministic_projection,
)
from doyoutrade.knowledge.strong_timeline import (
    TIMELINE_CANDIDATE_RELPATHS,
    read_strong_timeline,
)


_HEADER = (
    "代码,名称,启动日,启动价(前复权),关注日,主升期望卖点,高点日,高点价(前复权),"
    "最高涨幅%,最晚行情结束日(在真正退潮日后面),拉升交易日(启动→高点),"
    "整段日历天(启动→结束),题材(待核),说明,标签"
)


def _write_timeline(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_HEADER.split(","))
        for row in rows:
            writer.writerow(row)


def _sample_row(
    *,
    symbol: str = "000957.SZ",
    name: str = "中通客车",
    start: str = "2022-05-13",
    start_price: str = "4.77",
    watch: str = "2022-05-25",
    sell_target: str = "",
    peak: str = "2022-07-19",
    peak_price: str = "27.97",
    gain: str = "486",
    end: str = "2022-09-23",
    rally_days: str = "47",
    calendar_days: str = "133",
    theme: str = "客车/新冠检测车",
    note: str = "5/13首板连续涨停到7/19见顶",
    tag: str = "龙头",
) -> list[str]:
    return [
        symbol,
        name,
        start,
        start_price,
        watch,
        sell_target,
        peak,
        peak_price,
        gain,
        end,
        rally_days,
        calendar_days,
        theme,
        note,
        tag,
    ]


class ReadStrongTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.kb = Path(self.tempdir.name) / "knowledge"
        self.kb.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_missing_file_returns_empty_items(self) -> None:
        result = read_strong_timeline(root=self.kb)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["warnings"], [])
        self.assertIsNone(result["relpath"])

    def test_prefers_canonical_english_filename(self) -> None:
        _write_timeline(
            self.kb / "cycles" / "强势股时间线.csv",
            [_sample_row(symbol="000001.SZ", name="旧文件")],
        )
        _write_timeline(
            self.kb / "cycles" / "_strong_timeline.csv",
            [_sample_row(symbol="000957.SZ", name="中通客车")],
        )
        result = read_strong_timeline(root=self.kb)
        self.assertEqual(result["relpath"], "cycles/_strong_timeline.csv")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["symbol"], "000957.SZ")

    def test_falls_back_to_chinese_filename(self) -> None:
        _write_timeline(
            self.kb / "cycles" / "强势股时间线.csv",
            [_sample_row()],
        )
        result = read_strong_timeline(root=self.kb)
        self.assertEqual(result["relpath"], "cycles/强势股时间线.csv")
        item = result["items"][0]
        self.assertEqual(item["symbol"], "000957.SZ")
        self.assertEqual(item["role"], "龙头")
        self.assertEqual(item["theme"], "客车/新冠检测车")
        self.assertEqual(item["start_date"], "2022-05-13")
        self.assertEqual(item["peak_price"], "27.97")
        self.assertEqual(item["line_number"], 2)

    def test_parses_quoted_note_with_commas(self) -> None:
        _write_timeline(
            self.kb / "cycles" / "_strong_timeline.csv",
            [
                _sample_row(
                    note="a,b,c 仍放量,其后退潮",
                    tag="",
                )
            ],
        )
        item = read_strong_timeline(root=self.kb)["items"][0]
        self.assertIn("a,b,c", item["note"])
        self.assertEqual(item["role"], "")

    def test_ongoing_end_date_marked(self) -> None:
        _write_timeline(
            self.kb / "cycles" / "_strong_timeline.csv",
            [
                _sample_row(
                    end="未退潮(进行中;截至2026-06-05)",
                    calendar_days="进行中",
                    tag="",
                )
            ],
        )
        item = read_strong_timeline(root=self.kb)["items"][0]
        self.assertTrue(item["ongoing"])
        self.assertIsNone(item["end_date"])

    def test_ongoing_via_calendar_days_when_end_empty(self) -> None:
        _write_timeline(
            self.kb / "cycles" / "_strong_timeline.csv",
            [
                _sample_row(
                    end="",
                    calendar_days="进行中",
                    tag="",
                )
            ],
        )
        item = read_strong_timeline(root=self.kb)["items"][0]
        self.assertTrue(item["ongoing"])
        self.assertIsNone(item["end_date"])

    def test_sell_target_with_chinese_commas_stays_one_field(self) -> None:
        """主升期望卖点里用中文逗号时，不应挤歪后面的高点列。"""
        _write_timeline(
            self.kb / "cycles" / "_strong_timeline.csv",
            [
                _sample_row(
                    sell_target="2026-01-13，妖股小票，风险极高",
                    peak="2026-01-14",
                    peak_price="27.72",
                    gain="377",
                    end="2026-01-28",
                )
            ],
        )
        item = read_strong_timeline(root=self.kb)["items"][0]
        self.assertEqual(item["peak_date"], "2026-01-14")
        self.assertEqual(item["peak_price"], "27.72")
        self.assertIn("妖股小票", item["sell_target"])

    def test_bad_symbol_row_surfaces_warning(self) -> None:
        _write_timeline(
            self.kb / "cycles" / "_strong_timeline.csv",
            [_sample_row(symbol="not-a-symbol")],
        )
        result = read_strong_timeline(root=self.kb)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["warnings"][0]["reason"], "timeline_row_bad_symbol")

    def test_candidate_relpaths_documented(self) -> None:
        self.assertEqual(
            TIMELINE_CANDIDATE_RELPATHS[0],
            "cycles/_strong_timeline.csv",
        )


class TimelineProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.kb = Path(self.tempdir.name) / "knowledge"
        (self.kb / "symbols").mkdir(parents=True)
        (self.kb / "symbols" / "roles.jsonl").write_text("", encoding="utf-8")
        (self.kb / "cycles" / "2026-03").mkdir(parents=True)
        (self.kb / "trades").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_projects_role_theme_and_cycle_edges_with_attrs(self) -> None:
        _write_timeline(
            self.kb / "cycles" / "强势股时间线.csv",
            [_sample_row()],
        )
        projection = build_deterministic_projection(self.kb)
        self.assertIn(SOURCE_TIMELINE, projection.source_hashes)

        node_keys = {(n.node_type, n.name) for n in projection.nodes}
        self.assertIn((NODE_SYMBOL, "000957.SZ"), node_keys)
        self.assertIn((NODE_ROLE, "龙头"), node_keys)
        self.assertIn((NODE_THEME, "客车/新冠检测车"), node_keys)
        self.assertIn((NODE_CYCLE, "2022-05"), node_keys)

        by_rel = {e.relation: e for e in projection.edges if e.source_key == SOURCE_TIMELINE}
        self.assertEqual(set(by_rel), {REL_HAS_ROLE, REL_BELONGS_TO_THEME, REL_TRADED_IN})

        role_edge = by_rel[REL_HAS_ROLE]
        self.assertIsNone(role_edge.state_key)
        self.assertEqual(role_edge.valid_at, datetime(2022, 5, 13))
        self.assertEqual(role_edge.invalid_at, datetime(2022, 9, 23))
        self.assertIn("强势股时间线.csv", role_edge.source_ref or "")

        cycle_edge = by_rel[REL_TRADED_IN]
        attrs = cycle_edge.attrs or {}
        self.assertEqual(attrs["start_date"], "2022-05-13")
        self.assertEqual(attrs["peak_date"], "2022-07-19")
        self.assertEqual(attrs["peak_price"], "27.97")
        self.assertEqual(attrs["max_gain_pct"], "486")
        self.assertEqual(attrs["end_date"], "2022-09-23")
        self.assertFalse(attrs["ongoing"])

    def test_skips_role_edge_when_tag_empty(self) -> None:
        _write_timeline(
            self.kb / "cycles" / "_strong_timeline.csv",
            [_sample_row(tag="", theme="AI/CPO算力")],
        )
        projection = build_deterministic_projection(self.kb)
        timeline_edges = [e for e in projection.edges if e.source_key == SOURCE_TIMELINE]
        relations = {e.relation for e in timeline_edges}
        self.assertNotIn(REL_HAS_ROLE, relations)
        self.assertIn(REL_BELONGS_TO_THEME, relations)
        self.assertIn(REL_TRADED_IN, relations)

    def test_multi_wave_same_symbol_distinct_dedupe_keys(self) -> None:
        _write_timeline(
            self.kb / "cycles" / "_strong_timeline.csv",
            [
                _sample_row(
                    symbol="002229.SZ",
                    name="鸿博股份(第一波)",
                    start="2023-02-07",
                    peak="2023-03-06",
                    end="2023-05-10",
                    theme="AI算力",
                    tag="龙头",
                ),
                _sample_row(
                    symbol="002229.SZ",
                    name="鸿博股份(第二波)",
                    start="2023-05-18",
                    peak="2023-06-20",
                    end="2023-07-24",
                    theme="AI算力",
                    tag="龙头",
                ),
            ],
        )
        projection = build_deterministic_projection(self.kb)
        role_edges = [
            e
            for e in projection.edges
            if e.source_key == SOURCE_TIMELINE and e.relation == REL_HAS_ROLE
        ]
        self.assertEqual(len(role_edges), 2)
        self.assertEqual(len({e.dedupe_key for e in role_edges}), 2)

    def test_timeline_does_not_steal_roles_jsonl_state_key(self) -> None:
        (self.kb / "symbols" / "roles.jsonl").write_text(
            '{"symbol":"000957.SZ","name":"中通客车","role":"杂毛",'
            '"updated_at":"2026-07-01"}\n',
            encoding="utf-8",
        )
        _write_timeline(
            self.kb / "cycles" / "_strong_timeline.csv",
            [_sample_row(tag="龙头")],
        )
        projection = build_deterministic_projection(self.kb)
        roles_edge = next(
            e
            for e in projection.edges
            if e.relation == REL_HAS_ROLE and e.state_key == "role|000957.SZ"
        )
        timeline_edge = next(
            e
            for e in projection.edges
            if e.source_key == SOURCE_TIMELINE and e.relation == REL_HAS_ROLE
        )
        self.assertEqual(roles_edge.dst[1], "杂毛")
        self.assertEqual(timeline_edge.dst[1], "龙头")
        self.assertIsNone(timeline_edge.state_key)

    def test_empty_timeline_still_registers_source_hash(self) -> None:
        projection = build_deterministic_projection(self.kb)
        self.assertIn(SOURCE_TIMELINE, projection.source_hashes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
