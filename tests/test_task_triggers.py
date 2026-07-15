"""Phase 1 unit coverage for the Task-centric Trigger model.

Covers the additive pieces that do not require a full TradingPlatformService:
- SqlAlchemyTaskTriggerRepository CRUD + patch semantics + the strict delivery_json
  shape guard (raises rather than coercing — §错误可见性 + SQLite-vs-Postgres gap),
- runtime.triggers validation (stable error_codes), next-fire math, run-mode mapping,
- TriggerScheduler.scan_once due-ness / overlap-guard / one-shot exhaustion.

The end-to-end run_id <-> trigger_id threading across cycle_runs / debug_sessions /
spans / model_invocations is asserted in the e2e suite (it needs the real runtime).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from doyoutrade.persistence.db import create_engine_and_session_factory, dispose_engine
from doyoutrade.persistence.errors import RecordNotFoundError
from doyoutrade.persistence.models import Base
from doyoutrade.persistence.repositories import (
    SqlAlchemyTaskRepository,
    SqlAlchemyTaskTriggerRepository,
)
from doyoutrade.runtime.trigger_scheduler import TriggerScheduler
from doyoutrade.runtime.triggers import (
    TriggerValidationError,
    compute_next_fire,
    is_due,
    run_mode_for_intent,
    validate_trigger_input,
)


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TaskTriggerRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "runtime.db"
        self.engine, self.session_factory = create_engine_and_session_factory(
            f"sqlite+aiosqlite:///{db_path}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.tasks = SqlAlchemyTaskRepository(self.session_factory)
        self.repo = SqlAlchemyTaskTriggerRepository(self.session_factory)
        await self.tasks.create_task(task_id="task-1", name="t1", mode="paper")

    async def asyncTearDown(self):
        await dispose_engine(self.engine)
        self.tempdir.cleanup()

    async def test_create_get_list_and_id_generation(self):
        snap = await self.repo.create_trigger(
            task_id="task-1",
            name="收盘信号推送",
            schedule_kind="cron",
            cron_expression="50 14 * * mon-fri",
            timezone="Asia/Shanghai",
            execution_intent="signal_only",
            delivery_json={"mode": "card", "target": {"kind": "channel", "channel_id": "feishu-1"}},
        )
        self.assertTrue(snap.id.startswith("trg-"))
        self.assertEqual(snap.status, "active")
        self.assertTrue(snap.enabled)
        got = await self.repo.get_trigger(snap.id)
        self.assertEqual(got.cron_expression, "50 14 * * mon-fri")
        self.assertEqual(got.delivery_json["mode"], "card")
        listed = await self.repo.list_for_task("task-1")
        self.assertEqual([t.id for t in listed], [snap.id])

    async def test_get_missing_raises(self):
        with self.assertRaises(RecordNotFoundError):
            await self.repo.get_trigger("trg-nope")

    async def test_delivery_json_strict_shape_guard(self):
        # Non-dict delivery_json must RAISE, never coerce to {} (the asyncpg-strict path).
        with self.assertRaises(ValueError):
            await self.repo.create_trigger(
                task_id="task-1",
                schedule_kind="interval",
                interval_seconds=60,
                delivery_json="not-a-dict",
            )

    async def test_update_patch_semantics(self):
        snap = await self.repo.create_trigger(
            task_id="task-1", schedule_kind="interval", interval_seconds=300, name="orig"
        )
        # Only the provided field changes; others are untouched.
        updated = await self.repo.update_trigger(snap.id, name="renamed")
        self.assertEqual(updated.name, "renamed")
        self.assertEqual(updated.interval_seconds, 300)
        self.assertEqual(updated.schedule_kind, "interval")

    async def test_list_schedulable_excludes_backtest_and_inactive(self):
        await self.repo.create_trigger(
            task_id="task-1", schedule_kind="interval", interval_seconds=60, name="live-one"
        )
        await self.repo.create_trigger(
            task_id="task-1",
            schedule_kind="backtest_range",
            range_start="2023-01-01",
            range_end="2023-06-30",
            name="bt",
        )
        paused = await self.repo.create_trigger(
            task_id="task-1", schedule_kind="interval", interval_seconds=60, name="paused-one"
        )
        await self.repo.update_trigger(paused.id, status="paused")
        sched = await self.repo.list_schedulable()
        names = sorted(t.name for t in sched)
        self.assertEqual(names, ["live-one"])  # backtest_range + paused excluded

    async def test_record_fire_updates_bookkeeping(self):
        snap = await self.repo.create_trigger(
            task_id="task-1", schedule_kind="interval", interval_seconds=300
        )
        fired_at = _naive_utcnow()
        nxt = fired_at + timedelta(seconds=300)
        await self.repo.record_fire(
            snap.id, last_fired_at=fired_at, next_fire_at=nxt, last_run_id="run-abc"
        )
        got = await self.repo.get_trigger(snap.id)
        self.assertEqual(got.last_run_id, "run-abc")
        self.assertEqual(got.next_fire_at, nxt)
        self.assertEqual(got.last_error, "")

    async def test_delete_trigger(self):
        snap = await self.repo.create_trigger(
            task_id="task-1", schedule_kind="interval", interval_seconds=60
        )
        await self.repo.delete_trigger(snap.id)
        with self.assertRaises(RecordNotFoundError):
            await self.repo.get_trigger(snap.id)


class TriggerValidationTests(unittest.TestCase):
    def test_cron_weekday_rewrite_and_normalize(self):
        out = validate_trigger_input(
            {
                "schedule_kind": "cron",
                "cron_expression": "50 14 * * 1-5",
                "timezone": "Asia/Shanghai",
                "execution_intent": "signal_only",
            }
        )
        self.assertEqual(out["cron_expression"], "50 14 * * mon-fri")

    def test_bad_cron_raises_invalid_cron_expression(self):
        with self.assertRaises(TriggerValidationError) as ctx:
            validate_trigger_input({"schedule_kind": "cron", "cron_expression": "99 99 * * *"})
        self.assertEqual(ctx.exception.error_code, "invalid_cron_expression")

    def test_unknown_schedule_kind(self):
        with self.assertRaises(TriggerValidationError) as ctx:
            validate_trigger_input({"schedule_kind": "weekly"})
        self.assertEqual(ctx.exception.error_code, "schedule_kind_unknown")

    def test_interval_requires_positive_int(self):
        with self.assertRaises(TriggerValidationError) as ctx:
            validate_trigger_input({"schedule_kind": "interval", "interval_seconds": 0})
        self.assertEqual(ctx.exception.error_code, "invalid_schedule_json")

    def test_delivery_card_without_target_unresolved(self):
        with self.assertRaises(TriggerValidationError) as ctx:
            validate_trigger_input(
                {"schedule_kind": "interval", "interval_seconds": 60, "delivery_json": {"mode": "card"}}
            )
        self.assertEqual(ctx.exception.error_code, "delivery_channel_unresolved")

    def test_delivery_session_origin_ok(self):
        out = validate_trigger_input(
            {
                "schedule_kind": "interval",
                "interval_seconds": 60,
                "delivery_json": {"mode": "card", "target": {"kind": "session", "origin": True}},
            }
        )
        self.assertEqual(out["delivery_json"]["target"]["origin"], True)

    def test_delivery_channel_requires_chat_id(self):
        # channel_id (the bot) alone does not address a group — chat_id is required.
        with self.assertRaises(TriggerValidationError) as ctx:
            validate_trigger_input(
                {
                    "schedule_kind": "interval",
                    "interval_seconds": 60,
                    "delivery_json": {
                        "mode": "card",
                        "target": {"kind": "channel", "channel_id": "ch-1"},
                    },
                }
            )
        self.assertEqual(ctx.exception.error_code, "delivery_channel_unresolved")
        self.assertEqual(ctx.exception.field, "delivery.target.chat_id")

    def test_delivery_channel_full_target_ok(self):
        out = validate_trigger_input(
            {
                "schedule_kind": "interval",
                "interval_seconds": 60,
                "delivery_json": {
                    "mode": "card",
                    "target": {
                        "kind": "channel",
                        "channel_id": "ch-1",
                        "chat_id": "oc_abc",
                        "chat_name": "策略群",
                    },
                },
            }
        )
        target = out["delivery_json"]["target"]
        self.assertEqual(target["channel_id"], "ch-1")
        self.assertEqual(target["chat_id"], "oc_abc")
        self.assertEqual(target["chat_name"], "策略群")

    def test_delivery_non_dict_raises(self):
        with self.assertRaises(TriggerValidationError) as ctx:
            validate_trigger_input(
                {"schedule_kind": "interval", "interval_seconds": 60, "delivery_json": [1, 2]}
            )
        self.assertEqual(ctx.exception.error_code, "invalid_delivery_json")

    def test_at_defaults_delete_after_run_true(self):
        out = validate_trigger_input({"schedule_kind": "at", "at_iso": "2026-06-12T09:25:00+08:00"})
        self.assertTrue(out["delete_after_run"])

    def test_run_mode_for_intent(self):
        self.assertEqual(run_mode_for_intent("signal_only", "live"), "signal_only")
        self.assertEqual(run_mode_for_intent("trade", "live"), "live")
        self.assertEqual(run_mode_for_intent("trade", "paper"), "paper")


class NextFireTests(unittest.TestCase):
    def test_interval_next_fire(self):
        now = datetime(2026, 6, 11, 3, 0, 0)
        nxt = compute_next_fire(schedule_kind="interval", interval_seconds=300, now=now)
        self.assertEqual(nxt, now + timedelta(seconds=300))

    def test_cron_next_fire_in_utc(self):
        now = datetime(2026, 6, 11, 3, 0, 0)  # 11:00 CST, before 14:50 CST
        nxt = compute_next_fire(
            schedule_kind="cron",
            cron_expression="50 14 * * mon-fri",
            timezone_str="Asia/Shanghai",
            now=now,
        )
        # 14:50 CST == 06:50 UTC, stored naive-UTC.
        self.assertEqual(nxt, datetime(2026, 6, 11, 6, 50, 0))

    def test_is_due(self):
        now = datetime(2026, 6, 11, 3, 0, 0)
        self.assertTrue(is_due(now - timedelta(seconds=1), now=now))
        self.assertFalse(is_due(now + timedelta(seconds=1), now=now))
        self.assertFalse(is_due(None, now=now))


class _Lock:
    def __init__(self, locked=False):
        self._locked = locked

    def locked(self):
        return self._locked


class _Instance:
    status = "running"


class _Sched:
    def __init__(self):
        self.tasks = {"task-1": _Instance()}


class _Service:
    def __init__(self, *, locked=False, run_id="run-xyz"):
        self.scheduler = _Sched()
        self._locked = locked
        self._run_id = run_id
        self.fired: list[str] = []

    def _cycle_lock(self, task_id):
        return _Lock(self._locked)

    async def run_trigger(self, trg):
        self.fired.append(trg.id)
        return self._run_id


class _Trg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Repo:
    def __init__(self, trgs):
        self.trgs = trgs
        self.records: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.updates: list[tuple[str, dict]] = []

    async def list_schedulable(self):
        return self.trgs

    async def record_fire(self, tid, **kw):
        self.records.append((tid, kw))

    async def update_trigger(self, tid, **kw):
        self.updates.append((tid, kw))
        # Mirror the persisted patch onto the in-memory trigger so a follow-up
        # scan in the same test sees the lazy-initialized next_fire_at.
        for trg in self.trgs:
            if getattr(trg, "id", None) == tid:
                for key, value in kw.items():
                    setattr(trg, key, value)

    async def delete_trigger(self, tid):
        self.deleted.append(tid)


def _make_trigger(**overrides):
    base = dict(
        id="trg-1",
        task_id="task-1",
        schedule_kind="interval",
        interval_seconds=300,
        cron_expression=None,
        timezone="UTC",
        at_iso=None,
        delete_after_run=False,
        execution_intent="signal_only",
        last_fired_at=None,
    )
    base.update(overrides)
    return _Trg(**base)


class TriggerSchedulerScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_trigger_fires_and_records(self):
        trg = _make_trigger(next_fire_at=_naive_utcnow() - timedelta(seconds=1))
        svc = _Service()
        repo = _Repo([trg])
        sched = TriggerScheduler(svc, repo)
        fired = await sched.scan_once()
        self.assertEqual(fired, 1)
        self.assertEqual(svc.fired, ["trg-1"])
        tid, kw = repo.records[0]
        self.assertEqual(tid, "trg-1")
        self.assertEqual(kw["last_run_id"], "run-xyz")
        self.assertIsNotNone(kw["next_fire_at"])  # interval recomputed

    async def test_not_due_trigger_skipped(self):
        trg = _make_trigger(next_fire_at=_naive_utcnow() + timedelta(hours=1))
        svc = _Service()
        sched = TriggerScheduler(svc, _Repo([trg]))
        self.assertEqual(await sched.scan_once(), 0)
        self.assertEqual(svc.fired, [])

    async def test_overlap_guard_skips_without_advancing(self):
        trg = _make_trigger(next_fire_at=_naive_utcnow() - timedelta(seconds=1))
        svc = _Service(locked=True)
        repo = _Repo([trg])
        sched = TriggerScheduler(svc, repo)
        self.assertEqual(await sched.scan_once(), 0)
        self.assertEqual(svc.fired, [])
        self.assertEqual(repo.records, [])  # next_fire NOT advanced -> retries next poll

    async def test_parent_not_running_skipped(self):
        trg = _make_trigger(next_fire_at=_naive_utcnow() - timedelta(seconds=1))
        svc = _Service()
        svc.scheduler.tasks["task-1"].status = "paused"
        sched = TriggerScheduler(svc, _Repo([trg]))
        self.assertEqual(await sched.scan_once(), 0)
        self.assertEqual(svc.fired, [])

    async def test_one_shot_at_exhausts_and_deletes(self):
        trg = _make_trigger(
            id="trg-at",
            schedule_kind="at",
            interval_seconds=None,
            at_iso="2026-06-11T00:00:00+00:00",
            delete_after_run=True,
            next_fire_at=_naive_utcnow() - timedelta(seconds=1),
        )
        svc = _Service()
        repo = _Repo([trg])
        sched = TriggerScheduler(svc, repo)
        self.assertEqual(await sched.scan_once(), 1)
        tid, kw = repo.records[0]
        self.assertEqual(kw["status"], "exhausted")
        self.assertIsNone(kw["next_fire_at"])
        self.assertEqual(repo.deleted, ["trg-at"])

    async def test_null_next_fire_lazy_inits_then_fires_on_next_scan(self):
        """A migrated/imported interval trigger (next_fire_at=NULL) must self-init
        next_fire_at on the first scan (without firing), then fire once due."""
        trg = _make_trigger(
            interval_seconds=5,
            execution_intent="trade",
            next_fire_at=None,
        )
        svc = _Service()
        repo = _Repo([trg])
        sched = TriggerScheduler(svc, repo)

        # First scan: lazy-init only — computes + persists next_fire_at, no fire.
        self.assertEqual(await sched.scan_once(), 0)
        self.assertEqual(svc.fired, [])
        self.assertEqual(len(repo.updates), 1)
        tid, kw = repo.updates[0]
        self.assertEqual(tid, "trg-1")
        self.assertIsNotNone(kw["next_fire_at"])  # now + interval_seconds
        self.assertIsNotNone(trg.next_fire_at)

        # Make it due, then a follow-up scan fires it.
        trg.next_fire_at = _naive_utcnow() - timedelta(seconds=1)
        self.assertEqual(await sched.scan_once(), 1)
        self.assertEqual(svc.fired, ["trg-1"])

    async def test_expire_pending_approvals_runs_each_scan_loop(self):
        """The sole-driver scheduler sweeps stale pending approvals (the duty the
        retired RuntimeTickLoop owned), best-effort and isolated from scan."""

        class _Gate:
            def __init__(self):
                self.calls = 0

            async def expire_pending(self):
                self.calls += 1
                return ["appr-1"]

        gate = _Gate()
        sched = TriggerScheduler(_Service(), _Repo([]), approval_gate=gate)
        await sched._expire_pending_approvals()
        self.assertEqual(gate.calls, 1)

    async def test_expire_pending_approvals_no_gate_is_noop(self):
        sched = TriggerScheduler(_Service(), _Repo([]), approval_gate=None)
        # Must not raise when no gate is wired.
        await sched._expire_pending_approvals()


class _DigestRepo:
    def __init__(self, digest):
        self._digest = digest

    async def get_by_run_id(self, run_id):
        return self._digest


class _MsgRepo:
    def __init__(self):
        self.appended: list[dict] = []

    async def append_message(self, **kw):
        self.appended.append(kw)
        return {"message_id": "m1"}

    async def append_event(self, **kw):
        pass


class _Asst:
    def __init__(self):
        self.repository = _MsgRepo()
        self.channel_manager = None

    async def get_session(self, sid):
        return {"config": {}}  # no channel binding -> forward no-op


class TriggerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_card_delivery_renders_and_tags_source_trigger(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        digest = {
            "run_mode": "signal_only",
            "status": "completed",
            "cycle_failed": False,
            "submitted_count": 0,
            "details": {
                "position_intents": [
                    {"symbol": "600000.SH", "action": "buy", "amount": 10000, "rationale": "breakout"}
                ],
                "fills": [],
            },
        }
        asst = _Asst()
        trg = _Trg(
            id="trg-d",
            name="收盘推送",
            delivery_json={
                "mode": "card",
                "target": {"kind": "session", "session_id": "sess-1"},
                "no_signal_mode": "brief",
            },
        )
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(digest)
        )
        self.assertEqual(status, "delivered")
        self.assertEqual(asst.repository.appended[0]["session_id"], "sess-1")
        self.assertEqual(asst.repository.appended[0]["metadata"]["source"], "trigger")
        self.assertEqual(asst.repository.appended[0]["metadata"]["trigger_id"], "trg-d")
        self.assertIn("600000.SH", asst.repository.appended[0]["content"])

    async def test_no_signal_silent_suppressed(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        digest = {"run_mode": "signal_only", "cycle_failed": False, "submitted_count": 0,
                  "details": {"position_intents": [], "fills": []}}
        asst = _Asst()
        trg = _Trg(
            id="trg-s",
            name="silent",
            delivery_json={
                "mode": "card",
                "target": {"kind": "session", "session_id": "sess-1"},
                "no_signal_mode": "silent",
            },
        )
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(digest)
        )
        self.assertEqual(status, "suppressed")
        self.assertEqual(asst.repository.appended, [])

    async def test_delivery_none_is_noop(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        trg = _Trg(id="trg-n", name="n", delivery_json=None)
        status = await deliver_trigger_result(
            _Asst(), trigger=trg, run_id="run-1", cycle_run_repository=None
        )
        self.assertIsNone(status)

    async def test_channel_delivery_sends_to_resolved_chat_id(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        class _Channel:
            channel_type = "feishu"

            def __init__(self):
                self.sent: list[tuple] = []

            async def send(self, session_id, content, meta):
                self.sent.append((session_id, content, meta))

        class _Manager:
            def __init__(self, channel):
                self._channel = channel

            def get(self, channel_id):
                return self._channel if channel_id == "ch-1" else None

        channel = _Channel()

        class _AsstWithChannels:
            def __init__(self):
                self.channel_manager = _Manager(channel)

        digest = {
            "run_mode": "signal_only",
            "cycle_failed": False,
            "submitted_count": 0,
            "details": {
                "position_intents": [{"symbol": "600000.SH", "action": "buy", "amount": 10000}],
                "fills": [],
            },
        }
        trg = _Trg(
            id="trg-c",
            name="群推送",
            delivery_json={
                "mode": "card",
                "target": {"kind": "channel", "channel_id": "ch-1", "chat_id": "oc_grp"},
                "no_signal_mode": "brief",
            },
        )
        status = await deliver_trigger_result(
            _AsstWithChannels(), trigger=trg, run_id="run-9", cycle_run_repository=_DigestRepo(digest)
        )
        self.assertEqual(status, "forwarded")
        self.assertEqual(len(channel.sent), 1)
        _session_id, _content, meta = channel.sent[0]
        self.assertEqual(meta["feishu_chat_id"], "oc_grp")
        self.assertEqual(meta["feishu_chat_type"], "group")

    async def test_channel_delivery_records_delivered_card_on_cycle_run(self):
        # A Feishu channel push is otherwise sent straight to the group with no
        # persisted trace; the delivery must record the EXACT card on the cycle
        # run so 周期详情 can replay it (else it falsely reads "未推送卡片").
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        class _Channel:
            channel_type = "feishu"

            async def send(self, session_id, content, meta):
                return None

        class _Manager:
            def __init__(self, ch):
                self._ch = ch

            def get(self, cid):
                return self._ch if cid == "ch-1" else None

        class _AsstWithChannels:
            def __init__(self, ch):
                self.channel_manager = _Manager(ch)

        class _PatchRepo:
            def __init__(self, digest):
                self._digest = dict(digest)
                self.patches: list[tuple[str, dict]] = []

            async def get_by_run_id(self, run_id):
                return self._digest

            async def patch_details(self, run_id, patch):
                self.patches.append((run_id, patch))
                self._digest["details"] = {**(self._digest.get("details") or {}), **patch}

        digest = {
            "run_mode": "signal_only",
            "cycle_failed": False,
            "submitted_count": 0,
            "details": {
                "position_intents": [{"symbol": "600000.SH", "action": "buy", "amount": 10000}],
                "fills": [],
            },
        }
        trg = _Trg(
            id="trg-rec",
            name="群推送",
            delivery_json={
                "mode": "card",
                "target": {
                    "kind": "channel",
                    "channel_id": "ch-1",
                    "chat_id": "oc_grp",
                    "chat_name": "信号群",
                },
                "no_signal_mode": "brief",
            },
        )
        repo = _PatchRepo(digest)
        status = await deliver_trigger_result(
            _AsstWithChannels(_Channel()), trigger=trg, run_id="run-rec", cycle_run_repository=repo
        )
        self.assertEqual(status, "forwarded")
        self.assertEqual(len(repo.patches), 1)
        rid, patch = repo.patches[0]
        self.assertEqual(rid, "run-rec")
        cards = patch["delivered_cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["target_kind"], "channel")
        self.assertEqual(cards[0]["status"], "forwarded")
        self.assertEqual(cards[0]["chat_name"], "信号群")
        self.assertTrue(cards[0]["content"])  # the exact rendered card was captured

    async def test_channel_delivery_without_chat_id_is_disabled(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        # A legacy/partial channel target with no chat_id must not attempt a send.
        trg = _Trg(
            id="trg-c2",
            name="no-chat",
            delivery_json={
                "mode": "card",
                "target": {"kind": "channel", "channel_id": "ch-1"},
            },
        )
        status = await deliver_trigger_result(
            _Asst(), trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo({"details": {}})
        )
        self.assertEqual(status, "channel_disabled")

    async def test_error_digest_rendered(self):
        from doyoutrade.runtime.trigger_delivery import render_trigger_digest

        trg = _Trg(id="trg-e", name="err")
        text = render_trigger_digest(trg, {"cycle_failed": True, "failure_message": "boom"})
        self.assertIn("运行失败", text)
        self.assertIn("boom", text)

    def test_held_intent_marked_pending_approval(self):
        from doyoutrade.runtime.trigger_delivery import render_trigger_digest

        trg = _Trg(id="trg-h", name="实盘")
        digest = {
            "run_mode": "live",
            "cycle_failed": False,
            "details": {
                "position_intents": [
                    {
                        "symbol": "601398.SH",
                        "action": "buy",
                        "amount": 780,
                        "rationale": "网格下轨",
                        "pending_approval": True,
                    },
                    {
                        "symbol": "600000.SH",
                        "action": "sell",
                        "amount": 100,
                        "rationale": "止盈",
                        "pending_approval": False,
                    },
                ],
                "fills": [],
            },
        }
        text = render_trigger_digest(trg, digest)
        # Only the held order is marked 待审批 (it is NOT placed); a placed/
        # non-held intent is never marked, so the digest can't read as "done".
        self.assertEqual(text.count("（待审批）"), 1)
        held_line = next(line for line in text.splitlines() if "601398.SH" in line)
        self.assertIn("（待审批）", held_line)
        other_line = next(line for line in text.splitlines() if "600000.SH" in line)
        self.assertNotIn("（待审批）", other_line)

    def test_no_signal_full_appends_market_and_diagnostics(self):
        from doyoutrade.runtime.trigger_delivery import render_trigger_digest

        trg = _Trg(id="trg-f", name="收盘推送")
        digest = {
            "run_mode": "signal_only",
            "cycle_failed": False,
            "details": {
                "position_intents": [],
                "fills": [],
                "market_snapshot": {
                    "601138.SH": {"last_price": 71.02, "pct_change": 2.16},
                },
                "signal_diagnostics": {
                    "601138.SH": {
                        "direction": "hold",
                        "tag": "wrong_pullback_age",
                        "rationale": "回踩天数不符，等待更深回踩",
                    },
                },
            },
        }
        # full → no-signal card carries 行情 (price + 涨跌幅) + 判断 (factors).
        full = render_trigger_digest(trg, digest, no_signal_mode="full")
        self.assertIn("本轮无可执行信号", full)
        self.assertIn("行情：", full)
        self.assertIn("71.02", full)
        self.assertIn("+2.16%", full)
        self.assertIn("判断：", full)
        self.assertIn("wrong_pullback_age", full)
        self.assertIn("回踩天数不符", full)

        # brief → just the one-line notice; no 行情/判断 sections (the reported bug:
        # full used to render identically to brief).
        brief = render_trigger_digest(trg, digest, no_signal_mode="brief")
        self.assertIn("本轮无可执行信号", brief)
        self.assertNotIn("行情：", brief)
        self.assertNotIn("判断：", brief)

    def test_task_name_and_symbol_names_rendered_when_resolved(self):
        from doyoutrade.runtime.trigger_delivery import render_trigger_digest

        trg = _Trg(id="trg-n", name="收盘推送", task_id="task-1")
        digest = {
            "run_mode": "live",
            "cycle_failed": False,
            "details": {
                "position_intents": [
                    {"symbol": "601398.SH", "action": "buy", "amount": 780, "rationale": "网格下轨"},
                ],
                "fills": [],
                "market_snapshot": {
                    "601398.SH": {"last_price": 7.78, "pct_change": 1.567},
                },
                "signal_diagnostics": {
                    "601398.SH": {"direction": "buy", "tag": "grid_l0", "rationale": "突破"},
                },
            },
        }
        text = render_trigger_digest(
            trg, digest, no_signal_mode="full",
            task_name="银行网格", symbol_names={"601398.SH": "工商银行"},
        )
        # 任务名 surfaces as a subtitle under the title line.
        self.assertIn("任务：银行网格", text)
        # 股票名称 renders beside the code in 意图 / 行情 / 判断 (not bare code only).
        self.assertIn("工商银行（601398.SH）", text)
        # Each of the three sections carries the labelled symbol.
        self.assertEqual(text.count("工商银行（601398.SH）"), 3)

    def test_symbol_names_absent_falls_back_to_bare_code(self):
        from doyoutrade.runtime.trigger_delivery import render_trigger_digest

        trg = _Trg(id="trg-b", name="收盘推送")
        digest = {
            "run_mode": "signal_only",
            "cycle_failed": False,
            "details": {
                "position_intents": [],
                "fills": [],
                "market_snapshot": {"601398.SH": {"last_price": 7.78, "pct_change": 1.0}},
            },
        }
        # No symbol_names / partial map → bare code, never raises, never drops 行情.
        text = render_trigger_digest(
            trg, digest, no_signal_mode="full", symbol_names={}
        )
        self.assertIn("601398.SH", text)
        self.assertNotIn("工商银行", text)

    def test_processing_time_rendered_in_beijing_tz(self):
        from doyoutrade.runtime.trigger_delivery import render_trigger_digest

        trg = _Trg(id="trg-t", name="收盘推送")
        digest = {
            "run_mode": "signal_only",
            "cycle_failed": False,
            # naive UTC, as cycle_run_to_dict serialises wall_started_at.
            "wall_started_at": "2026-06-12T07:00:03",
            "details": {"position_intents": [], "fills": []},
        }
        text = render_trigger_digest(trg, digest, no_signal_mode="brief")
        # 07:00:03 UTC → 15:00:03 北京时间 (UTC+8).
        self.assertIn("处理时间：2026-06-12 15:00:03", text)

    def test_processing_time_handles_aware_iso_and_failure_branch(self):
        from doyoutrade.runtime.trigger_delivery import render_trigger_digest

        trg = _Trg(id="trg-t2", name="收盘推送")
        # tz-aware ISO must also normalise to 北京时间, and the failure branch
        # carries the processing time too.
        failed = render_trigger_digest(
            trg,
            {"cycle_failed": True, "failure_message": "boom", "wall_started_at": "2026-06-12T07:00:03+00:00"},
        )
        self.assertIn("运行失败", failed)
        self.assertIn("（2026-06-12 15:00:03）", failed)

    def test_processing_time_absent_or_unparseable_is_dropped(self):
        from doyoutrade.runtime.trigger_delivery import render_trigger_digest

        trg = _Trg(id="trg-t3", name="收盘推送")
        # Missing field → no 处理时间 line, push still renders.
        no_time = render_trigger_digest(
            trg, {"run_mode": "signal_only", "details": {"position_intents": [], "fills": []}}
        )
        self.assertNotIn("处理时间：", no_time)
        self.assertIn("本轮无可执行信号", no_time)
        # Unparseable value → dropped (logged warning), never raises.
        bad = render_trigger_digest(
            trg,
            {"run_mode": "signal_only", "wall_started_at": "not-a-timestamp",
             "details": {"position_intents": [], "fills": []}},
        )
        self.assertNotIn("处理时间：", bad)


class _AgentRepoStub:
    def __init__(self, agent_ids):
        self._ids = list(agent_ids)

    async def list_agents(self, *, include_inactive: bool = False):
        # Mirror the real repo: ``_agent_dict`` keys the id as ``id`` (NOT
        # ``agent_id``) and orders is_default-first, so the first row is the
        # default agent. A prior fake used ``agent_id`` and masked a real
        # key-name bug — keep this matching production.
        return [{"id": aid, "status": "active", "is_default": idx == 0}
                for idx, aid in enumerate(self._ids)]

    async def get_agent(self, agent_id):
        # _resolve_default_agent_id now prefers the fixed main agent by id; this
        # stub has only custom ids, so it returns None for MAIN_AGENT_ID and the
        # resolver cleanly falls back to the first active row from list_agents.
        if agent_id in self._ids:
            return {"id": agent_id, "status": "active", "is_default": self._ids.index(agent_id) == 0}
        return None


class _ProseAsst:
    """Fake assistant_service exercising the prose compose-via-agent turn."""

    def __init__(self, *, reply="本轮持有不动：指数退潮，按策略观望。", agents=("ag-1",), raise_on_send=False):
        self.repository = _MsgRepo()
        self.channel_manager = None
        self._reply = reply
        self._raise = raise_on_send
        self.agent_repo = _AgentRepoStub(agents) if agents is not None else None
        self.created_sessions: list[dict] = []
        self.sent: list[dict] = []

    async def get_session(self, sid):
        return {"config": {}}  # no channel binding -> forward no-op

    async def create_session(self, *, agent_id, title=""):
        self.created_sessions.append({"agent_id": agent_id, "title": title})
        return {"session_id": f"sess-compose-{len(self.created_sessions)}"}

    async def send_message(self, *, session_id, content, streaming_controller=None, source_attribution=None):
        self.sent.append({"session_id": session_id, "content": content, "source_attribution": source_attribution})
        if self._raise:
            raise RuntimeError("model route down")
        return {
            "messages": [
                {"role": "user", "content": content},
                {"role": "assistant", "content": self._reply},
            ]
        }


def _prose_digest():
    return {
        "run_mode": "signal_only",
        "cycle_failed": False,
        "submitted_count": 0,
        "details": {
            "position_intents": [],
            "fills": [],
            "market_snapshot": {"601138.SH": {"last_price": 71.02, "pct_change": 2.16}},
            "signal_diagnostics": {
                "601138.SH": {
                    "direction": "hold",
                    "tag": "wrong_pullback_age",
                    "rationale": "回踩天数不符，等待更深回踩",
                }
            },
        },
    }


class TriggerProseDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _prose_trg(self, **delivery):
        base = {
            "mode": "prose",
            "target": {"kind": "session", "session_id": "sess-1"},
            "no_signal_mode": "full",
        }
        base.update(delivery)
        return _Trg(id="trg-p", name="中继反抽推送", delivery_json=base)

    async def test_prose_uses_agent_composed_text(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        # The composer returns its section contents as JSON; the backend renders
        # the fixed title + section headers from it.
        import json as _json
        reply = _json.dumps({
            "market": "601138.SH 71.02（+2.16%）",
            "judgement": "回踩天数不符，等待更深回踩",
            "account": "账户数据缺失",
            "action": "本轮无可执行信号，策略维持观望。",
        })
        asst = _ProseAsst(reply=reply)
        trg = self._prose_trg(composer_agent_id="ag-explicit")
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(_prose_digest())
        )
        self.assertEqual(status, "delivered")
        content = asst.repository.appended[0]["content"]
        # Title + the four section headers are LITERALLY rendered by the backend
        # (deterministic shape) — the model cannot rename them.
        self.assertTrue(content.startswith("【中继反抽推送 · 策略信号】"), content)
        self.assertIn("行情：\n601138.SH 71.02（+2.16%）", content)
        self.assertIn("判断：\n回踩天数不符", content)
        self.assertIn("账户：\n账户数据缺失", content)
        self.assertIn("本轮动作：\n本轮无可执行信号", content)
        # The explicit composer_agent_id was used, and the digest was fed into framing.
        self.assertEqual(asst.created_sessions[0]["agent_id"], "ag-explicit")
        self.assertIn("[Trigger]", asst.created_sessions[0]["title"])
        self.assertIn("wrong_pullback_age", asst.sent[0]["content"])
        self.assertIn("cycle_digest", asst.sent[0]["content"])

    async def test_prose_framing_carries_beijing_processing_time(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result
        import json as _json

        asst = _ProseAsst(
            reply=_json.dumps({"market": "x", "judgement": "y", "account": "z", "action": "无信号"}),
            agents=("ag-default",),
        )
        trg = self._prose_trg()
        digest = _prose_digest()
        digest["wall_started_at"] = "2026-06-12T07:00:03"  # naive UTC
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(digest)
        )
        self.assertEqual(status, "delivered")
        # The composer framing must hand the agent the 北京时间 processing moment,
        # and the rendered skeleton must carry it too.
        self.assertIn("2026-06-12 15:00:03", asst.sent[0]["content"])
        self.assertIn("处理时刻", asst.sent[0]["content"])
        self.assertIn("处理时间：2026-06-12 15:00:03", asst.repository.appended[0]["content"])

    async def test_prose_resolves_first_active_agent_when_unset(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result
        import json as _json

        asst = _ProseAsst(
            reply=_json.dumps({"market": "m", "judgement": "j", "account": "a", "action": "无信号"}),
            agents=("ag-default", "ag-2"),
        )
        trg = self._prose_trg()  # no composer_agent_id
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(_prose_digest())
        )
        self.assertEqual(status, "delivered")
        self.assertEqual(asst.created_sessions[0]["agent_id"], "ag-default")
        # Delivered content is the rendered fixed-shape skeleton (title is deterministic).
        self.assertTrue(asst.repository.appended[0]["content"].startswith("【中继反抽推送 · 策略信号】"))

    async def test_prose_prefers_dedicated_signal_composer_agent(self):
        """When composer_agent_id is unset, the resolver prefers the dedicated
        signal-card composer agent (compose-only: no tools/skills) over the
        main agent — that is the whole point of the dedicated composer."""
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result
        from doyoutrade.assistant.signal_composer_agent import SIGNAL_COMPOSER_AGENT_ID
        import json as _json

        # The stub has BOTH the composer agent and the main agent active.
        asst = _ProseAsst(
            reply=_json.dumps({"market": "m", "judgement": "j", "account": "a", "action": "无信号"}),
            agents=(SIGNAL_COMPOSER_AGENT_ID, "agent_default", "ag-2"),
        )
        trg = self._prose_trg()  # no explicit composer_agent_id
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(_prose_digest())
        )
        self.assertEqual(status, "delivered")
        # The composer agent was chosen, not the main agent.
        self.assertEqual(asst.created_sessions[0]["agent_id"], SIGNAL_COMPOSER_AGENT_ID)
        # The fixed title is rendered by the backend into the delivered card.
        self.assertTrue(asst.repository.appended[0]["content"].startswith("【中继反抽推送 · 策略信号】"))

    async def test_prose_explicit_composer_agent_id_wins(self):
        """An explicit composer_agent_id still overrides the default resolver,
        so an operator can pin a specific agent per-trigger if they want to."""
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result
        from doyoutrade.assistant.signal_composer_agent import SIGNAL_COMPOSER_AGENT_ID
        import json as _json

        asst = _ProseAsst(
            reply=_json.dumps({"market": "m", "judgement": "j", "account": "a", "action": "无信号"}),
            agents=(SIGNAL_COMPOSER_AGENT_ID, "ag-explicit"),
        )
        trg = self._prose_trg(composer_agent_id="ag-explicit")
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(_prose_digest())
        )
        self.assertEqual(status, "delivered")
        self.assertEqual(asst.created_sessions[0]["agent_id"], "ag-explicit")

    async def test_prose_unparseable_reply_falls_back_to_deterministic_card(self):
        """If the composer returns free-form text instead of the section JSON,
        the delivery falls back VISIBLE to the deterministic card (never ships
        a free-form push with an inconsistent title)."""
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        asst = _ProseAsst(reply="【飞书 · 策略信号】\n这是模型自己编的正文，不是 JSON。")
        trg = self._prose_trg(composer_agent_id="ag-1")
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(_prose_digest())
        )
        self.assertEqual(status, "delivered")
        content = asst.repository.appended[0]["content"]
        # Fell back to the deterministic card (it carries the diagnostic tag).
        self.assertIn("行情：", content)
        self.assertIn("wrong_pullback_age", content)
        # The free-form reply text was NOT shipped.
        self.assertNotIn("这是模型自己编的正文", content)

    async def test_prose_freetext_with_correct_title_is_shipped_as_is(self):
        """Tier 2: a chat-oriented model that ignores the JSON ask but DOES
        reproduce the fixed title verbatim still gets its narrated card shipped
        (Agent interpretation preserved on models that can't do structured
        output). The title is deterministic; section coverage is guided by the
        framing. Only the exact-name title qualifies — a wrong-name title falls
        through to the deterministic card."""
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        # The trigger name is "中继反抽推送" (from _prose_trg); the reply uses the
        # SAME name in the fixed title, so Tier 2 matches.
        asst = _ProseAsst(
            reply=(
                "【中继反抽推送 · 策略信号】\n"
                "行情：\n601138.SH 71.02（+2.16%）\n\n"
                "判断：\n回踩天数不符\n\n"
                "账户：\n现金充足\n\n"
                "本轮动作：\n本轮无可执行信号。"
            ),
        )
        trg = self._prose_trg(composer_agent_id="ag-1")
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(_prose_digest())
        )
        self.assertEqual(status, "delivered")
        content = asst.repository.appended[0]["content"]
        # The model's narrated card is shipped verbatim (Agent interpretation kept),
        # with the deterministic fixed title.
        self.assertTrue(content.startswith("【中继反抽推送 · 策略信号】"))
        self.assertIn("回踩天数不符", content)
        # It is NOT the deterministic card (no diagnostic-tag-formatted fallback).
        self.assertNotIn("wrong_pullback_age", content)

    async def test_prose_attributes_model_invocation_to_cycle_run_id(self):
        """The composer's model invocation must be attributed to the CYCLE's
        run_id + task_id (run_id 贯穿), not an opaque asst-run id, so it shows
        up under 周期详情 model invocations."""
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result
        import json as _json

        asst = _ProseAsst(
            reply=_json.dumps({"market": "m", "judgement": "j", "account": "a", "action": "无信号"}),
        )
        trg = self._prose_trg(composer_agent_id="ag-1")
        trg.task_id = "task-xyz-123"  # the cycle's owning task
        await deliver_trigger_result(
            asst, trigger=trg, run_id="run-cycle-abc", cycle_run_repository=_DigestRepo(_prose_digest())
        )
        # The compose turn carried the cycle's run_id + task_id as attribution.
        attribution = asst.sent[0]["source_attribution"]
        self.assertEqual(attribution["run_id"], "run-cycle-abc")
        self.assertEqual(attribution["task_id"], "task-xyz-123")

    async def test_prose_compose_failure_falls_back_to_deterministic_card(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        asst = _ProseAsst(raise_on_send=True)
        trg = self._prose_trg(composer_agent_id="ag-1")
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(_prose_digest())
        )
        # Push is NOT dropped: it falls back to the deterministic full card.
        self.assertEqual(status, "delivered")
        content = asst.repository.appended[0]["content"]
        self.assertIn("行情：", content)
        self.assertIn("wrong_pullback_age", content)

    async def test_prose_empty_reply_falls_back_to_deterministic_card(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        asst = _ProseAsst(reply="[SILENT]")
        trg = self._prose_trg(composer_agent_id="ag-1")
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(_prose_digest())
        )
        self.assertEqual(status, "delivered")
        self.assertIn("行情：", asst.repository.appended[0]["content"])

    async def test_prose_no_resolvable_agent_falls_back_to_deterministic_card(self):
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        # No composer_agent_id and no active agents -> visible fallback, no send.
        asst = _ProseAsst(agents=())
        trg = self._prose_trg()
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1", cycle_run_repository=_DigestRepo(_prose_digest())
        )
        self.assertEqual(status, "delivered")
        self.assertEqual(asst.sent, [])
        self.assertIn("行情：", asst.repository.appended[0]["content"])

    def test_framing_demands_strategy_reasoning_not_paraphrase(self):
        """The composer framing must instruct the agent to EXPLAIN why this
        signal / why this operation by reading signal_diagnostics.diagnostics
        (the indicator values / thresholds), translating tags, and contrasting
        with the current position — not just paraphrase direction/tag/rationale.
        Guards against the "AI 解读太简陋" regression where judgement/action
        merely restated field values.
        """
        from doyoutrade.assistant.prompt_templates import render_trigger_framing

        digest = {
            "run_mode": "live",
            "agent_name": "银行网格",
            "details": {
                "position_intents": [
                    {"symbol": "601398.SH", "action": "buy", "amount": 780,
                     "rationale": "网格第一档下轨"},
                ],
                "fills": [],
                "market_snapshot": {"601398.SH": {"last_price": 7.78, "pct_change": -1.2}},
                "signal_diagnostics": {
                    "601398.SH": {
                        "direction": "buy", "tag": "grid_buy_1",
                        "rationale": "触及网格第一档下轨",
                        "diagnostics": {"grid_level": 1, "lower_band": 7.80, "gap_pct": -0.26},
                    },
                },
                "post_cycle_account": {
                    "account": {"cash": "9994966.45", "equity": "9999652"},
                    "positions": [],
                },
            },
        }
        framing = render_trigger_framing(
            trigger_name="银行网格推送", trigger_id="trg-1", fired_at="2026-06-15 15:00:00",
            processed_at="2026-06-15 15:00:03", run_mode="live",
            digest=digest, no_signal_mode="full",
        )
        # The framing points the composer at the diagnostics sub-field (the
        # indicator values / thresholds that justify the signal).
        self.assertIn("diagnostics", framing)
        # It demands a "why" interpretation, not a field-value restatement.
        self.assertIn("为什么", framing)
        # It tells the composer to translate English tags into rule meaning
        # (so `grid_buy_1` is explained, not pasted raw).
        self.assertIn("tag", framing)
        # The composer must contrast against the current position (新开/加仓/减仓/平仓).
        self.assertIn("持仓", framing)
        # The digest's actual diagnostics values are handed to the composer so
        # it can cite concrete numbers (7.80 serialises as 7.8 in the JSON dump).
        self.assertIn("grid_level", framing)
        self.assertIn("lower_band", framing)
        self.assertIn("7.8", framing)


class ComposerSkeletonTests(unittest.TestCase):
    """Direct unit tests for the fixed-shape renderer + lenient JSON extractor.

    These pin the contract that makes the prose push deterministic: the title
    line and the four section headers are reproduced VERBATIM by the backend,
    and the extractor tolerates the ways models commonly mangle a bare-JSON ask
    (fences, surrounding prose) while still rejecting unparseable free text."""

    def test_extract_parses_bare_json(self):
        from doyoutrade.runtime.trigger_delivery import _extract_composer_json

        sections = _extract_composer_json(
            '{"market": "601398.SH 7.78（+1.57%）", "judgement": "持有", '
            '"account": "现金 100", "action": "无信号"}'
        )
        self.assertEqual(sections["market"], "601398.SH 7.78（+1.57%）")
        self.assertEqual(sections["action"], "无信号")

    def test_extract_strips_code_fence_and_surrounding_prose(self):
        from doyoutrade.runtime.trigger_delivery import _extract_composer_json

        fenced = "```json\n{\"market\": \"m\", \"judgement\": \"j\"}\n```"
        self.assertEqual(
            _extract_composer_json(fenced), {"market": "m", "judgement": "j"}
        )
        with_prose = '好的，如下：\n{"market": "m", "judgement": "j", "account": "a", "action": "x"}\n以上。'
        self.assertEqual(_extract_composer_json(with_prose)["action"], "x")

    def test_extract_returns_none_for_unparseable_free_text(self):
        from doyoutrade.runtime.trigger_delivery import _extract_composer_json

        # Free-form card text (no JSON object at all) → None → visible fallback.
        self.assertIsNone(_extract_composer_json("【飞书 · 策略信号】\n模型自己写的正文"))
        self.assertIsNone(_extract_composer_json(""))
        self.assertIsNone(_extract_composer_json("not json at all"))

    def test_render_reproduces_fixed_title_and_section_headers(self):
        from doyoutrade.runtime.trigger_delivery import _render_composer_skeleton

        out = _render_composer_skeleton(
            trigger_name="飞书",
            processed_at="2026-06-14 22:40:00",
            sections={"market": "m1", "judgement": "j1", "account": "a1", "action": "act1"},
        )
        lines = out.splitlines()
        self.assertEqual(lines[0], "【飞书 · 策略信号】")
        self.assertEqual(lines[1], "处理时间：2026-06-14 22:40:00（北京时间）")
        # The four section headers appear verbatim, in order.
        self.assertIn("行情：", lines)
        self.assertIn("判断：", lines)
        self.assertIn("账户：", lines)
        self.assertIn("本轮动作：", lines)
        self.assertLess(lines.index("行情："), lines.index("判断："))
        self.assertLess(lines.index("判断："), lines.index("账户："))
        self.assertLess(lines.index("账户："), lines.index("本轮动作："))

    def test_render_fills_missing_section_with_data_missing_sentinel(self):
        from doyoutrade.runtime.trigger_delivery import _render_composer_skeleton

        out = _render_composer_skeleton(
            trigger_name="T", processed_at="", sections={"action": "无信号"}
        )
        # A section the composer omitted is NOT silently dropped — the operator
        # sees "数据缺失" so a skipped section stays visible.
        self.assertIn("行情：\n行情数据缺失", out)
        self.assertIn("判断：\n判断数据缺失", out)
        self.assertIn("账户：\n账户数据缺失", out)
        self.assertIn("本轮动作：\n无信号", out)
        # No processed_at → no 处理时间 line.
        self.assertNotIn("处理时间：", out)


class _ApprovalRow:
    def __init__(self, run_id):
        self.approval_id = "ap-1"
        self.intent_id = "it-1"
        self.task_id = "t-1"
        self.run_id = run_id
        self.symbol = "601398.SH"
        self.action = "buy"
        self.notional = "780"
        self.mode = "live"
        self.created_at = None
        self.expires_at = None
        self.intent_payload = (
            '{"symbol": "601398.SH", "action": "buy", "rationale": "网格下轨", '
            '"signal_tag": "grid_buy_1", "strategy_tag": "grid", '
            '"price_reference": 7.8, "order_type": "limit", "tif": "day"}'
        )


class _ApprovalGateRows:
    def __init__(self, rows):
        self._rows = rows

    async def list_pending(self):
        return self._rows


class _ApprovalChannel:
    channel_type = "feishu"

    def __init__(self):
        self.sent: list[dict] = []
        self.results: list[dict] = []

    async def send_trade_approval_card(self, chat_id, payload, narration=None):
        self.sent.append({"chat_id": chat_id, "payload": payload, "narration": narration})
        return "om_test"

    async def send_trade_approval_result_card(self, chat_id, payload, *, outcome):
        self.results.append({"chat_id": chat_id, "payload": payload, "outcome": outcome})
        return "om_result"


class _TaskTriggerRepoStub:
    """Minimal async ``list_for_task`` so the result-card delivery can re-resolve
    the task's Feishu channel target (the resume sweep has no trigger in hand)."""

    def __init__(self, triggers):
        self._triggers = triggers

    async def list_for_task(self, task_id):
        return list(self._triggers)


