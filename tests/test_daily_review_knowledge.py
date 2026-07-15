"""Daily-review KB digest + journal write-back helpers.

Uses an isolated ``DOYOUTRADE_HOME`` so ``knowledge_root()`` points at a temp KB.
Verifies: empty-KB digest, populated digest (prior journal / roles / cycles /
trades CSV), and the write-back contract (synthesized ``# `` title, no silent
same-day overwrite, ``_index.md`` refresh).
"""

import json
import os
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from doyoutrade.knowledge.review import (
    build_daily_review_knowledge_digest,
    read_sentiment_timeline,
    upsert_sentiment_log,
    write_daily_review_journal,
)

_ASOF = date(2026, 6, 17)


class DailyReviewKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self._prev_home = os.environ.get("DOYOUTRADE_HOME")
        self._tmp = tempfile.mkdtemp()
        os.environ["DOYOUTRADE_HOME"] = self._tmp
        self.kb = Path(self._tmp) / "knowledge"

    def tearDown(self):
        if self._prev_home is None:
            os.environ.pop("DOYOUTRADE_HOME", None)
        else:
            os.environ["DOYOUTRADE_HOME"] = self._prev_home

    def _write(self, rel: str, content: str):
        p = self.kb / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    # --- digest -----------------------------------------------------------

    def test_empty_kb_digest(self):
        d = build_daily_review_knowledge_digest(_ASOF)
        self.assertFalse(d["root_exists"])
        self.assertIsNone(d["latest_journal"])
        self.assertIsNone(d["trades_csv"])
        self.assertEqual(d["errors"], [])

    def test_populated_digest(self):
        self._write("journal/2026/2026-06-16.md", "# 2026-06-16 复盘\n昨天减仓。")
        self._write("symbols/roles.md", "# 标的角色\n600000 = 银行龙头")
        self._write("cycles/2026-06/_overview.md", "# 6月情绪周期\n高潮期")
        self._write(
            "trades/broker/2026-06.csv",
            "date,symbol,side,qty,price\n2026-06-17,600000.SH,BUY,100,10.5\n",
        )
        d = build_daily_review_knowledge_digest(_ASOF)
        self.assertTrue(d["root_exists"])
        # latest PRIOR journal (06-16, not the asof file which doesn't exist yet)
        self.assertEqual(d["latest_journal"]["path"], "journal/2026/2026-06-16.md")
        self.assertIn("昨天减仓", d["latest_journal"]["content"])
        self.assertEqual(d["symbols_roles"]["path"], "symbols/roles.md")
        self.assertIn("情绪周期", d["cycles_overview"]["content"])
        self.assertEqual(d["trades_csv"]["columns"], ["date", "symbol", "side", "qty", "price"])
        self.assertEqual(d["trades_csv"]["row_count"], 1)
        self.assertFalse(d["trades_csv"]["truncated"])

    def test_digest_excludes_same_day_journal(self):
        # the asof-day journal must NOT be returned as "prior context"
        self._write("journal/2026/2026-06-17.md", "# 今天 复盘\nx")
        self._write("journal/2026/2026-06-10.md", "# 早些 复盘\ny")
        d = build_daily_review_knowledge_digest(_ASOF)
        self.assertEqual(d["latest_journal"]["path"], "journal/2026/2026-06-10.md")

    # --- write-back -------------------------------------------------------

    def test_write_synthesizes_title_and_refreshes_index(self):
        r = write_daily_review_journal(_ASOF, "今天小幅获利。", fired_at=datetime(2026, 6, 17, 15, 30))
        self.assertEqual(r["path"], "journal/2026/2026-06-17.md")
        self.assertFalse(r["appended"])
        self.assertTrue(r["title_synthesized"])
        self.assertTrue(r["index_refreshed"])
        body = (self.kb / r["path"]).read_text(encoding="utf-8")
        self.assertTrue(body.startswith("# 2026-06-17 复盘"))
        self.assertTrue((self.kb / "_index.md").exists())

    def test_existing_title_not_synthesized(self):
        r = write_daily_review_journal(_ASOF, "# 我的标题\n正文", fired_at=datetime(2026, 6, 17, 15, 30))
        self.assertFalse(r["title_synthesized"])
        body = (self.kb / r["path"]).read_text(encoding="utf-8")
        self.assertTrue(body.startswith("# 我的标题"))

    def test_same_day_second_write_appends_not_overwrites(self):
        write_daily_review_journal(_ASOF, "# 2026-06-17 复盘\n第一版", fired_at=datetime(2026, 6, 17, 15, 30))
        r2 = write_daily_review_journal(_ASOF, "尾盘补充", fired_at=datetime(2026, 6, 17, 16, 0))
        self.assertTrue(r2["appended"])
        body = (self.kb / r2["path"]).read_text(encoding="utf-8")
        # original preserved + supplement section added (no silent overwrite)
        self.assertIn("第一版", body)
        self.assertIn("复盘补充 16:00", body)
        self.assertIn("尾盘补充", body)
        # still exactly one H1 title
        self.assertEqual(body.count("\n# "), 0)
        self.assertTrue(body.startswith("# 2026-06-17 复盘"))


