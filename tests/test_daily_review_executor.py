"""``DailyReviewExecutor`` — the 每日复盘 cron task executor.

Covers validate_params, the non-trading-day skip, the happy path (compose →
journal write-back → deliver), and the distinct failure modes
(review_data_unavailable / review_compose_failed / empty_reply). Delivery is
patched so the test does not need full channel wiring; the journal write goes
to an isolated ``DOYOUTRADE_HOME``.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from doyoutrade.assistant.cron_executors.base import JobRunContext
from doyoutrade.assistant.cron_executors.daily_review import DailyReviewExecutor

_FIRED_AT = datetime(2026, 6, 17, 7, 30, tzinfo=timezone.utc)  # 15:30 Asia/Shanghai
_CTX = JobRunContext(cron_job_run_id="crun-1", job_id="task-1", fired_at=_FIRED_AT)


class _Svc:
    def __init__(self, reply="# 2026-06-17 复盘\n\n## 账户概览\n现金 1000。"):
        self._reply = reply

    async def create_session(self, *, agent_id, title):
        return {"session_id": "asst-sess-9", "title": title}

    async def send_message(self, *, session_id, content):
        self.last_framing = content
        return {"messages": [{"role": "assistant", "content": self._reply}]}


class _SvcWithJsonTail(_Svc):
    """An agent that emits the trailing ```json block the framing asks for."""

    def __init__(self):
        super().__init__(
            reply=(
                "# 2026-06-17 复盘\n\n## 账户概览\n现金 1000。\n\n"
                "```json\n"
                '{\n'
                '  "source": "llm",\n'
                '  "ai_status": "ok",\n'
                '  "summary": "今日无重大动作",\n'
                '  "diagnosis": [],\n'
                '  "recommendations": ["明日观察"],\n'
                '  "cautions": ["本复盘只读"]\n'
                "}\n"
                "```"
            )
        )


class _RaisingSvc(_Svc):
    async def send_message(self, *, session_id, content):
        raise RuntimeError("model route down")


class _CronRepo:
    async def get_job(self, jid):
        return {"id": jid, "name": "每日收盘复盘"}


async def _stmt_ok(account_id, asof, captured_at):
    return {
        "asof": asof.isoformat(),
        "source": "broker",
        "account": {"account": {"cash": "1000", "equity": "5000"}, "positions": []},
        "asset": None,
        "trades": [],
        "trade_count": 0,
        "errors": [],
    }


async def _stmt_with_holdings(account_id, asof, captured_at):
    return {
        "asof": asof.isoformat(),
        "source": "broker",
        "account": {
            "account": {"cash": "1000", "equity": "5000"},
            "positions": [
                {"symbol": "600519.SH", "name": "贵州茅台", "quantity": 100},
                {"symbol": "000001.SZ", "name": "平安银行", "quantity": 200},
            ],
        },
        "asset": None,
        "trades": [],
        "trade_count": 0,
        "errors": [],
    }


# --- fake market four-dimension providers ---------------------------------


class _Breadth:
    """Minimal MarketBreadth-shaped object for the fake breadth provider."""

    def __init__(self):
        self.trade_date = "20260617"
        self.limit_up = list(range(108))  # counts derive from list len
        self.limit_up_count = 108
        self.limit_down_count = 19
        self.broken_board_count = 52
        self.broken_board_rate = 0.325
        self.max_streak = 4
        self.ladder = {"2": 30, "3": 10, "4": 1}
        self.pool_errors = {}


class _FakeBreadthProvider:
    def __init__(self, breadth=None, exc=None):
        self._breadth = breadth or _Breadth()
        self._exc = exc

    async def fetch_market_breadth(self, trade_date):
        if self._exc is not None:
            raise self._exc
        return self._breadth


class _HeatRow:
    def __init__(self, board_name, change_pct, leader_stock="", leader_change_pct=None):
        self.board_name = board_name
        self.change_pct = change_pct
        self.leader_stock = leader_stock
        self.leader_change_pct = leader_change_pct


class _FakeSectorProvider:
    def __init__(self, rows=None, exc=None):
        self._rows = rows if rows is not None else [
            _HeatRow("AI算力", 5.2, "寒武纪", 20.0),
            _HeatRow("固态电池", 3.1, "某龙头", 10.0),
            _HeatRow("低估值", None),  # None change_pct → excluded from ranking
        ]
        self._exc = exc

    async def get_sector_heat(self, sector_type):
        assert sector_type == "concept"
        if self._exc is not None:
            raise self._exc
        return self._rows


