import asyncio
import os
import unittest

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
    wait_for_debug_session_terminal,
)


@unittest.skipUnless(e2e_enabled(), "set DOYOUTRADE_E2E=1 to run end-to-end tests")
class RuntimeE2ETests(unittest.TestCase):
    def test_e2e_config_loads_root_config_with_profile_overrides(self) -> None:
        async def _run() -> None:
            async with build_e2e_runtime(profile="isolated") as ctx:
                self.assertTrue(ctx.root_config_path.name.endswith(".yaml"))
                self.assertEqual(ctx.app_config.data.default_provider, "mock")
                self.assertTrue(ctx.app_config.observability.tracing_enabled)
                self.assertIn("sqlite+aiosqlite:///", ctx.app_config.database.url)

        asyncio.run(_run())

    def test_manual_tick_persists_run_debug_session_and_spans(self) -> None:
        async def _run() -> None:
            async with build_e2e_runtime(profile="isolated", model_mode=E2EModelMode.STUB) as ctx:
                task = await ctx.create_agent_task(mode="live", status="running")

                executed = await ctx.service.tick_once(source="manual")
                self.assertGreaterEqual(executed, 1)

                cycles = await ctx.list_cycle_runs(task.task_id, run_kind="manual")
                self.assertEqual(cycles["total"], 1)
                run = cycles["items"][0]
                self.assertIn(run["status"], {"completed", "finished"})
                self.assertEqual(run["run_kind"], "manual")
                self.assertTrue(run["run_id"])
                self.assertTrue(run["session_id"])
                self.assertTrue(run["trace_id"])

                debug_view = await ctx.service.get_run_debug_view(run["run_id"])
                self.assertEqual(debug_view["cycle_run"]["run_id"], run["run_id"])
                self.assertIsNotNone(debug_view["session"])
                self.assertEqual(debug_view["session"]["session_id"], run["session_id"])
                self.assertGreater(len(debug_view["spans"]), 0)
                for span in debug_view["spans"]:
                    self.assertEqual(span.get("session_id"), run["session_id"])
                for item in debug_view["model_invocations"]:
                    self.assertEqual(item["run_id"], run["run_id"])

                await ctx.stop_agent_task(task.task_id)

        asyncio.run(_run())

    def test_debug_session_runs_real_cycle_and_running_task_is_rejected(self) -> None:
        async def _run() -> None:
            async with build_e2e_runtime(profile="isolated", model_mode=E2EModelMode.STUB) as ctx:
                task = await ctx.create_agent_task(mode="live", status="configured")
                created = await ctx.service.start_debug_session(
                    task.task_id,
                    input_overrides={"debug_note": "e2e-debug"},
                )
                session = await wait_for_debug_session_terminal(
                    ctx.service,
                    task.task_id,
                    created["session_id"],
                )
                self.assertEqual(session["status"], "completed")
                self.assertTrue(session["run_id"])
                self.assertGreater(len(session["spans"]), 0)
                for item in session["model_invocations"]:
                    self.assertEqual(item["run_id"], session["run_id"])

                await ctx.service.start_task(task.task_id)
                with self.assertRaisesRegex(RuntimeError, "running instances do not support debug"):
                    await ctx.service.start_debug_session(task.task_id)

                await ctx.stop_agent_task(task.task_id)

        asyncio.run(_run())