class _ChannelMgr:
    def __init__(self, channel):
        self._c = channel

    def get(self, channel_id):
        return self._c


class _CatalogRepo:
    async def get(self, symbol):
        return {"display_name": "工商银行"} if symbol == "601398.SH" else None


class ApprovalCardDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _trg(self, **delivery):
        base = {"target": {"kind": "channel", "channel_id": "ch-1", "chat_id": "oc_1"}}
        base.update(delivery)
        return _Trg(id="trg-a", name="审批推送", delivery_json=base)

    async def test_prose_mode_composes_narration_keeps_card(self):
        from doyoutrade.runtime.trigger_delivery import deliver_pending_approval_cards

        channel = _ApprovalChannel()
        asst = _ProseAsst(reply="【工商银行 601398.SH】建议买入 ¥780，网格下轨触发。请在卡片上批准或拒绝。")
        asst.channel_manager = _ChannelMgr(channel)
        sent = await deliver_pending_approval_cards(
            asst,
            trigger=self._trg(mode="prose", composer_agent_id="ag-x"),
            run_id="run-1",
            approval_gate=_ApprovalGateRows([_ApprovalRow("run-1")]),
            cycle_run_repository=_DigestRepo({"details": {}}),
            instrument_catalog_repository=_CatalogRepo(),
        )
        self.assertEqual(sent, 1)
        # Agent-composed narration was delivered; stock name resolved; explicit
        # composer agent used; framing is an [Approval] session.
        self.assertIn("网格下轨触发", channel.sent[0]["narration"])
        self.assertEqual(channel.sent[0]["payload"]["symbol_name"], "工商银行")
        self.assertEqual(asst.created_sessions[0]["agent_id"], "ag-x")
        self.assertIn("[Approval]", asst.created_sessions[0]["title"])

    async def test_card_mode_no_narration_but_named(self):
        from doyoutrade.runtime.trigger_delivery import deliver_pending_approval_cards

        channel = _ApprovalChannel()
        asst = _ProseAsst()
        asst.channel_manager = _ChannelMgr(channel)
        sent = await deliver_pending_approval_cards(
            asst,
            trigger=self._trg(mode="card"),
            run_id="run-1",
            approval_gate=_ApprovalGateRows([_ApprovalRow("run-1")]),
            instrument_catalog_repository=_CatalogRepo(),
        )
        self.assertEqual(sent, 1)
        # No agent composition (deterministic card), but the stock is still named.
        self.assertIsNone(channel.sent[0]["narration"])
        self.assertEqual(channel.sent[0]["payload"]["symbol_name"], "工商银行")
        self.assertEqual(asst.created_sessions, [])

    async def test_prose_compose_failure_falls_back_to_deterministic(self):
        from doyoutrade.runtime.trigger_delivery import deliver_pending_approval_cards

        channel = _ApprovalChannel()
        asst = _ProseAsst(raise_on_send=True)  # LLM down → compose returns None
        asst.channel_manager = _ChannelMgr(channel)
        sent = await deliver_pending_approval_cards(
            asst,
            trigger=self._trg(mode="prose"),
            run_id="run-1",
            approval_gate=_ApprovalGateRows([_ApprovalRow("run-1")]),
            instrument_catalog_repository=_CatalogRepo(),
        )
        # Still delivered, as the deterministic card (narration None) — 功能不阉割.
        self.assertEqual(sent, 1)
        self.assertIsNone(channel.sent[0]["narration"])


class ApprovalResultCardDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """R2: deliver_approval_result_card — the post-dispatch order-result receipt."""

    def _trg(self):
        return _Trg(
            id="trg-a",
            name="审批推送",
            delivery_json={"target": {"kind": "channel", "channel_id": "ch-1", "chat_id": "oc_1"}},
        )

    def _asst(self, channel):
        asst = _ProseAsst()
        asst.channel_manager = _ChannelMgr(channel)
        return asst

    async def test_filled_result_card_carries_actual_fill_facts(self):
        from doyoutrade.runtime.trigger_delivery import deliver_approval_result_card

        channel = _ApprovalChannel()
        fill = {"symbol": "601398.SH", "side": "buy", "quantity": 100, "price": 7.79,
                "timestamp": "2026-06-14T05:05:00"}
        status = await deliver_approval_result_card(
            self._asst(channel),
            approval=_ApprovalRow("run-1"),
            outcome="filled",
            fill=fill,
            trigger_repository=_TaskTriggerRepoStub([self._trg()]),
            instrument_catalog_repository=_CatalogRepo(),
        )
        self.assertEqual(status, "delivered")
        self.assertEqual(len(channel.results), 1)
        rec = channel.results[0]
        self.assertEqual(rec["chat_id"], "oc_1")
        self.assertEqual(rec["outcome"], "filled")
        p = rec["payload"]
        self.assertEqual(p["symbol_name"], "工商银行")
        self.assertEqual(p["fill_quantity"], "100")
        self.assertEqual(p["fill_price"], "7.79")
        self.assertEqual(p["fill_amount"], "779")           # 100 * 7.79, decimal (金额十进制)
        self.assertEqual(p["fill_time"], "2026-06-14 13:05:00")  # UTC → 北京时间
        self.assertEqual(p["strategy_tag"], "grid")          # from intent payload, not mode

    async def test_abandoned_result_card_carries_error(self):
        from doyoutrade.runtime.trigger_delivery import deliver_approval_result_card

        channel = _ApprovalChannel()
        status = await deliver_approval_result_card(
            self._asst(channel),
            approval=_ApprovalRow("run-1"),
            outcome="abandoned",
            error="broker rejected: insufficient funds",
            trigger_repository=_TaskTriggerRepoStub([self._trg()]),
            instrument_catalog_repository=_CatalogRepo(),
        )
        self.assertEqual(status, "delivered")
        rec = channel.results[0]
        self.assertEqual(rec["outcome"], "abandoned")
        self.assertEqual(rec["payload"]["error"], "broker rejected: insufficient funds")
        self.assertEqual(rec["payload"]["notional"], "780")  # planned amount preserved

    async def test_no_channel_target_is_web_only_not_a_failure(self):
        from doyoutrade.runtime.trigger_delivery import deliver_approval_result_card

        channel = _ApprovalChannel()
        # A task whose only trigger pushes to a session (no channel) → web-only.
        session_trg = _Trg(
            id="trg-s", name="s",
            delivery_json={"target": {"kind": "session", "origin": True}},
        )
        status = await deliver_approval_result_card(
            self._asst(channel),
            approval=_ApprovalRow("run-1"),
            outcome="filled",
            fill={"quantity": 100, "price": 7.79},
            trigger_repository=_TaskTriggerRepoStub([session_trg]),
            instrument_catalog_repository=_CatalogRepo(),
        )
        self.assertEqual(status, "no_channel_target")
        self.assertEqual(channel.results, [])

    async def test_channel_send_failure_is_surfaced(self):
        from doyoutrade.runtime.trigger_delivery import deliver_approval_result_card

        class _BoomChannel(_ApprovalChannel):
            async def send_trade_approval_result_card(self, chat_id, payload, *, outcome):
                raise RuntimeError("feishu down")

        channel = _BoomChannel()
        status = await deliver_approval_result_card(
            self._asst(channel),
            approval=_ApprovalRow("run-1"),
            outcome="filled",
            fill={"quantity": 100, "price": 7.79},
            trigger_repository=_TaskTriggerRepoStub([self._trg()]),
            instrument_catalog_repository=_CatalogRepo(),
        )
        self.assertEqual(status, "send_failed")  # surfaced, not silently swallowed