class _LhbRow:
    def __init__(self, symbol, name, reason="", change_pct=None, net_buy_amount=None):
        self.symbol = symbol
        self.name = name
        self.reason = reason
        self.change_pct = change_pct
        self.net_buy_amount = net_buy_amount


class _FakeDragonProvider:
    def __init__(self, rows=None, exc=None):
        self._rows = rows if rows is not None else [
            _LhbRow("600519.SH", "贵州茅台", "涨幅偏离", 8.0, 1.2e8),
            _LhbRow("300750.SZ", "宁德时代", "换手率", 5.0, 3.0e7),  # not held → filtered
        ]
        self._exc = exc

    async def fetch_dragon_tiger(self, start_date, end_date):
        if self._exc is not None:
            raise self._exc
        return self._rows


async def _stmt_raises(account_id, asof, captured_at):
    raise RuntimeError("no default account")


async def _is_trading(asof):
    return True


async def _not_trading(asof):
    return False


def _patch_deliver(status="delivered", info=None):
    async def _fake(svc, *, target_session_id, content, cron_job_id, cron_job_run_id, cron_task_kind):
        _fake.content = content
        _fake.target = target_session_id
        return (status, info or {})

    return mock.patch(
        "doyoutrade.assistant.cron_executors.daily_review.deliver_assistant_message_to_session",
        _fake,
    ), _fake


class DailyReviewValidateTests(unittest.TestCase):
    def setUp(self):
        self.ex = DailyReviewExecutor(
            assistant_service=_Svc(),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_ok,
            trading_day_checker=_is_trading,
        )

    def test_missing_agent_id(self):
        self.assertEqual(self.ex.validate_params({})["error_code"], "missing_agent_id")

    def test_ok(self):
        self.assertIsNone(self.ex.validate_params({"agent_id": "a1"}))

    def test_invalid_account_id(self):
        err = self.ex.validate_params({"agent_id": "a1", "account_id": 123})
        self.assertEqual(err["error_code"], "invalid_account_id")

    def test_invalid_target(self):
        err = self.ex.validate_params({"agent_id": "a1", "target_session_id": 5})
        self.assertEqual(err["error_code"], "invalid_target_session_id")

    def test_invalid_user_request(self):
        err = self.ex.validate_params({"agent_id": "a1", "user_request": {"x": 1}})
        self.assertEqual(err["error_code"], "invalid_user_request")

    def test_non_dict_params(self):
        self.assertEqual(self.ex.validate_params([])["error_code"], "invalid_task_params")


