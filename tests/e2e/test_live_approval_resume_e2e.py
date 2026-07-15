"""E2E: live-trading approval held → approved → resumed → fill, run_id intact.

Exercises the real persistence surfaces (real SQL ``approvals`` table via the
migration, ``cycle_runs``, ``trade_fills``) and the real scheduler resume sweep —
not in-memory fakes. The invariant under test:

    a live cycle holds a buy order for approval (pending_approval_count >= 1,
    NO fill yet) → approve via the real gate → scheduler resume sweep dispatches
    it through the task's worker → a trade_fill appears, correlated to the
    ORIGINAL cycle via ``trade_fills.cycle_run_id == cycle_runs.run_id``.

A second test pins the SQL approval repository round-trip (run context +
intent_payload persist and the resumable lifecycle) against the real DB engine,
covering what the in-memory unit tests cannot.
"""
from __future__ import annotations

import unittest

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
    wait_for_model_invocation_tasks,
)


@unittest.skipUnless(e2e_enabled(), "set DOYOUTRADE_E2E=1 to run end-to-end tests")
class LiveApprovalResumeE2E(unittest.IsolatedAsyncioTestCase):
    async def test_approved_order_resumes_to_fill_via_scheduler(self):
        """Held live order → approve → REAL scheduler resume sweep → fill.

        Exercises the real integration: TriggerScheduler._resume_approved_intents
        → service.dispatch_resumed_approval → the running task's live worker →
        PaperExecutionAdapter → trade_fills, all against the real SQL DB. The
        pending approval is persisted via the real gate bound to the running
        task's id (a live cycle is what normally creates it; the isolated mock
        harness can't price an instrument to size a cycle intent, but resume uses
        the captured intent.price_reference and so is independent of that).
        """
        async with build_e2e_runtime(model_mode=E2EModelMode.STUB) as ctx:
            from sqlalchemy import select

            from doyoutrade.core.cycle_state import CycleRunState
            from doyoutrade.core.models import OrderIntent
            from doyoutrade.persistence.models import TradeFillRecord
            from doyoutrade.runtime.trigger_scheduler import TriggerScheduler

            service = ctx.service
            trigger_repo = ctx.runtime["task_trigger_repository"]
            approval_gate = ctx.runtime["approval_gate"]
            trade_fill_repo = ctx.runtime["trade_fill_repository"]

            async def _fills_for_run(task_id: str, cycle_run_id: str) -> list:
                # Live/resume fills are keyed by cycle_run_id (run_id column is the
                # backtest-parent grouping, null here), so query that correlation
                # directly — it IS the cycle_runs.run_id ↔ trade_fills.cycle_run_id link.
                async with trade_fill_repo.session_factory() as session:
                    rows = (
                        await session.execute(
                            select(TradeFillRecord).where(
                                TradeFillRecord.task_id == task_id,
                                TradeFillRecord.cycle_run_id == cycle_run_id,
                            )
                        )
                    ).scalars().all()
                    return list(rows)

            task = await ctx.create_agent_task(mode="live")
            await service.start_task(task.task_id)

            # A live order held for approval, bound to the running task + a run_id.
            run_id = "run-e2e-resume"
            intent = OrderIntent(
                intent_id="oi-e2e-resume",
                symbol="600000.SH",
                action="buy",
                amount=10000.0,
                order_type="limit",
                tif="day",
                strategy_tag="E2E",
                price_reference=10.0,
                rationale="e2e-resume",
                signal_tag="sig",
            )
            state = CycleRunState(
                run_id=run_id, trace_id="tr-e2e", task_id=task.task_id, agent_name="agent"
            )
            result = await approval_gate.request(
                intent, mode="live", cycle_state=state, account_id="acct-e2e"
            )
            self.assertEqual(result.status, "pending")

            # Held → no fill yet.
            before = await _fills_for_run(task.task_id, run_id)
            self.assertEqual(before, [], "held order must not fill before approval")

            # Approve, then drive the REAL scheduler resume sweep.
            await approval_gate.approve(
                result.approval_id, resolver_id="e2e-operator", decision_source="web"
            )
            sched = TriggerScheduler(
                service=service,
                trigger_repository=trigger_repo,
                approval_gate=approval_gate,
            )
            await sched._resume_approved_intents()
            await wait_for_model_invocation_tasks()

            # Filled, correlated to the ORIGINAL run_id via cycle_run_id.
            after = await _fills_for_run(task.task_id, run_id)
            self.assertTrue(after, "approved order should fill on scheduler resume")
            self.assertEqual(after[0].side, "buy")
            self.assertEqual(float(after[0].quantity), 1000.0)  # 10000 / 10.0

            # Idempotent: no longer resumable (dispatched stamped).
            still_resumable = [
                r for r in await approval_gate.list_resumable()
                if r.approval_id == result.approval_id
            ]
            self.assertEqual(still_resumable, [], "dispatched approval must not resume again")

    async def test_resume_pushes_order_result_card_with_actual_fill(self):
        """R2: a real resume fill pushes a deterministic 已成交 result card.

        Proves the post-dispatch receipt is wired into the REAL scheduler resume
        sweep: the result card carries the ACTUAL fill (quantity/price computed by
        the live worker, not the card) and the ORIGINAL run_id — so 已批准 is never
        mistaken for 已成交 and the fact path stays deterministic end-to-end.
        """
        async with build_e2e_runtime(model_mode=E2EModelMode.STUB) as ctx:
            from doyoutrade.core.cycle_state import CycleRunState
            from doyoutrade.core.models import OrderIntent
            from doyoutrade.runtime.trigger_scheduler import TriggerScheduler

            service = ctx.service
            trigger_repo = ctx.runtime["task_trigger_repository"]
            approval_gate = ctx.runtime["approval_gate"]

            # A fake Feishu channel that captures the pushed result card, wired
            # behind a minimal assistant_service.channel_manager.
            class _Captured:
                channel_type = "feishu"

                def __init__(self):
                    self.results = []

                async def send_trade_approval_result_card(self, chat_id, payload, *, outcome):
                    self.results.append({"chat_id": chat_id, "payload": payload, "outcome": outcome})
                    return "om_e2e_result"

            captured = _Captured()

            class _Mgr:
                def get(self, channel_id):
                    return captured

            class _Asst:
                channel_manager = _Mgr()

            task = await ctx.create_agent_task(mode="live")
            await service.start_task(task.task_id)
            # A channel-bound trigger so the result card can re-resolve the target
            # from the task (the resume sweep has no trigger in hand).
            await trigger_repo.create_trigger(
                task_id=task.task_id,
                schedule_kind="interval",
                interval_seconds=60,
                execution_intent="trade",
                delivery_json={
                    "target": {"kind": "channel", "channel_id": "ch-e2e", "chat_id": "oc_e2e"}
                },
            )

            run_id = "run-e2e-result-card"
            intent = OrderIntent(
                intent_id="oi-e2e-result",
                symbol="600000.SH",
                action="buy",
                amount=10000.0,
                order_type="limit",
                tif="day",
                strategy_tag="E2E",
                price_reference=10.0,
                rationale="e2e-result",
                signal_tag="sig",
            )
            state = CycleRunState(
                run_id=run_id, trace_id="tr-e2e", task_id=task.task_id, agent_name="agent"
            )
            result = await approval_gate.request(
                intent, mode="live", cycle_state=state, account_id="acct-e2e"
            )
            await approval_gate.approve(
                result.approval_id, resolver_id="e2e-operator", decision_source="feishu_card"
            )

            sched = TriggerScheduler(
                service=service,
                trigger_repository=trigger_repo,
                assistant_service=_Asst(),
                approval_gate=approval_gate,
            )
            await sched._resume_approved_intents()
            await wait_for_model_invocation_tasks()

            # The receipt card was pushed with the ACTUAL fill + original run_id.
            self.assertEqual(len(captured.results), 1, "a filled order must push one result card")
            rec = captured.results[0]
            self.assertEqual(rec["outcome"], "filled")
            self.assertEqual(rec["chat_id"], "oc_e2e")
            payload = rec["payload"]
            self.assertEqual(payload["run_id"], run_id)          # run_id 贯穿
            self.assertEqual(payload["symbol"], "600000.SH")
            self.assertEqual(payload["fill_quantity"], "1000")   # 10000 / 10.0, from the live worker
            self.assertEqual(payload["fill_price"], "10")
            self.assertEqual(payload["fill_amount"], "10000")

    async def test_real_sql_approval_repo_round_trip(self):
        """SQL approval repo: run context + intent_payload persist; resumable
        lifecycle (pending → approved → dispatched) works against the real DB."""
        async with build_e2e_runtime(model_mode=E2EModelMode.STUB) as ctx:
            from doyoutrade.core.cycle_state import CycleRunState
            from doyoutrade.core.models import OrderIntent, intent_from_json

            gate = ctx.runtime["approval_gate"]
            intent = OrderIntent(
                intent_id="oi-e2e-1",
                symbol="600000.SH",
                action="buy",
                amount=10000.0,
                order_type="limit",
                tif="day",
                strategy_tag="E2E",
                price_reference=10.0,
                rationale="e2e",
                signal_tag="sig",
            )
            state = CycleRunState(
                run_id="run-e2e-approval", trace_id="tr-e2e", task_id="task-e2e", agent_name="a"
            )
            result = await gate.request(
                intent, mode="live", cycle_state=state, account_id="acct-e2e"
            )
            self.assertEqual(result.status, "pending")

            pending = [p for p in await gate.list_pending() if p.approval_id == result.approval_id]
            self.assertEqual(len(pending), 1)
            p = pending[0]
            self.assertEqual(p.run_id, "run-e2e-approval")
            self.assertEqual(p.task_id, "task-e2e")
            self.assertEqual(p.account_id, "acct-e2e")
            self.assertEqual(p.notional, "10000")
            # Intent body survived the DB round-trip and rebuilds.
            oi = intent_from_json(p.intent_payload)
            self.assertEqual(oi.intent_id, "oi-e2e-1")
            self.assertEqual(oi.amount, 10000.0)

            await gate.approve(result.approval_id, resolver_id="op", decision_source="feishu_card")
            resumable = [r for r in await gate.list_resumable() if r.approval_id == result.approval_id]
            self.assertEqual(len(resumable), 1)
            self.assertEqual(resumable[0].resolver_id, "op")
            self.assertEqual(resumable[0].decision_source, "feishu_card")

            # History view (the GET /approvals query) sees the resolved row with
            # filters applied, against the real SQL DB — what the in-memory unit
            # tests cannot cover (ilike, func.count, status filter on postgres).
            history, total = await gate.list_approvals(
                statuses=["approved"], symbol="600000.SH", search="oi-e2e-1"
            )
            self.assertTrue(any(r.approval_id == result.approval_id for r in history))
            self.assertGreaterEqual(total, 1)
            # A non-matching status filter excludes the approved row entirely.
            only_pending, _ = await gate.list_approvals(statuses=["pending"], search="oi-e2e-1")
            self.assertEqual(
                [r for r in only_pending if r.approval_id == result.approval_id], []
            )

            self.assertTrue(await gate.mark_dispatched(result.approval_id))
            self.assertEqual(
                [r for r in await gate.list_resumable() if r.approval_id == result.approval_id],
                [],
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
