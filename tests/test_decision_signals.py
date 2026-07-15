"""功能 5 决策信号闭环: repository + pure evaluator + assistant tool tests."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from doyoutrade.backtest.decision_signal_eval import (
    evaluate_decision_signal,
    infer_direction_expected,
    parse_horizon_days,
)
from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.errors import RecordNotFoundError
from doyoutrade.persistence.models import Base
from doyoutrade.persistence.repositories import SqlAlchemyDecisionSignalRepository
from doyoutrade.tools import ToolResult
from doyoutrade.tools.decision_signal import RecordDecisionSignalTool


def _bar(day: str, *, open_=10.0, high=10.5, low=9.5, close=10.0):
    return {
        "symbol": "600519.SH",
        "timestamp": day,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1000.0,
    }


class DecisionSignalRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.repo = SqlAlchemyDecisionSignalRepository(self.session_factory)

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def _seed(self, **overrides):
        fields = {
            "run_id": "run-abc",
            "task_id": "task-1",
            "source": "backtest",
            "symbol": "600519.SH",
            "action": "buy",
            "horizon": "5d",
        }
        fields.update(overrides)
        return await self.repo.create_if_absent(**fields)

    async def test_create_if_absent_is_idempotent(self):
        first, created_first = await self._seed()
        again, created_again = await self._seed(reason="second attempt")
        self.assertTrue(created_first)
        self.assertFalse(created_again)
        self.assertEqual(first.id, again.id)
        self.assertTrue(first.id.startswith("dsig-"))
        self.assertEqual(first.dedupe_key, "run-abc|600519.SH|buy|5d")
        _, total = await self.repo.list_signals()
        self.assertEqual(total, 1)

    async def test_create_dedupe_scope_prefers_run_then_trace_then_session(self):
        snap, _ = await self._seed(run_id=None, trace_id="tr-1", session_id="sess-1")
        self.assertEqual(snap.dedupe_key, "tr-1|600519.SH|buy|5d")
        snap2, _ = await self._seed(
            run_id=None, trace_id=None, session_id="sess-1", action="sell"
        )
        self.assertEqual(snap2.dedupe_key, "sess-1|600519.SH|sell|5d")

    async def test_create_without_attribution_raises(self):
        with self.assertRaises(ValueError):
            await self._seed(run_id=None, trace_id=None, session_id=None)

    async def test_create_rejects_bad_enums_and_metadata(self):
        with self.assertRaises(ValueError):
            await self._seed(action="yolo")
        with self.assertRaises(ValueError):
            await self._seed(source="oracle")
        with self.assertRaises(ValueError):
            await self._seed(symbol="")
        with self.assertRaises(ValueError):
            await self._seed(metadata_json="not-a-dict")

    async def test_list_filters_and_pagination(self):
        await self._seed(run_id="run-1", symbol="600519.SH", action="buy")
        await self._seed(run_id="run-1", symbol="000001.SZ", action="sell")
        await self._seed(run_id="run-2", task_id="task-2", symbol="600519.SH", action="watch")

        by_run, total = await self.repo.list_signals(run_id="run-1")
        self.assertEqual(total, 2)
        self.assertEqual({s.run_id for s in by_run}, {"run-1"})

        by_symbol, total = await self.repo.list_signals(symbol="600519.SH")
        self.assertEqual(total, 2)

        by_task, total = await self.repo.list_signals(task_id="task-2")
        self.assertEqual(total, 1)
        self.assertEqual(by_task[0].action, "watch")

        page, total = await self.repo.list_signals(limit=2, offset=2)
        self.assertEqual(total, 3)
        self.assertEqual(len(page), 1)

        with self.assertRaises(ValueError):
            await self.repo.list_signals(status="nope")

    async def test_get_and_update_status(self):
        snap, _ = await self._seed()
        loaded = await self.repo.get_signal(snap.id)
        self.assertEqual(loaded.status, "active")
        updated = await self.repo.update_status(snap.id, "invalidated")
        self.assertEqual(updated.status, "invalidated")
        with self.assertRaises(ValueError):
            await self.repo.update_status(snap.id, "bogus")
        with self.assertRaises(RecordNotFoundError):
            await self.repo.get_signal("dsig-missing")
        with self.assertRaises(RecordNotFoundError):
            await self.repo.update_status("dsig-missing", "expired")

    async def test_expire_due_signals(self):
        past = datetime(2026, 1, 1)
        future = datetime(2099, 1, 1)
        overdue, _ = await self._seed(action="buy", expires_at=past)
        fresh, _ = await self._seed(action="sell", expires_at=future)
        no_expiry, _ = await self._seed(action="hold")
        count = await self.repo.expire_due_signals(datetime(2026, 7, 13))
        self.assertEqual(count, 1)
        self.assertEqual((await self.repo.get_signal(overdue.id)).status, "expired")
        self.assertEqual((await self.repo.get_signal(fresh.id)).status, "active")
        self.assertEqual((await self.repo.get_signal(no_expiry.id)).status, "active")
        # Second pass is a no-op (idempotent lazy expiry).
        self.assertEqual(await self.repo.expire_due_signals(datetime(2026, 7, 13)), 0)

    async def test_upsert_outcome_idempotent_unique_key(self):
        snap, _ = await self._seed()
        base = dict(
            signal_id=snap.id,
            horizon="5d",
            engine_version="v1",
            outcome="hit",
            direction_expected="up",
            direction_correct=True,
            anchor_date="2026-07-01",
            eval_window_days=5,
            entry_price=10.0,
            exit_price=11.0,
            return_pct=10.0,
        )
        first = await self.repo.upsert_outcome(**base)
        replaced = await self.repo.upsert_outcome(**{**base, "outcome": "miss", "return_pct": -3.0})
        self.assertEqual(first.id, replaced.id)
        self.assertEqual(replaced.outcome, "miss")
        outcomes = await self.repo.list_outcomes(snap.id)
        self.assertEqual(len(outcomes), 1)
        # A different horizon is a separate row.
        await self.repo.upsert_outcome(**{**base, "horizon": "10d"})
        self.assertEqual(len(await self.repo.list_outcomes(snap.id)), 2)
        with self.assertRaises(ValueError):
            await self.repo.upsert_outcome(**{**base, "outcome": "kaboom"})
        with self.assertRaises(ValueError):
            await self.repo.upsert_outcome(**{**base, "signal_id": ""})


class DecisionSignalEvalTests(unittest.TestCase):
    def test_infer_direction_expected(self):
        self.assertEqual(infer_direction_expected("buy"), "up")
        self.assertEqual(infer_direction_expected("add"), "up")
        self.assertEqual(infer_direction_expected("sell"), "down")
        self.assertEqual(infer_direction_expected("stop_loss"), "down")
        self.assertEqual(infer_direction_expected("take_profit"), "down")
        self.assertEqual(infer_direction_expected("hold"), "flat")
        self.assertEqual(infer_direction_expected("watch"), "flat")
        with self.assertRaises(ValueError):
            infer_direction_expected("yolo")

    def test_parse_horizon_days(self):
        self.assertEqual(parse_horizon_days("5d"), 5)
        self.assertEqual(parse_horizon_days("10"), 10)
        for bad in ("", "0d", "-3d", "monthly"):
            with self.assertRaises(ValueError):
                parse_horizon_days(bad)

    def _bars_rising(self):
        # Anchor 2026-07-01; five post-anchor days rising 10 -> 11.
        days = ["2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08"]
        bars = [_bar("2026-07-01", open_=9.9, close=10.0)]  # anchor day itself: ignored
        price = 10.0
        for day in days:
            bars.append(_bar(day, open_=price, high=price + 0.3, low=price - 0.1, close=price + 0.25))
            price += 0.25
        return bars

    def test_buy_hit_and_metrics(self):
        result = evaluate_decision_signal(
            {"action": "buy", "anchor_date": "2026-07-01", "horizon": "5d"},
            self._bars_rising(),
            horizon_days=5,
        )
        self.assertEqual(result["outcome"], "hit")
        self.assertTrue(result["direction_correct"])
        self.assertEqual(result["direction_expected"], "up")
        self.assertEqual(result["entry_price"], 10.0)  # first post-anchor open
        self.assertAlmostEqual(result["return_pct"], 12.5, places=3)
        self.assertGreater(result["max_gain_pct"], 0)
        self.assertLessEqual(result["max_drawdown_pct"], 0)

    def test_buy_miss_when_falling(self):
        bars = []
        price = 10.0
        for day in ["2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08"]:
            bars.append(_bar(day, open_=price, high=price + 0.05, low=price - 0.4, close=price - 0.3))
            price -= 0.3
        result = evaluate_decision_signal(
            {"action": "buy", "anchor_date": "2026-07-01"}, bars, horizon_days=5
        )
        self.assertEqual(result["outcome"], "miss")
        self.assertFalse(result["direction_correct"])

    def test_neutral_band(self):
        bars = [
            _bar(day, open_=10.0, high=10.05, low=9.95, close=10.02)
            for day in ["2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08"]
        ]
        result = evaluate_decision_signal(
            {"action": "buy", "anchor_date": "2026-07-01"}, bars, horizon_days=5
        )
        self.assertEqual(result["outcome"], "neutral")
        # flat prediction inside the band is a hit
        result_flat = evaluate_decision_signal(
            {"action": "hold", "anchor_date": "2026-07-01"}, bars, horizon_days=5
        )
        self.assertEqual(result_flat["outcome"], "hit")

    def test_target_touched_before_stop_is_hit(self):
        bars = self._bars_rising()
        result = evaluate_decision_signal(
            {
                "action": "buy",
                "anchor_date": "2026-07-01",
                "target_price": "10.3",  # touched on day 1 (high 10.3)
                "stop_loss": "9.0",
            },
            bars,
            horizon_days=5,
        )
        self.assertEqual(result["outcome"], "hit")

    def test_stop_touched_first_is_miss_even_if_return_positive(self):
        bars = [
            _bar("2026-07-02", open_=10.0, high=10.1, low=8.9, close=9.0),  # stop 9.0 touched
            _bar("2026-07-03", open_=9.0, high=12.0, low=9.0, close=11.9),
            _bar("2026-07-06", open_=11.9, high=12.0, low=11.5, close=11.9),
            _bar("2026-07-07", open_=11.9, high=12.0, low=11.5, close=11.9),
            _bar("2026-07-08", open_=11.9, high=12.0, low=11.5, close=11.9),
        ]
        result = evaluate_decision_signal(
            {
                "action": "buy",
                "anchor_date": "2026-07-01",
                "target_price": "11.5",
                "stop_loss": "9.0",
            },
            bars,
            horizon_days=5,
        )
        self.assertEqual(result["outcome"], "miss")

    def test_data_insufficient(self):
        bars = [_bar("2026-07-02"), _bar("2026-07-03")]
        result = evaluate_decision_signal(
            {"action": "buy", "anchor_date": "2026-07-01"}, bars, horizon_days=5
        )
        self.assertIsNone(result["outcome"])
        self.assertEqual(result["reason"], "data_insufficient")
        self.assertEqual(result["bars_available"], 2)
        self.assertEqual(result["bars_required"], 5)

    def test_invalid_inputs_raise(self):
        with self.assertRaises(ValueError):
            evaluate_decision_signal({"action": "buy", "anchor_date": "bad"}, [], horizon_days=5)
        with self.assertRaises(ValueError):
            evaluate_decision_signal(
                {"action": "buy", "anchor_date": "2026-07-01"}, [], horizon_days=0
            )
        with self.assertRaises(ValueError):
            evaluate_decision_signal(
                {
                    "action": "buy",
                    "anchor_date": "2026-07-01",
                    "target_price": "not-a-price",
                },
                [_bar("2026-07-02")] * 5,
                horizon_days=5,
            )


class _FakeSignalSnapshot:
    def __init__(self, **kw):
        self.id = kw.get("id", "dsig-fake00000001")
        self.symbol = kw.get("symbol", "600519.SH")
        self.action = kw.get("action", "buy")
        self.horizon = kw.get("horizon", "5d")
        self.status = kw.get("status", "active")
        self.expires_at = kw.get("expires_at")


class _FakeRepo:
    def __init__(self, created=True):
        self.created = created
        self.calls = []

    async def create_if_absent(self, **fields):
        self.calls.append(fields)
        return (
            _FakeSignalSnapshot(
                symbol=fields["symbol"],
                action=fields["action"],
                horizon=fields["horizon"],
                expires_at=fields.get("expires_at"),
            ),
            self.created,
        )


class RecordDecisionSignalToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_kwargs_rejected(self):
        tool = RecordDecisionSignalTool(decision_signal_repository=_FakeRepo())
        result = await tool.execute(
            session_id="sess-1", symbol="600519.SH", action="buy", tickerr="oops"
        )
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.is_error)
        self.assertIn("unknown_arguments", result.text)

    async def test_validation_errors(self):
        tool = RecordDecisionSignalTool(decision_signal_repository=_FakeRepo())
        cases = [
            {"symbol": "600519.SH", "action": "yolo"},
            {"symbol": "", "action": "buy"},
            {"symbol": "600519.SH", "action": "buy", "target_price": "1,800"},
            {"symbol": "600519.SH", "action": "buy", "confidence": 1.5},
            {"symbol": "600519.SH", "action": "buy", "horizon": "yearly"},
            {"symbol": "600519.SH", "action": "buy", "expires_in_days": 0},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                result = await tool.execute(session_id="sess-1", **kwargs)
                self.assertTrue(result.is_error)
                self.assertIn("validation_error", result.text)

    async def test_metadata_array_is_invalid_metadata_json(self):
        tool = RecordDecisionSignalTool(decision_signal_repository=_FakeRepo())
        result = await tool.execute(
            session_id="sess-1", symbol="600519.SH", action="buy", metadata=[1, 2]
        )
        self.assertTrue(result.is_error)
        self.assertIn("invalid_metadata_json", result.text)

    async def test_created_path_persists_normalized_fields(self):
        repo = _FakeRepo(created=True)
        tool = RecordDecisionSignalTool(decision_signal_repository=repo)
        result = await tool.execute(
            session_id="sess-1",
            symbol="600519.SH",
            action="buy",
            confidence=0.7,
            target_price="1800.00",
            stop_loss="1650",
            reason="breakout",
            expires_in_days=10,
            metadata={"theme": "白酒"},
        )
        self.assertFalse(result.is_error)
        self.assertIn("dsig-", result.text)
        self.assertIn('"status": "created"', result.text)
        fields = repo.calls[0]
        self.assertEqual(fields["session_id"], "sess-1")
        self.assertEqual(fields["source"], "assistant")
        self.assertEqual(fields["target_price"], "1800.00")
        self.assertEqual(fields["stop_loss"], "1650")
        self.assertEqual(fields["metadata_json"], {"theme": "白酒"})
        self.assertIsNotNone(fields["expires_at"])

    async def test_deduped_path(self):
        tool = RecordDecisionSignalTool(decision_signal_repository=_FakeRepo(created=False))
        result = await tool.execute(session_id="sess-1", symbol="600519.SH", action="buy")
        self.assertFalse(result.is_error)
        self.assertIn('"deduped": true', result.text)

    async def test_unwired_repo_is_structured_error(self):
        tool = RecordDecisionSignalTool(decision_signal_repository=None)
        result = await tool.execute(session_id="sess-1", symbol="600519.SH", action="buy")
        self.assertTrue(result.is_error)
        self.assertIn("decision_signal_unwired", result.text)

    async def test_missing_session_is_structured_error(self):
        tool = RecordDecisionSignalTool(decision_signal_repository=_FakeRepo())
        result = await tool.execute(symbol="600519.SH", action="buy")
        self.assertTrue(result.is_error)
        self.assertIn("decision_signal_unwired", result.text)


if __name__ == "__main__":
    unittest.main()