class _FeishuDigestChannel:
    """A minimal Feishu channel whose generic ``send()`` captures the CardContent.

    Models the trigger-digest delivery path (which goes through the channel's
    ``send()`` with a ``CardContent``, unlike the approval cards that use a
    dedicated ``send_trade_approval_card`` method).
    """

    channel_type = "feishu"

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, session_id, content, meta):
        self.sent.append({"session_id": session_id, "content": content, "meta": meta})


class SignalDigestCardBuilderTests(unittest.TestCase):
    """Unit tests for the rich :func:`build_signal_digest_card` renderer.

    Pins the beautification contract: themed header, colored 涨跌幅 (A-share
    涨红跌绿), the four fact sections in full mode, the compact card in brief
    mode, the failure branch, and the advisory AI 解读 panel for prose mode.
    """

    def _market_digest(self):
        return {
            "run_mode": "signal_only",
            "cycle_failed": False,
            "wall_started_at": "2026-06-14T15:36:02",
            "details": {
                "position_intents": [],
                "fills": [],
                "market_snapshot": {
                    "601398.SH": {
                        "last_price": 7.78, "pct_change": 1.567,
                        "open": 7.59, "high": 7.78, "low": 7.52, "prev_close": 7.66,
                    },
                },
                "signal_diagnostics": {
                    "601398.SH": {
                        "direction": "target_exposure", "tag": "grid_l0",
                        "rationale": "维持目标仓位为零的观望状态。", "target_exposure": 0.0,
                    },
                },
                "post_cycle_account": {
                    "account": {"cash": "9994966.45", "equity": "9999652"},
                    "total_market_value": "758",
                    "positions": [
                        {"symbol": "000592.SZ", "name": "平潭发展", "quantity": 100,
                         "available": 100, "cost_price": "11.06", "last_price": "7.58",
                         "market_value": "758"},
                    ],
                },
            },
        }

    @staticmethod
    def _md_texts(card: dict) -> list[str]:
        """Flatten every lark_md/markdown ``content`` in the card body."""
        out: list[str] = []
        for el in card.get("body", {}).get("elements", []):
            txt = el.get("text", {}).get("content") if isinstance(el.get("text"), dict) else None
            if txt:
                out.append(txt)
            for f in el.get("fields", []) or []:
                ft = f.get("text", {}).get("content") if isinstance(f.get("text"), dict) else None
                if ft:
                    out.append(ft)
        return out

    def test_full_no_signal_renders_all_sections_and_a_share_colors(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_signal_digest_card

        card = build_signal_digest_card(
            trigger_name="飞书",
            digest=self._market_digest(),
            processed_at="2026-06-14 23:36:02",
            no_signal_mode="full",
            run_id="run-1",
        )
        # Blue header for a no-action cycle.
        self.assertEqual(card["header"]["template"], "blue")
        self.assertIn("飞书", card["header"]["title"]["content"])
        blob = json.dumps(card, ensure_ascii=False)
        # 处理时间 + all four sections present.
        self.assertIn("处理时间", blob)
        for section in ("行情", "判断", "账户", "持仓", "本轮动作"):
            self.assertIn(section, blob)
        # 涨红 (A-share convention): positive pct is red — check the raw md text
        # (json.dumps escapes single quotes, so assert against the flattened text).
        texts = self._md_texts(card)
        self.assertTrue(
            any("<font color='red'>+1.57%</font>" in t for t in texts),
            f"red-colored pct not found in {texts}",
        )
        # Account decimal facts carried verbatim (no re-floating).
        self.assertIn("9994966.45", blob)
        # 观望 notice (no intents/fills).
        self.assertIn("本轮无可执行信号", blob)
        # run_id in footer.
        self.assertIn("run_id：run-1", blob)

    def test_task_name_and_symbol_names_rendered_in_card(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_signal_digest_card

        digest = self._market_digest()
        digest["details"]["position_intents"] = [
            {"symbol": "601398.SH", "action": "buy", "amount": 780, "rationale": "网格下轨"},
        ]
        card = build_signal_digest_card(
            trigger_name="收盘推送",
            digest=digest,
            processed_at="2026-06-14 23:36:02",
            no_signal_mode="brief",
            run_id="run-n",
            task_id="task-1",
            task_name="银行网格",
            symbol_names={"601398.SH": "工商银行"},
        )
        blob = json.dumps(card, ensure_ascii=False)
        # 任务名 subtitle + footer (footer keeps task_id for traceability).
        self.assertIn("任务：银行网格", blob)
        self.assertIn("task：银行网格（task-1）", blob)
        # 股票名称 beside the code in 行情 / 判断 / 本轮动作 (positions already had names).
        self.assertIn("工商银行（601398.SH）", blob)

    def test_card_without_names_falls_back_to_bare_code(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_signal_digest_card

        # No task_name / symbol_names → bare code, no subtitle, no raise.
        card = build_signal_digest_card(
            trigger_name="收盘推送", digest=self._market_digest(),
            no_signal_mode="full", run_id="run-1",
        )
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("601398.SH", blob)
        self.assertNotIn("工商银行", blob)
        self.assertNotIn("任务：", blob)

    def test_actionable_digest_uses_green_header_and_lists_intents(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_signal_digest_card

        digest = self._market_digest()
        digest["details"]["position_intents"] = [
            {"symbol": "601398.SH", "action": "buy", "amount": 780,
             "rationale": "网格下轨", "pending_approval": True},
        ]
        card = build_signal_digest_card(
            trigger_name="T", digest=digest, no_signal_mode="brief",
            run_id="run-x",
        )
        # Green header + intent line + 待审批 marker.
        self.assertEqual(card["header"]["template"], "green")
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("买入", blob)
        self.assertIn("（待审批）", blob)
        # Actionable forces full sections even in brief mode.
        self.assertIn("行情", blob)

    def test_failed_digest_uses_red_header_and_no_fact_sections(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_signal_digest_card

        digest = {"cycle_failed": True, "failure_message": "strategy blew up",
                  "wall_started_at": "2026-06-14T15:36:02"}
        card = build_signal_digest_card(
            trigger_name="T", digest=digest, processed_at="2026-06-14 23:36:02",
            no_signal_mode="full", run_id="run-err",
        )
        self.assertEqual(card["header"]["template"], "red")
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("strategy blew up", blob)
        # No fact sections on a failed cycle.
        self.assertNotIn("行情", blob)
        self.assertNotIn("账户", blob)

    def test_brief_card_mode_without_action_is_compact(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_signal_digest_card

        card = build_signal_digest_card(
            trigger_name="T", digest=self._market_digest(),
            processed_at="2026-06-14 23:36:02", no_signal_mode="brief",
        )
        blob = json.dumps(card, ensure_ascii=False)
        # Compact: only 处理时间 + 本轮动作 (no 行情/账户 even though data exists).
        self.assertNotIn("行情", blob)
        self.assertNotIn("账户", blob)
        self.assertIn("本轮无可执行信号", blob)
        self.assertIn("处理时间", blob)

    def test_prose_narration_sections_render_collapsed_advisory_panel(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_signal_digest_card

        card = build_signal_digest_card(
            trigger_name="T", digest=self._market_digest(),
            no_signal_mode="brief", prose_mode=True,
            narration_sections={
                "market": "601398.SH 7.78（+1.57%）",
                "judgement": "观望",
                "account": "现金充足",
                "action": "本轮无可执行信号。",
            },
            run_id="run-p",
        )
        # Prose mode forces full sections too.
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("行情", blob)
        # The AI 解读 panel is collapsed by default and labelled advisory.
        panel = next(
            (e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"),
            None,
        )
        self.assertIsNotNone(panel)
        self.assertFalse(panel["expanded"])
        self.assertIn("AI 解读", panel["header"]["title"]["content"])

    def test_missing_market_snapshot_shows_data_missing_not_silent(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_signal_digest_card

        digest = {"run_mode": "signal_only", "details": {"position_intents": [], "fills": []}}
        card = build_signal_digest_card(
            trigger_name="T", digest=digest, no_signal_mode="full",
        )
        # A missing 行情 section is NOT silently dropped — the operator sees 缺失.
        self.assertIn("行情数据缺失", json.dumps(card, ensure_ascii=False))


class TriggerChannelDigestCardTests(unittest.IsolatedAsyncioTestCase):
    """The Feishu channel delivery path now builds the rich digest card."""

    def _trg(self, **delivery):
        base = {
            "mode": "card",
            "no_signal_mode": "full",
            "target": {"kind": "channel", "channel_id": "ch-1", "chat_id": "oc_1"},
        }
        base.update(delivery)
        return _Trg(id="trg-d", name="收盘推送", delivery_json=base)

    def _asst(self, channel):
        asst = _ProseAsst()
        asst.channel_manager = _ChannelMgr(channel)
        return asst

    async def test_card_mode_builds_rich_signal_digest_card(self):
        from doyoutrade.assistant.channels.base import CardContent
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        channel = _FeishuDigestChannel()
        digest = {
            "run_mode": "signal_only", "cycle_failed": False,
            "wall_started_at": "2026-06-14T15:36:02",
            "details": {
                "position_intents": [], "fills": [],
                "market_snapshot": {"601398.SH": {"last_price": 7.78, "pct_change": 1.567}},
                "signal_diagnostics": {
                    "601398.SH": {"direction": "hold", "tag": "grid_l0", "rationale": "观望"},
                },
            },
        }
        status = await deliver_trigger_result(
            self._asst(channel), trigger=self._trg(), run_id="run-1",
            cycle_run_repository=_DigestRepo(digest),
        )
        self.assertEqual(status, "forwarded")
        self.assertEqual(len(channel.sent), 1)
        content = channel.sent[0]["content"]
        self.assertIsInstance(content, CardContent)
        # The rich card (NOT the old single-markdown-blob complete card) carries
        # the themed header + structured fact sections.
        card = content.card
        self.assertEqual(card["header"]["template"], "blue")
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("行情", blob)
        self.assertIn("+1.57%", blob)
        self.assertIn("run_id：run-1", blob)

    async def test_card_resolves_task_name_and_symbol_names_end_to_end(self):
        """The push resolves 任务名 / 股票名称 from the task + instrument catalogs."""
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        class _Catalog:
            async def get(self, symbol):
                if symbol == "601398.SH":
                    return {"symbol": symbol, "display_name": "工商银行"}
                return None

        class _TaskRepo:
            async def get_task(self, task_id):
                import types

                # Lightweight snapshot stand-in carrying only .name (the resolver
                # reads getattr(snapshot, "name", "")).
                return types.SimpleNamespace(name="银行网格")

        channel = _FeishuDigestChannel()
        digest = {
            "run_mode": "signal_only", "cycle_failed": False,
            "wall_started_at": "2026-06-14T15:36:02",
            "details": {
                "position_intents": [
                    {"symbol": "601398.SH", "action": "buy", "amount": 780, "rationale": "网格下轨"},
                ],
                "fills": [],
                "market_snapshot": {"601398.SH": {"last_price": 7.78, "pct_change": 1.567}},
                "signal_diagnostics": {
                    "601398.SH": {"direction": "buy", "tag": "grid_l0", "rationale": "突破"},
                },
            },
        }
        trg = self._trg()
        trg.task_id = "task-1"
        status = await deliver_trigger_result(
            self._asst(channel), trigger=trg, run_id="run-1",
            cycle_run_repository=_DigestRepo(digest),
            instrument_catalog_repository=_Catalog(),
            task_repository=_TaskRepo(),
        )
        self.assertEqual(status, "forwarded")
        card = channel.sent[0]["content"].card
        blob = json.dumps(card, ensure_ascii=False)
        # 任务名 subtitle + footer (with task_id kept for traceability).
        self.assertIn("任务：银行网格", blob)
        self.assertIn("task：银行网格（task-1）", blob)
        # 股票名称 beside the code in the pushed card.
        self.assertIn("工商银行（601398.SH）", blob)

    async def test_missing_repos_fall_back_to_bare_code_silently(self):
        """No catalogs passed → bare code / no 任务名, push still succeeds."""
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        channel = _FeishuDigestChannel()
        digest = {
            "run_mode": "signal_only", "cycle_failed": False,
            "details": {
                "position_intents": [], "fills": [],
                "market_snapshot": {"601398.SH": {"last_price": 7.78, "pct_change": 1.0}},
            },
        }
        status = await deliver_trigger_result(
            self._asst(channel), trigger=self._trg(), run_id="run-1",
            cycle_run_repository=_DigestRepo(digest),
        )
        self.assertEqual(status, "forwarded")
        blob = json.dumps(channel.sent[0]["content"].card, ensure_ascii=False)
        self.assertIn("601398.SH", blob)
        self.assertNotIn("工商银行", blob)
        self.assertNotIn("任务：", blob)

    async def test_prose_mode_narration_rides_along_as_ai_panel(self):
        from doyoutrade.assistant.channels.base import CardContent
        from doyoutrade.runtime.trigger_delivery import deliver_trigger_result

        import json as _json
        reply = _json.dumps({
            "market": "601398.SH 7.78（+1.57%）",
            "judgement": "观望",
            "account": "现金充足",
            "action": "本轮无可执行信号。",
        })
        channel = _FeishuDigestChannel()
        asst = _ProseAsst(reply=reply)
        asst.channel_manager = _ChannelMgr(channel)
        trg = self._trg(mode="prose", composer_agent_id="ag-1")
        status = await deliver_trigger_result(
            asst, trigger=trg, run_id="run-1",
            cycle_run_repository=_DigestRepo(_prose_digest()),
        )
        self.assertEqual(status, "forwarded")
        card = channel.sent[0]["content"].card
        blob = json.dumps(card, ensure_ascii=False)
        # Structured fact sections came from the digest, AND the composer's
        # narration is present as a collapsed AI 解读 panel.
        self.assertIn("行情", blob)
        self.assertIn("AI 解读", blob)
        self.assertIn("观望", blob)


if __name__ == "__main__":
    unittest.main()