class SentimentLogTests(unittest.TestCase):
    """``upsert_sentiment_log`` — the machine-readable 情绪周期 daily log."""

    def setUp(self):
        self._prev_home = os.environ.get("DOYOUTRADE_HOME")
        self._tmp = tempfile.mkdtemp()
        os.environ["DOYOUTRADE_HOME"] = self._tmp
        self.kb = Path(self._tmp) / "knowledge"

    def tearDown(self):
        if self._prev_home is None:
            os.environ.pop("DOYOUTRADE_HOME", None)
        else:
            os.environ["DOYOUTRADE_HOME"] = self._prev_home

    def _log_path(self, month: str) -> Path:
        return self.kb / "cycles" / month / "_sentiment.jsonl"

    def _rows(self, month: str) -> list[dict]:
        return [
            json.loads(l)
            for l in self._log_path(month).read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]

    def test_new_row_created(self):
        r = upsert_sentiment_log(
            date(2026, 7, 3),
            {
                "label": "分歧加剧",
                "limit_up_count": 108,
                "limit_down_count": 19,
                "broken_board_count": 52,
                "broken_board_rate": 0.325,
                "max_streak": 4,
            },
        )
        self.assertEqual(r["path"], "cycles/2026-07/_sentiment.jsonl")
        self.assertTrue(r["upserted"])
        self.assertFalse(r["replaced"])
        self.assertEqual(r["row_count"], 1)
        rows = self._rows("2026-07")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-07-03")
        self.assertEqual(rows[0]["label"], "分歧加剧")
        self.assertEqual(rows[0]["limit_up_count"], 108)
        self.assertEqual(rows[0]["max_streak"], 4)
        # date is authoritative from asof, not from any payload date field.
        self.assertEqual(set(rows[0].keys()), {
            "date", "label", "limit_up_count", "limit_down_count",
            "broken_board_count", "broken_board_rate", "max_streak",
        })

    def test_idempotent_same_day_replaces_only_that_row(self):
        upsert_sentiment_log(date(2026, 7, 2), {"label": "退潮/低迷", "limit_up_count": 20})
        upsert_sentiment_log(date(2026, 7, 3), {"label": "分歧加剧", "limit_up_count": 108})
        # Re-fire 07-03 with corrected numbers → replaces that row in place.
        r = upsert_sentiment_log(date(2026, 7, 3), {"label": "发酵/活跃", "limit_up_count": 130})
        self.assertTrue(r["replaced"])
        self.assertEqual(r["row_count"], 2)  # 07-02 untouched, 07-03 replaced
        rows = self._rows("2026-07")
        by_date = {row["date"]: row for row in rows}
        self.assertEqual(by_date["2026-07-02"]["label"], "退潮/低迷")  # untouched
        self.assertEqual(by_date["2026-07-03"]["label"], "发酵/活跃")  # replaced
        self.assertEqual(by_date["2026-07-03"]["limit_up_count"], 130)
        # Rows stay ascending by date.
        self.assertEqual([row["date"] for row in rows], ["2026-07-02", "2026-07-03"])

    def test_malformed_existing_line_loud_skip_and_self_heal(self):
        path = self._log_path("2026-07")
        path.parent.mkdir(parents=True, exist_ok=True)
        good = json.dumps({"date": "2026-07-01", "label": "中性", "limit_up_count": 40})
        # A junk line + a valid JSON that isn't an object.
        path.write_text(good + "\nnot json at all\n[1,2,3]\n", encoding="utf-8")
        r = upsert_sentiment_log(date(2026, 7, 3), {"label": "分歧加剧", "limit_up_count": 108})
        self.assertEqual(r["dropped"], 2)  # the junk line + the non-object array
        rows = self._rows("2026-07")
        # good pre-existing row + new row survive; junk lines dropped.
        self.assertEqual([row["date"] for row in rows], ["2026-07-01", "2026-07-03"])

    def test_missing_numeric_fields_stay_none_not_zero(self):
        # A partial breadth (only a label) must not manufacture 0 counts.
        upsert_sentiment_log(date(2026, 7, 3), {"label": "分歧加剧"})
        row = self._rows("2026-07")[0]
        self.assertEqual(row["label"], "分歧加剧")
        self.assertIsNone(row["limit_up_count"])
        self.assertIsNone(row["max_streak"])