# These run() tests don't inject market providers, so the executor would
# lazy-default to the REAL akshare impls and hit the network (RemoteDisconnected
# → retry sleeps = minutes, flaky, offline-breaking). Patch the lazy-default
# source classes to the offline fakes so the market gather stays deterministic
# and fast; these tests assert on compose/journal/fallback, not market content.
@mock.patch("doyoutrade.data.lhb_akshare.AkshareDragonTigerProvider", _FakeDragonProvider)
@mock.patch("doyoutrade.data.sector_akshare.AkshareSectorProvider", _FakeSectorProvider)
@mock.patch("doyoutrade.data.limit_pool_akshare.AkshareMarketBreadthProvider", _FakeBreadthProvider)
class DailyReviewRunTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_non_trading_day_skips(self):
        ex = DailyReviewExecutor(
            assistant_service=_Svc(),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_ok,
            trading_day_checker=_not_trading,
        )
        r = await ex.run({"agent_id": "a1", "target_session_id": "s1"}, _CTX)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.delivery_status, "suppressed")
        self.assertEqual(r.data["reason"], "not_trading_day")
        # no journal written on a skip
        self.assertFalse((self.kb / "journal").exists())

    async def test_happy_path_writes_journal_and_delivers(self):
        patch, fake = _patch_deliver("delivered")
        ex = DailyReviewExecutor(
            assistant_service=_Svc(),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_ok,
            trading_day_checker=_is_trading,
        )
        with patch:
            r = await ex.run({"agent_id": "a1", "target_session_id": "s1"}, _CTX)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.delivery_status, "delivered")
        self.assertEqual(r.agent_session_id, "asst-sess-9")
        self.assertEqual(r.data["journal"]["path"], "journal/2026/2026-06-17.md")
        # journal persisted + delivered content match the composed reply
        body = (self.kb / "journal" / "2026" / "2026-06-17.md").read_text(encoding="utf-8")
        self.assertTrue(body.startswith("# 2026-06-17 复盘"))
        self.assertIn("账户概览", fake.content)
        # P0/P1: deterministic analytics layer is computed and surfaced.
        self.assertIn("metrics", r.data)
        self.assertIn("diagnostics", r.data)
        self.assertEqual(r.data["metrics"]["asof"], "2026-06-17")
        self.assertEqual(r.data["fallback_applied"], False)
        self.assertIsNone(r.data["fallback_reason"])

    async def test_trailing_json_block_is_parsed_and_surfaced(self):
        # P4-light: the LLM was asked to emit a trailing ```json block; the
        # executor parses it and surfaces the AI's own summary/diagnosis/
        # recommendations in data['ai_structured'] (alongside the rule-derived
        # diagnostics). Combined with cron_job_runs.pre_result_json this gives
        # a queryable structured review history with no schema change.
        patch, _fake = _patch_deliver("delivered")
        ex = DailyReviewExecutor(
            assistant_service=_SvcWithJsonTail(),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_ok,
            trading_day_checker=_is_trading,
        )
        with patch:
            r = await ex.run({"agent_id": "a1", "target_session_id": "s1"}, _CTX)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.data["fallback_applied"], False)
        self.assertIsNotNone(r.data["ai_structured"])
        self.assertEqual(r.data["ai_structured"]["source"], "llm")
        self.assertEqual(r.data["ai_structured"]["summary"], "今日无重大动作")
        self.assertEqual(r.data["ai_structured"]["recommendations"], ["明日观察"])

    async def test_no_trailing_json_yields_none_not_failure(self):
        # A reply with no trailing block (legacy agent / model that ignored
        # the instruction) must NOT fail the fire; ai_structured is None and
        # the rule-derived diagnostics still carry the structured signal.
        patch, _fake = _patch_deliver("delivered")
        ex = DailyReviewExecutor(
            assistant_service=_Svc(reply="# 2026-06-17 复盘\n\nplain prose, no JSON"),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_ok,
            trading_day_checker=_is_trading,
        )
        with patch:
            r = await ex.run({"agent_id": "a1"}, _CTX)
        self.assertEqual(r.status, "ok")
        self.assertIsNone(r.data["ai_structured"])
        # Rule-derived structure is still present.
        self.assertIn("diagnostics", r.data)

    async def test_statement_unavailable_fails(self):
        ex = DailyReviewExecutor(
            assistant_service=_Svc(),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_raises,
            trading_day_checker=_is_trading,
        )
        r = await ex.run({"agent_id": "a1"}, _CTX)
        self.assertEqual(r.status, "failed")
        self.assertTrue(r.error.startswith("review_data_unavailable"))

    async def test_compose_failure_applies_fallback(self):
        # P5: when the LLM turn raises, a Python-composed fallback journal
        # (from review_analytics) is written + delivered; the user is never
        # left empty-handed. The original failure stays visible via the
        # fallback_reason flag + the review_compose_failed debug event.
        patch, fake = _patch_deliver("delivered")
        ex = DailyReviewExecutor(
            assistant_service=_RaisingSvc(),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_ok,
            trading_day_checker=_is_trading,
        )
        with patch:
            r = await ex.run({"agent_id": "a1", "target_session_id": "s1"}, _CTX)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.delivery_status, "delivered")
        self.assertEqual(r.data["fallback_applied"], True)
        self.assertEqual(r.data["fallback_reason"], "compose_failed")
        # Fallback journal is persisted to the same KB path.
        body = (self.kb / "journal" / "2026" / "2026-06-17.md").read_text(encoding="utf-8")
        self.assertTrue(body.startswith("# 2026-06-17 复盘"))
        self.assertIn("reason=compose_failed", body)
        self.assertIn("兜底", body)
        # And delivered (the patched deliver captured the content).
        self.assertIn("兜底", fake.content)

    async def test_empty_reply_applies_fallback(self):
        # Same fallback path, but triggered by an empty LLM reply rather
        # than a thrown exception.
        patch, fake = _patch_deliver("delivered")
        ex = DailyReviewExecutor(
            assistant_service=_Svc(reply="   "),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_ok,
            trading_day_checker=_is_trading,
        )
        with patch:
            r = await ex.run({"agent_id": "a1", "target_session_id": "s1"}, _CTX)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.data["fallback_applied"], True)
        self.assertEqual(r.data["fallback_reason"], "empty_reply")
        body = (self.kb / "journal" / "2026" / "2026-06-17.md").read_text(encoding="utf-8")
        self.assertTrue(body.startswith("# 2026-06-17 复盘"))
        self.assertIn("reason=empty_reply", body)

    async def test_no_trading_checker_proceeds(self):
        # checker omitted → no calendar gate, review still runs
        patch, fake = _patch_deliver("delivered")
        ex = DailyReviewExecutor(
            assistant_service=_Svc(),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_ok,
            trading_day_checker=None,
        )
        with patch:
            r = await ex.run({"agent_id": "a1", "target_session_id": "s1"}, _CTX)
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.data["asof"], "2026-06-17")