@unittest.skipUnless(e2e_enabled(), "set DOYOUTRADE_E2E=1 to run end-to-end tests")
class BacktestE2ETests(unittest.TestCase):
    """E2E tests for definition-instance backtest tasks."""

    def test_definition_instance_backtest_completes(self) -> None:
        async def _run() -> None:
            async with build_e2e_runtime(profile="isolated", model_mode=E2EModelMode.STUB) as ctx:
                task = await ctx.create_definition_backtest_task()
                self.assertEqual(task.config.mode, "backtest")
                self.assertTrue(task.config.strategy_definition_id)

                run = await ctx.start_backtest_and_wait(task.task_id)
                job_id = run["run_id"]
                session_id = run.get("session_id") or ""

                self.assertTrue(job_id)
                self.assertEqual(run["task_id"], task.task_id)
                self.assertIn(run["status"], {"completed", "finished"})

                cycles = await ctx.list_cycle_runs(task.task_id, run_id=job_id)
                self.assertGreater(cycles["total"], 0, "cycle_runs should have at least one record")
                first_cycle_run_id = cycles["items"][0]["run_id"]
                for cycle in cycles["items"]:
                    self.assertEqual(cycle.get("session_id"), session_id)

                debug_view = await ctx.service.get_run_debug_view(first_cycle_run_id)
                self.assertIsNotNone(debug_view["session"], "debug_sessions record must exist for backtest")
                self.assertEqual(debug_view["session"]["session_id"], session_id)
                self.assertGreater(len(debug_view["spans"]), 0)
                run_strategy_spans = [
                    span for span in debug_view["spans"]
                    if span.get("name") == "worker.phase.generate_signals"
                ]
                self.assertGreater(len(run_strategy_spans), 0)
                attrs = run_strategy_spans[0].get("attributes") or {}
                events = attrs.get("_events") or []
                definition_events = [
                    item for item in events
                    if item.get("event_type") == "strategy_definition_execution"
                ]
                self.assertGreater(len(definition_events), 0)
                payload = definition_events[0].get("payload") or {}
                self.assertTrue(payload.get("strategy_definition_id"))
                self.assertEqual(payload.get("strategy_execution_profile"), "default")
                trace = payload.get("trace") or {}
                self.assertEqual(trace.get("definition_id"), payload.get("strategy_definition_id"))
                self.assertTrue(trace.get("definition_id"))

                await ctx.stop_backtest_job(task.task_id, job_id)

        asyncio.run(_run())

    def test_fast_mode_backtest_skips_observability_but_keeps_report(self) -> None:
        """A ``debug_enabled=False`` backtest must complete and persist its
        report/summary while writing NO debug session / spans / cycle_runs /
        model_invocations. The absence of trace is intentional and surfaced
        via ``runs.debug_enabled`` + an explicit get_run_debug_view payload.
        """

        async def _run() -> None:
            async with build_e2e_runtime(profile="isolated", model_mode=E2EModelMode.STUB) as ctx:
                task = await ctx.create_definition_backtest_task()

                run = await ctx.start_backtest_and_wait(task.task_id, debug_enabled=False)
                job_id = run["run_id"]
                self.assertIn(run["status"], {"completed", "finished"})
                # Fast mode is recorded as a first-class field, and no debug
                # session was created.
                self.assertEqual(run["debug_enabled"], False)
                self.assertIsNone(run.get("session_id"))

                # No cycle_runs / model_invocations rows for this run.
                cycles = await ctx.list_cycle_runs(task.task_id, run_id=job_id)
                self.assertEqual(cycles["total"], 0, "fast mode must not persist cycle_runs")
                invocations = await ctx.service.model_invocation_repository.list_invocations_for_run(job_id)
                self.assertEqual(invocations, [], "fast mode must not persist model_invocations")

                # No backtest debug session for this task.
                sessions = await ctx.service.debug_session_repository.list_sessions(task.task_id)
                self.assertEqual(
                    [s for s in sessions if getattr(s, "session_type", None) == "backtest"],
                    [],
                    "fast mode must not create a backtest debug session",
                )

                # get_run_debug_view returns an explicit fast-mode payload, not an error.
                debug_view = await ctx.service.get_run_debug_view(job_id)
                self.assertEqual(debug_view["debug_enabled"], False)
                self.assertEqual(debug_view["debug_unavailable_reason"], "debug_disabled")
                self.assertEqual(debug_view["spans"], [])
                self.assertEqual(debug_view["cycle_runs"], [])
                self.assertEqual(debug_view["model_invocations"], [])

                # Core result is preserved: the run carries a persisted summary.
                record = await ctx.service.task_repository.get_task(task.task_id)
                self.assertIsInstance(record.backtest_summary, dict)

                await ctx.stop_backtest_job(task.task_id, job_id)

        asyncio.run(_run())

    def test_definition_instance_backtest_summary_open_positions_have_prices(self) -> None:
        """End-of-run open positions must carry last_price / market_value / weight_pct.

        Guards against the regression where ``FinalPosition`` was built only from
        ``PositionSnapshot.market_price`` (None at end-of-run under the mock
        store) so the open-position section of ``backtest_summary`` rendered
        市值 / 现价 / 仓位占比 as "—" even though the price was present in
        ``ledger_checkpoint_json.symbol_to_price``.

        Asserts the cross-table relationship between ``runs.ledger_checkpoint_json``
        and ``tasks.backtest_summary.final_positions`` — i.e. that any open
        position in the summary has a ``last_price`` traceable to the ledger
        checkpoint's ``symbol_to_price`` map. The current stub strategy may
        not actually open trades in this fixture, so the contract is asserted
        conditionally; absence of open positions is not a failure.
        """

        async def _run() -> None:
            from decimal import Decimal

            async with build_e2e_runtime(profile="isolated", model_mode=E2EModelMode.STUB) as ctx:
                task = await ctx.create_definition_backtest_task()
                run = await ctx.start_backtest_and_wait(task.task_id)
                job_id = run["run_id"]
                self.assertIn(run["status"], {"completed", "finished"})

                record = await ctx.service.task_repository.get_task(task.task_id)
                summary = record.backtest_summary
                self.assertIsInstance(summary, dict)
                assert summary is not None
                self.assertIn("final_positions", summary)

                final_positions = summary.get("final_positions") or []
                for fp in final_positions:
                    sym = fp.get("symbol")
                    self.assertTrue(
                        fp.get("last_price"),
                        msg=f"final position {sym} missing last_price",
                    )
                    self.assertTrue(
                        fp.get("market_value"),
                        msg=f"final position {sym} missing market_value",
                    )
                    self.assertTrue(
                        fp.get("weight_pct"),
                        msg=f"final position {sym} missing weight_pct (requires market_value + ending_equity)",
                    )

                job_row = await ctx.service.get_backtest_job(task.task_id, job_id)
                ck = job_row.get("ledger_checkpoint_json") or {}
                checkpoint_prices = ck.get("symbol_to_price") if isinstance(ck, dict) else None
                if final_positions and isinstance(checkpoint_prices, dict) and checkpoint_prices:
                    by_sym = {fp["symbol"]: fp for fp in final_positions}
                    for sym, raw_px in checkpoint_prices.items():
                        if sym not in by_sym:
                            continue
                        self.assertEqual(
                            Decimal(by_sym[sym]["last_price"]),
                            Decimal(str(raw_px)),
                            msg=f"{sym} last_price must equal ledger_checkpoint price",
                        )

                await ctx.stop_backtest_job(task.task_id, job_id)

        asyncio.run(_run())

if __name__ == "__main__":
    unittest.main()
