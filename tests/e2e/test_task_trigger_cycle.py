"""E2E: a Task Trigger fire propagates run_id + trigger_id end-to-end.

Phase 1 invariant — firing a Trigger runs exactly one real worker cycle through the
same path as every other trigger source, and the attribution chain holds:

    task_triggers.id (trigger_id)
        == cycle_runs.trigger_id            (run_kind == "trigger")
    cycle_runs.run_id
        == debug_sessions.run_id            (session_id starts with "trigger-")
        == model_invocations.run_id

It also asserts the no-duplicate-cycle_runs-per-fire property (two fires → two
distinct run_ids / cycle_runs rows) that gates the Phase 2 scheduler cutover.

``service.run_trigger`` is exactly what ``TriggerScheduler`` calls, so this exercises
the real persistence surfaces (cycle_runs, debug_sessions / span export, model
invocations) rather than in-memory state.
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
class TaskTriggerCycleE2E(unittest.IsolatedAsyncioTestCase):
    async def test_trigger_fire_propagates_run_id_and_trigger_id(self):
        async with build_e2e_runtime(model_mode=E2EModelMode.STUB) as ctx:
            service = ctx.service
            trigger_repo = ctx.runtime["task_trigger_repository"]
            debug_session_repository = ctx.runtime["debug_session_repository"]
            cycle_run_repository = ctx.runtime["cycle_run_repository"]
            model_invocation_repository = ctx.runtime["model_invocation_repository"]

            # 1. A running paper task that owns a signal_only interval trigger.
            task = await ctx.create_agent_task(mode="paper")
            await service.start_task(task.task_id)

            trigger = await trigger_repo.create_trigger(
                task_id=task.task_id,
                name="e2e-trigger",
                schedule_kind="interval",
                interval_seconds=300,
                execution_intent="signal_only",
                delivery_json=None,
            )

            # 2. Fire it exactly as TriggerScheduler would.
            run_id = await service.run_trigger(trigger)
            self.assertIsInstance(run_id, str)
            self.assertTrue(run_id.startswith("run-"), run_id)

            await wait_for_model_invocation_tasks()

            # 3a. cycle_runs row: tagged run_kind='trigger' + trigger_id, right task.
            cycle_row = await cycle_run_repository.get_by_run_id(run_id)
            self.assertIsNotNone(cycle_row, "cycle_runs row missing for trigger run_id")
            self.assertEqual(cycle_row["task_id"], task.task_id)
            self.assertEqual(cycle_row["run_kind"], "trigger")
            self.assertEqual(cycle_row["trigger_id"], trigger.id)

            # 3b. debug_sessions: a row whose run_id matches and session is a trigger session.
            sessions = await debug_session_repository.list_sessions(task.task_id)
            matching = [s for s in sessions if s.run_id == run_id]
            self.assertTrue(
                matching,
                f"no debug session with run_id={run_id}; "
                f"have={[(s.session_id, s.run_id) for s in sessions]}",
            )
            self.assertTrue(
                matching[0].session_id.startswith("trigger-"),
                f"unexpected session_id prefix: {matching[0].session_id}",
            )
            self.assertEqual(matching[0].session_type, "trigger")
            self.assertEqual(cycle_row["session_id"], matching[0].session_id)

            # 3c. model_invocations keyed by run_id all belong to this task (invariant;
            #     the deterministic stub strategy may record zero).
            invocations = await model_invocation_repository.list_invocations_for_run(run_id)
            for inv in invocations:
                self.assertEqual(inv["run_id"], run_id)
                self.assertEqual(inv["task_id"], task.task_id)

            # 4. No-duplicate-per-fire: a second fire mints a distinct run_id / cycle_runs row.
            run_id_2 = await service.run_trigger(trigger)
            self.assertIsInstance(run_id_2, str)
            self.assertNotEqual(run_id_2, run_id)
            cycle_row_2 = await cycle_run_repository.get_by_run_id(run_id_2)
            self.assertIsNotNone(cycle_row_2)
            self.assertEqual(cycle_row_2["trigger_id"], trigger.id)

    async def test_signal_only_trigger_on_paper_task_places_no_orders(self):
        """execution_intent=signal_only overrides the paper task's run_mode for the
        fire, so the cycle short-circuits after generate_signals (no fills)."""
        async with build_e2e_runtime(model_mode=E2EModelMode.STUB) as ctx:
            service = ctx.service
            trigger_repo = ctx.runtime["task_trigger_repository"]
            cycle_run_repository = ctx.runtime["cycle_run_repository"]

            task = await ctx.create_agent_task(mode="paper")
            await service.start_task(task.task_id)
            trigger = await trigger_repo.create_trigger(
                task_id=task.task_id,
                name="e2e-signal-only",
                schedule_kind="interval",
                interval_seconds=300,
                execution_intent="signal_only",
                delivery_json=None,
            )
            run_id = await service.run_trigger(trigger)
            cycle_row = await cycle_run_repository.get_by_run_id(run_id)
            self.assertEqual(cycle_row["run_mode"], "signal_only")
            # signal_only short-circuits dispatch -> no submitted orders.
            self.assertIn(cycle_row.get("submitted_count"), (0, None))
            details = cycle_row.get("details") or {}
            self.assertEqual(details.get("fills") or [], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