class DailyReviewMarketGatherTests(unittest.IsolatedAsyncioTestCase):
    """Market four-dimension soft-gather (success + failure) + sentiment log."""

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

    async def test_market_gather_success_populates_and_writes_sentiment_log(self):
        import json

        patch, fake = _patch_deliver("delivered")
        ex = DailyReviewExecutor(
            assistant_service=_Svc(),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_with_holdings,
            trading_day_checker=_is_trading,
            market_breadth_provider=_FakeBreadthProvider(),
            sector_provider=_FakeSectorProvider(),
            dragon_tiger_provider=_FakeDragonProvider(),
        )
        with patch:
            r = await ex.run({"agent_id": "a1", "target_session_id": "s1"}, _CTX)
        self.assertEqual(r.status, "ok")
        market = r.data["market"]
        self.assertIsNotNone(market)
        # breadth
        self.assertEqual(market["breadth"]["limit_up_count"], 108)
        self.assertEqual(market["breadth"]["sentiment"]["label"], "分歧加剧")
        # sector heat top: None-change board excluded, sorted desc
        boards = [s["board_name"] for s in market["sector_heat_top"]]
        self.assertEqual(boards, ["AI算力", "固态电池"])
        # holdings lhb: only held symbols kept (600519 held, 300750 filtered)
        lhb_syms = [h["symbol"] for h in market["holdings_lhb"]]
        self.assertEqual(lhb_syms, ["600519.SH"])
        # framing (the composer prompt) includes the market block + the
        # 今日市场 section (the delivered reply is the fake agent's fixed text).
        self.assertIn("今日市场", ex._svc.last_framing)
        self.assertIn("分歧加剧", ex._svc.last_framing)
        # sentiment log written idempotently to cycles/<month>/_sentiment.jsonl
        log = self.kb / "cycles" / "2026-06" / "_sentiment.jsonl"
        self.assertTrue(log.exists())
        rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-06-17")
        self.assertEqual(rows[0]["label"], "分歧加剧")
        self.assertEqual(rows[0]["limit_up_count"], 108)

    async def test_market_gather_all_fail_review_still_ok(self):
        from http.client import RemoteDisconnected

        patch, fake = _patch_deliver("delivered")
        exc = RemoteDisconnected("Remote end closed connection without response")
        ex = DailyReviewExecutor(
            assistant_service=_Svc(),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_with_holdings,
            trading_day_checker=_is_trading,
            market_breadth_provider=_FakeBreadthProvider(exc=exc),
            sector_provider=_FakeSectorProvider(exc=exc),
            dragon_tiger_provider=_FakeDragonProvider(exc=exc),
        )
        with patch:
            r = await ex.run({"agent_id": "a1", "target_session_id": "s1"}, _CTX)
        # The review must NOT fail because the market endpoints rate-limited.
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.delivery_status, "delivered")
        self.assertIsNone(r.data["market"])
        # Framing tells the composer market data is unavailable (no fabrication).
        self.assertIn("未采集到今日市场四维数据", ex._svc.last_framing)
        # No sentiment log written when breadth failed.
        self.assertFalse((self.kb / "cycles").exists())

    async def test_market_partial_breadth_ok_sector_fails(self):
        # breadth succeeds (→ sentiment log written) even if sector heat fails.
        from http.client import RemoteDisconnected

        patch, fake = _patch_deliver("delivered")
        ex = DailyReviewExecutor(
            assistant_service=_Svc(),
            cron_job_repository=_CronRepo(),
            statement_provider=_stmt_ok,  # no holdings → lhb skipped
            trading_day_checker=_is_trading,
            market_breadth_provider=_FakeBreadthProvider(),
            sector_provider=_FakeSectorProvider(exc=RemoteDisconnected("boom")),
            dragon_tiger_provider=_FakeDragonProvider(),
        )
        with patch:
            r = await ex.run({"agent_id": "a1"}, _CTX)
        self.assertEqual(r.status, "ok")
        market = r.data["market"]
        self.assertIn("breadth", market)
        self.assertNotIn("sector_heat_top", market)  # failed dimension absent
        # no holdings → lhb dimension not attempted
        self.assertNotIn("holdings_lhb", market)
        self.assertTrue((self.kb / "cycles" / "2026-06" / "_sentiment.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