class SentimentTimelineReadTests(unittest.TestCase):
    """``read_sentiment_timeline`` — merge + sort + N-month window."""

    def setUp(self):
        self._prev_home = os.environ.get("DOYOUTRADE_HOME")
        self._tmp = tempfile.mkdtemp()
        os.environ["DOYOUTRADE_HOME"] = self._tmp
        self.kb = Path(self._tmp) / "knowledge"

    def tearDown(self):
        if self._prev_home is None:
            os.environ.pop("DOYOUTRADE_HOME", None)
        else:
            os.environ["DOYOUTRADE_HOME"] = self._prev_home

    def _write_log(self, month: str, rows: list[dict]):
        p = self.kb / "cycles" / month / "_sentiment.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8",
        )

    def test_empty_when_no_logs(self):
        self.assertEqual(read_sentiment_timeline(), {"items": []})

    def test_empty_when_cycles_dir_absent(self):
        # KB exists but no cycles/ partition → still a clean empty list.
        (self.kb / "journal").mkdir(parents=True)
        self.assertEqual(read_sentiment_timeline(months=6), {"items": []})

    def test_merge_multiple_months_sorted_ascending(self):
        self._write_log("2026-06", [
            {"date": "2026-06-30", "label": "退潮/低迷", "limit_up_count": 22},
            {"date": "2026-06-29", "label": "中性", "limit_up_count": 40},
        ])
        self._write_log("2026-07", [
            {"date": "2026-07-03", "label": "分歧加剧", "limit_up_count": 108},
            {"date": "2026-07-01", "label": "发酵/活跃", "limit_up_count": 70},
        ])
        out = read_sentiment_timeline(months=3)
        dates = [it["date"] for it in out["items"]]
        self.assertEqual(dates, ["2026-06-29", "2026-06-30", "2026-07-01", "2026-07-03"])
        # Projected onto the fixed schema.
        self.assertEqual(set(out["items"][0].keys()), {
            "date", "label", "limit_up_count", "limit_down_count",
            "broken_board_count", "broken_board_rate", "max_streak",
        })

    def test_month_window_drops_older_months(self):
        # Newest row is 2026-07; months=1 keeps only July.
        self._write_log("2026-05", [{"date": "2026-05-15", "label": "中性"}])
        self._write_log("2026-06", [{"date": "2026-06-20", "label": "退潮/低迷"}])
        self._write_log("2026-07", [{"date": "2026-07-03", "label": "分歧加剧"}])
        out = read_sentiment_timeline(months=1)
        self.assertEqual([it["date"] for it in out["items"]], ["2026-07-03"])
        # months=2 keeps June + July.
        out2 = read_sentiment_timeline(months=2)
        self.assertEqual([it["date"] for it in out2["items"]], ["2026-06-20", "2026-07-03"])

    def test_bad_lines_skipped_not_crash(self):
        p = self.kb / "cycles" / "2026-07" / "_sentiment.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"date": "2026-07-03", "label": "分歧加剧"}) + "\n"
            + "garbage\n"
            + json.dumps({"label": "no date field"}) + "\n"  # missing date → skip
            + json.dumps({"date": "2026-07-02", "label": "退潮/低迷"}) + "\n",
            encoding="utf-8",
        )
        out = read_sentiment_timeline(months=6)
        self.assertEqual([it["date"] for it in out["items"]], ["2026-07-02", "2026-07-03"])


if __name__ == "__main__":
    unittest.main()
