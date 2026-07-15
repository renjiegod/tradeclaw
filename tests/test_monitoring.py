"""Unit tests for the realtime stock monitoring (盯盘) subsystem.

Covers the condition-tree validator (stable error_codes), the 6 preset
detectors + intraday state, the full-eval tree walk + field predicates, the
dedup/cooldown gate, and the MonitorDaemon fire path (rising-edge → dedup →
persist → deliver) including the seal-data-missing skip.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from doyoutrade.core.models import QuoteSnapshot
from doyoutrade.monitoring.conditions import (
    MonitorConditionError,
    iter_referenced_presets,
    validate_condition_tree,
)
from doyoutrade.monitoring.dedup import DedupGate
from doyoutrade.monitoring.evaluator import EvalContext, MonitorEvalError, evaluate_tree
from doyoutrade.monitoring.state import IntradayStateStore, trading_day_for


def _ctx(snapshot, state, now=None):
    return EvalContext(snapshot=snapshot, state=state, now=now or datetime(2026, 6, 22, 2, 0, tzinfo=timezone.utc))


def _fresh_state(symbol="000001.SZ", day="2026-06-22"):
    return IntradayStateStore().get_or_reset(symbol, day)


class ConditionValidatorTests(unittest.TestCase):
    def test_valid_preset_leaf_normalizes_params(self):
        out = validate_condition_tree({"preset": "limit_up"})
        self.assertEqual(out, {"preset": "limit_up", "params": {}})

    def test_valid_composite_tree(self):
        tree = {
            "op": "or",
            "children": [
                {"preset": "limit_up_open"},
                {"predicate": {"field": "change_pct", "op": ">=", "value": 9.0}},
            ],
        }
        out = validate_condition_tree(tree)
        self.assertEqual(out["op"], "or")
        self.assertEqual(len(out["children"]), 2)

    def test_error_codes(self):
        cases = {
            "condition_empty": {},
            "condition_op_unknown": {"op": "xor", "children": [{"preset": "limit_up"}]},
            "condition_children_empty": {"op": "and", "children": []},
            "condition_preset_unknown": {"preset": "nope"},
            "condition_params_invalid": {"preset": "limit_up", "params": 5},
            "condition_predicate_invalid": {"predicate": {"field": "foo", "op": ">", "value": 1}},
            "condition_node_invalid": {"foo": "bar"},
        }
        for code, tree in cases.items():
            with self.assertRaises(MonitorConditionError) as cm:
                validate_condition_tree(tree)
            self.assertEqual(cm.exception.error_code, code, f"tree={tree!r}")

    def test_predicate_bad_op_and_value(self):
        with self.assertRaises(MonitorConditionError) as cm:
            validate_condition_tree({"predicate": {"field": "price", "op": "~", "value": 1}})
        self.assertEqual(cm.exception.error_code, "condition_predicate_invalid")
        with self.assertRaises(MonitorConditionError):
            validate_condition_tree({"predicate": {"field": "price", "op": ">", "value": "x"}})

    def test_depth_exceeded(self):
        node = {"preset": "limit_up"}
        for _ in range(10):
            node = {"op": "and", "children": [node]}
        with self.assertRaises(MonitorConditionError) as cm:
            validate_condition_tree(node)
        self.assertEqual(cm.exception.error_code, "condition_depth_exceeded")

    def test_iter_referenced_presets(self):
        tree = {
            "op": "and",
            "children": [
                {"preset": "limit_up"},
                {"op": "or", "children": [{"preset": "limit_up_open"}, {"predicate": {"field": "price", "op": ">", "value": 1}}]},
            ],
        }
        self.assertEqual(iter_referenced_presets(tree), {"limit_up", "limit_up_open"})


class PresetDetectorTests(unittest.TestCase):
    def test_limit_up_hit_and_miss(self):
        st = _fresh_state()
        # at limit (prev 10 → +10% = 11.0)
        trig, _ = evaluate_tree({"preset": "limit_up"}, _ctx(QuoteSnapshot(symbol="000001.SZ", price=11.0, prev_close=10.0, limit_up_price=11.0), st))
        self.assertTrue(trig)
        trig2, _ = evaluate_tree({"preset": "limit_up"}, _ctx(QuoteSnapshot(symbol="000001.SZ", price=10.5, prev_close=10.0, limit_up_price=11.0), st))
        self.assertFalse(trig2)

    def test_limit_down_hit(self):
        st = _fresh_state()
        trig, _ = evaluate_tree({"preset": "limit_down"}, _ctx(QuoteSnapshot(symbol="000001.SZ", price=9.0, prev_close=10.0, limit_down_price=9.0), st))
        self.assertTrue(trig)

    def test_limit_pct_override_for_st(self):
        # 5% ST name: prev 10 → limit 10.5; default a_share_limit_pct would say 10% (11.0)
        st = _fresh_state()
        snap = QuoteSnapshot(symbol="000001.SZ", price=10.5, prev_close=10.0)  # no precomputed limit
        trig, leaves = evaluate_tree({"preset": "limit_up", "params": {"limit_pct": 0.05}}, _ctx(snap, st))
        self.assertTrue(trig)
        self.assertEqual(leaves[0].diagnostics["limit_price"], 10.5)

    def test_seal_shrink_uses_intraday_peak(self):
        store = IntradayStateStore()
        day = "2026-06-22"
        # tick1 sealed at limit with big seal → no peak yet, fold sets peak
        s1 = QuoteSnapshot(symbol="000001.SZ", price=11.0, prev_close=10.0, limit_up_price=11.0, bid_vol1=1_000_000)
        st = store.get_or_reset("000001.SZ", day)
        t1, _ = evaluate_tree({"preset": "limit_up_seal_shrink"}, _ctx(s1, st))
        self.assertFalse(t1)  # no prior peak
        st.fold_snapshot(s1)
        self.assertEqual(st.seal_peak_bid, 1_000_000)
        # tick2 seal dropped to 300k (70% drop) → triggers (default 50%)
        s2 = QuoteSnapshot(symbol="000001.SZ", price=11.0, prev_close=10.0, limit_up_price=11.0, bid_vol1=300_000)
        st = store.get_or_reset("000001.SZ", day)
        t2, leaves = evaluate_tree({"preset": "limit_up_seal_shrink"}, _ctx(s2, st))
        self.assertTrue(t2)
        self.assertAlmostEqual(leaves[0].diagnostics["drop_pct"], 0.7, places=4)

    def test_seal_shrink_skips_when_seal_missing(self):
        st = _fresh_state()
        st.seal_peak_bid = 1_000_000  # had a peak
        snap = QuoteSnapshot(symbol="000001.SZ", price=11.0, prev_close=10.0, limit_up_price=11.0, bid_vol1=None)
        trig, leaves = evaluate_tree({"preset": "limit_up_seal_shrink"}, _ctx(snap, st))
        self.assertFalse(trig)
        self.assertEqual(leaves[0].diagnostics.get("skipped_reason"), "seal_vol_missing")

    def test_limit_up_open_transition(self):
        st = _fresh_state()
        st.last_sealed_up = True  # was sealed last tick
        snap = QuoteSnapshot(symbol="000001.SZ", price=10.8, prev_close=10.0, limit_up_price=11.0)
        trig, _ = evaluate_tree({"preset": "limit_up_open"}, _ctx(snap, st))
        self.assertTrue(trig)
        # not sealed before → no open
        st2 = _fresh_state()
        trig2, _ = evaluate_tree({"preset": "limit_up_open"}, _ctx(snap, st2))
        self.assertFalse(trig2)


class PredicateTests(unittest.TestCase):
    def test_predicate_on_snapshot_field(self):
        st = _fresh_state()
        snap = QuoteSnapshot(symbol="000001.SZ", price=12.3, prev_close=10.0, change_pct=23.0)
        trig, leaves = evaluate_tree({"predicate": {"field": "change_pct", "op": ">=", "value": 9.0}}, _ctx(snap, st))
        self.assertTrue(trig)
        self.assertEqual(leaves[0].diagnostics["actual"], 23.0)

    def test_predicate_field_unavailable_skips(self):
        st = _fresh_state()
        snap = QuoteSnapshot(symbol="000001.SZ", price=None)
        trig, leaves = evaluate_tree({"predicate": {"field": "price", "op": ">", "value": 1}}, _ctx(snap, st))
        self.assertFalse(trig)
        self.assertEqual(leaves[0].diagnostics.get("skipped_reason"), "field_unavailable")

    def test_full_eval_collects_all_leaves(self):
        st = _fresh_state()
        snap = QuoteSnapshot(symbol="000001.SZ", price=11.0, prev_close=10.0, limit_up_price=11.0)
        tree = {"op": "or", "children": [{"preset": "limit_up"}, {"predicate": {"field": "price", "op": ">", "value": 99}}]}
        trig, leaves = evaluate_tree(tree, _ctx(snap, st))
        self.assertTrue(trig)
        self.assertEqual(len(leaves), 2)  # both leaves evaluated despite OR short-circuit potential

    def test_malformed_tree_raises_at_eval(self):
        st = _fresh_state()
        with self.assertRaises(MonitorEvalError):
            evaluate_tree({"op": "and", "children": "nope"}, _ctx(QuoteSnapshot(symbol="x"), st))


class DedupGateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 22, 2, 0, tzinfo=timezone.utc)
        self.key = ("mon-1", "000001.SZ", "limit_up")

    def test_rising_edge_only(self):
        g = DedupGate()
        fire, _ = g.should_fire(self.key, triggered=True, now=self.now, cooldown_seconds=0)
        self.assertTrue(fire)
        g.record_fired(self.key, now=self.now)
        # still True next tick → no fire
        fire2, reason = g.should_fire(self.key, triggered=True, now=self.now + timedelta(seconds=1), cooldown_seconds=0)
        self.assertFalse(fire2)
        self.assertEqual(reason, "edge_not_rising")
        # drops to False (re-arms)
        g.should_fire(self.key, triggered=False, now=self.now + timedelta(seconds=2), cooldown_seconds=0)
        fire3, _ = g.should_fire(self.key, triggered=True, now=self.now + timedelta(seconds=3), cooldown_seconds=0)
        self.assertTrue(fire3)

    def test_cooldown_floor(self):
        g = DedupGate()
        g.should_fire(self.key, triggered=True, now=self.now, cooldown_seconds=300)
        g.record_fired(self.key, now=self.now)
        g.should_fire(self.key, triggered=False, now=self.now + timedelta(seconds=10), cooldown_seconds=300)
        # re-armed edge but within cooldown → suppressed
        fire, reason = g.should_fire(self.key, triggered=True, now=self.now + timedelta(seconds=60), cooldown_seconds=300)
        self.assertFalse(fire)
        self.assertEqual(reason, "within_cooldown")
        # after cooldown → fires
        g.should_fire(self.key, triggered=False, now=self.now + timedelta(seconds=301), cooldown_seconds=300)
        fire2, _ = g.should_fire(self.key, triggered=True, now=self.now + timedelta(seconds=302), cooldown_seconds=300)
        self.assertTrue(fire2)

    def test_rehydrate_seeds_cooldown(self):
        g = DedupGate()
        g.rehydrate({self.key: self.now})
        fire, reason = g.should_fire(self.key, triggered=True, now=self.now + timedelta(seconds=60), cooldown_seconds=300)
        self.assertFalse(fire)
        self.assertEqual(reason, "within_cooldown")


class StateStoreTests(unittest.TestCase):
    def test_day_reset(self):
        store = IntradayStateStore()
        st = store.get_or_reset("000001.SZ", "2026-06-22")
        st.seal_peak_bid = 999
        st2 = store.get_or_reset("000001.SZ", "2026-06-23")  # new day → fresh
        self.assertIsNone(st2.seal_peak_bid)
        self.assertEqual(store.reset_day("2026-06-23"), 0)  # nothing stale

    def test_forget_unmonitored(self):
        store = IntradayStateStore()
        store.get_or_reset("000001.SZ", "2026-06-22")
        store.get_or_reset("600000.SH", "2026-06-22")
        dropped = store.forget({"000001.SZ"})
        self.assertEqual(dropped, 1)
        self.assertEqual(store.size(), 1)

    def test_trading_day_shanghai(self):
        # 2026-06-22 18:00 UTC = 2026-06-23 02:00 Shanghai
        day = trading_day_for(datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc))
        self.assertEqual(day, "2026-06-23")


# ---- daemon fire path ----------------------------------------------------


@dataclass
class _FakeRule:
    id: str
    scope_kind: str
    scope_json: dict
    condition_json: dict
    delivery_json: dict | None = None
    cooldown_seconds: int = 0
    enabled: bool = True
    status: str = "active"


class _FakeRuleRepo:
    def __init__(self, rules):
        self._rules = rules

    async def list_active(self):
        return self._rules


class _FakeAlertRepo:
    def __init__(self):
        self.inserted: list[dict] = []
        self._id = 0

    async def list_latest_per_dedup_key(self):
        return []

    async def insert_alert(self, **kw):
        self._id += 1

        @dataclass
        class _A:
            id: int

        self.inserted.append(kw)
        return _A(self._id)

    async def mark_delivered(self, *a, **k):
        pass


class _FakeQuoteStream:
    def __init__(self):
        self.observers = []
        self.monitored = set()

    def add_snapshot_observer(self, o):
        self.observers.append(o)

    def remove_snapshot_observer(self, o):
        if o in self.observers:
            self.observers.remove(o)

    async def set_monitored_symbols(self, s):
        self.monitored = set(s)

    async def push(self, sym, snap):
        for o in list(self.observers):
            await o(sym, snap)


class MonitorDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def _build(self, rule):
        from doyoutrade.monitoring import daemon as daemon_mod

        # Force the session gate open for the test.
        self._orig_gate = daemon_mod.is_ashare_continuous_trading
        daemon_mod.is_ashare_continuous_trading = lambda *a, **k: True
        qs = _FakeQuoteStream()
        alerts = _FakeAlertRepo()
        d = daemon_mod.MonitorDaemon(
            quote_stream_service=qs,
            monitor_rule_repository=_FakeRuleRepo([rule]),
            monitor_alert_repository=alerts,
            watchlist_repository=None,
            debug_session_repository=None,
            debug_session_span_repository=None,
            assistant_service=None,
            sweep_interval_seconds=9999,
        )
        await d.start()
        return d, qs, alerts, daemon_mod

    async def asyncTearDown(self):
        mod = getattr(self, "_daemon_mod", None)
        if mod is not None and hasattr(self, "_orig_gate"):
            mod.is_ashare_continuous_trading = self._orig_gate

    async def test_rising_edge_fire_and_suppress(self):
        rule = _FakeRule(
            id="mon-1",
            scope_kind="symbols",
            scope_json={"symbols": ["000001.SZ"]},
            condition_json={"preset": "limit_up", "params": {"limit_pct": 0.10}},
        )
        d, qs, alerts, mod = await self._build(rule)
        self._daemon_mod = mod
        self.assertEqual(qs.monitored, {"000001.SZ"})
        await qs.push("000001.SZ", QuoteSnapshot(symbol="000001.SZ", price=10.5, prev_close=10.0))
        self.assertEqual(len(alerts.inserted), 0)
        await qs.push("000001.SZ", QuoteSnapshot(symbol="000001.SZ", price=11.0, prev_close=10.0))
        self.assertEqual(len(alerts.inserted), 1)
        self.assertEqual(alerts.inserted[0]["condition_name"], "limit_up")
        self.assertEqual(alerts.inserted[0]["run_id"][:4], "run-")
        # stay at limit → no re-fire
        await qs.push("000001.SZ", QuoteSnapshot(symbol="000001.SZ", price=11.0, prev_close=10.0))
        self.assertEqual(len(alerts.inserted), 1)
        self.assertEqual(d._counters.suppressed_edge, 1)
        await d.stop()

    async def test_seal_missing_does_not_false_fire(self):
        rule = _FakeRule(
            id="mon-2",
            scope_kind="symbols",
            scope_json={"symbols": ["000001.SZ"]},
            condition_json={"preset": "limit_up_seal_shrink"},
        )
        d, qs, alerts, mod = await self._build(rule)
        self._daemon_mod = mod
        # sealed at limit but bid_vol1 missing on every tick → never fires 大减
        for _ in range(3):
            await qs.push("000001.SZ", QuoteSnapshot(symbol="000001.SZ", price=11.0, prev_close=10.0, limit_up_price=11.0, bid_vol1=None))
        self.assertEqual(len(alerts.inserted), 0)
        self.assertGreaterEqual(d._counters.seal_missing, 1)
        await d.stop()


class MonitorRunLinkTests(unittest.IsolatedAsyncioTestCase):
    """run_id must thread monitor_alerts ↔ debug_sessions on a fire (run-link)."""

    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path

        from doyoutrade.persistence.db import Base, create_engine_and_session_factory

        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "monitor.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        from doyoutrade.persistence.db import dispose_engine

        if hasattr(self, "_orig_gate"):
            self._daemon_mod.is_ashare_continuous_trading = self._orig_gate
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_run_id_threads_alerts_and_debug_sessions(self):
        from doyoutrade.monitoring import daemon as daemon_mod
        from doyoutrade.persistence.repositories import (
            SqlAlchemyDebugSessionRepository,
            SqlAlchemyMonitorAlertRepository,
            SqlAlchemyMonitorRuleRepository,
        )

        self._daemon_mod = daemon_mod
        self._orig_gate = daemon_mod.is_ashare_continuous_trading
        daemon_mod.is_ashare_continuous_trading = lambda *a, **k: True

        rules = SqlAlchemyMonitorRuleRepository(self.session_factory)
        alerts = SqlAlchemyMonitorAlertRepository(self.session_factory)
        sessions = SqlAlchemyDebugSessionRepository(self.session_factory)
        rule = await rules.create_rule(
            name="涨停", enabled=True, status="active", scope_kind="symbols",
            scope_json={"symbols": ["000001.SZ"]},
            condition_json={"preset": "limit_up", "params": {"limit_pct": 0.10}},
            delivery_json=None, cooldown_seconds=0,
        )

        qs = _FakeQuoteStream()
        d = daemon_mod.MonitorDaemon(
            quote_stream_service=qs,
            monitor_rule_repository=rules,
            monitor_alert_repository=alerts,
            watchlist_repository=None,
            debug_session_repository=sessions,
            debug_session_span_repository=None,  # skip OTel span export in the unit env
            assistant_service=None,
            sweep_interval_seconds=9999,
        )
        await d.start()
        await qs.push("000001.SZ", QuoteSnapshot(symbol="000001.SZ", price=11.0, prev_close=10.0))
        await d.stop()

        rows = await alerts.list_for_rule(rule.id)
        self.assertEqual(len(rows), 1)
        run_id = rows[0].run_id
        self.assertTrue(run_id and run_id.startswith("run-"))

        # the matching debug session carries session_type='monitor' + the same run_id
        sess_list = await sessions.list_sessions(rule.id)
        self.assertEqual(len(sess_list), 1)
        self.assertEqual(sess_list[0].session_type, "monitor")
        self.assertEqual(sess_list[0].run_id, run_id)
        self.assertEqual(sess_list[0].status, "finished")


if __name__ == "__main__":
    unittest.main()
