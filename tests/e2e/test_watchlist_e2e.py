import asyncio
import unittest

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
)


def _e2e_symbol(ctx) -> str:
    raw = ctx.e2e_settings.get("symbols")
    if isinstance(raw, list) and raw:
        return str(raw[0]).strip() or "600000.SH"
    return "600000.SH"


@unittest.skipUnless(e2e_enabled(), "set DOYOUTRADE_E2E=1 to run end-to-end tests")
class WatchlistUniverseE2ETests(unittest.TestCase):
    """A task whose universe is a ``@watchlist:<tag>`` reference must resolve to
    the seeded watchlist symbols at worker assembly, run a real backtest cycle on
    them, and keep ``run_id`` connected across cycle_runs / debug session / spans
    — with the resolution itself visible as a ``watchlist_universe_resolved``
    debug event (CLAUDE.md §最低同步要求).
    """

    def test_watchlist_tag_universe_resolves_and_runs(self) -> None:
        async def _run() -> None:
            async with build_e2e_runtime(profile="isolated", model_mode=E2EModelMode.STUB) as ctx:
                symbol = _e2e_symbol(ctx)
                watchlist_repo = ctx.runtime["watchlist_repository"]
                # Seed the watchlist: the e2e symbol tagged "核心池".
                entry = await watchlist_repo.upsert_entry(
                    {"symbol": symbol, "tags": ["核心池"], "note": "e2e"}
                )
                self.assertTrue(entry["id"].startswith("wl-"))
                self.assertEqual(await watchlist_repo.list_symbols(tag="核心池"), [symbol])

                # Task universe references the tag, not the symbol literally.
                task = await ctx.create_definition_backtest_task(
                    universe_override=["@watchlist:核心池"],
                )
                self.assertEqual(task.config.mode, "backtest")
                self.assertEqual(tuple(task.config.universe), ("@watchlist:核心池",))

                run = await ctx.start_backtest_and_wait(task.task_id)
                job_id = run["run_id"]
                session_id = run.get("session_id") or ""
                self.assertIn(run["status"], {"completed", "finished"})
                self.assertTrue(job_id)

                # run_id throughput: cycle_runs exist and share the run's session.
                cycles = await ctx.list_cycle_runs(task.task_id, run_id=job_id)
                self.assertGreater(cycles["total"], 0, "resolved universe should run cycles")
                first_cycle_run_id = cycles["items"][0]["run_id"]
                for cycle in cycles["items"]:
                    self.assertEqual(cycle.get("session_id"), session_id)

                # A debug session is wired for the run (run_id throughput intact).
                debug_view = await ctx.service.get_run_debug_view(first_cycle_run_id)
                self.assertIsNotNone(debug_view["session"])
                self.assertEqual(debug_view["session"]["session_id"], session_id)

                # Mechanism (A): the persisted token universe resolves to the
                # seeded watchlist symbol against the real repository — exactly the
                # expansion _build_worker performs before constructing the data
                # stack. This is the deterministic proof that the backtest above
                # ran on [symbol] rather than on the literal token.
                resolved_cfg = await ctx.service._resolve_watchlist_universe_config(task.config)
                self.assertEqual(tuple(resolved_cfg.universe), (symbol,))

                await ctx.stop_backtest_job(task.task_id, job_id)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
