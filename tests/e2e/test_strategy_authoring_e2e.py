"""E2E: strategy authoring lifecycle and code_version pinning.

Covers the four scenarios from Task 10 of
docs/superpowers/plans/2026-05-24-strategy-as-files.md:

1. open + write + compile + finalize  → version_label starts with "v0001-"
2. cycle pin                          → cycle_run.code_version == v0001 label
3. mid-flight version bump            → in-flight cycle keeps v0001,
                                        new cycle picks up v0002
4. sandbox rejection                  → write_strategy_file with
                                        file_path="../../../etc/passwd"
                                        returns error_code="path_outside_workspace"

All assertions are against persisted artifacts (cycle_runs table, definition
record) rather than in-memory return values only.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
)
from doyoutrade.strategy_registry import StrategyDefinitionCreate


# ---------------------------------------------------------------------------
# Strategy code used for both v0001 and v0002 (v0002 has a trivial comment
# change so the hash is different).
# ---------------------------------------------------------------------------

_STRATEGY_V1 = """\
from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

class Strategy(BaseStrategy):
    startup_history = 10

    def on_bar(self, df, ctx):
        return Signal.hold()
"""

_STRATEGY_V2 = """\
from doyoutrade.strategy_sdk import Strategy as BaseStrategy, Signal

class Strategy(BaseStrategy):
    startup_history = 10

    # v2: bumped by mid-flight test
    def on_bar(self, df, ctx):
        return Signal.hold()
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine in a fresh event loop (unittest-compatible)."""
    return asyncio.run(coro)


async def _write_draft_and_finalize(
    storage,
    definition_repository,
    definition_id: str,
    session_id: str,
    code: str,
) -> tuple[str, str]:
    """Write *code* to a draft and finalize it.  Returns (version_label, code_hash)."""
    draft = storage.open_draft(definition_id, session_id, base_version=None)
    (draft / "strategy.py").write_text(code, encoding="utf-8")
    version_label, code_hash = storage.finalize_draft(definition_id, session_id)
    await definition_repository.update_definition(
        definition_id,
        current_version=version_label,
        code_hash=code_hash,
        status="active",
    )
    return version_label, code_hash


# ---------------------------------------------------------------------------
# Test case
# ---------------------------------------------------------------------------


@unittest.skipUnless(e2e_enabled(), "set DOYOUTRADE_E2E=1 to run end-to-end tests")
class StrategyAuthoringLifecycleE2E(unittest.TestCase):
    """End-to-end tests for strategy authoring lifecycle and code_version pinning."""

    # ------------------------------------------------------------------
    # Scenario 1 + 2: open → write → compile → finalize → cycle pin
    # ------------------------------------------------------------------

    def test_open_write_compile_finalize_and_cycle_pin(self) -> None:
        """Open a new authoring session, write strategy.py, compile (no persist),
        finalize (promote), then run a cycle and verify the CycleRunRecord carries
        the correct code_version and code_hash."""

        async def _run_test() -> None:
            async with build_e2e_runtime(
                profile="isolated", model_mode=E2EModelMode.STUB
            ) as ctx:
                service = ctx.service
                storage = ctx.runtime["strategy_storage"]
                defn_repo = ctx.runtime["strategy_definition_repository"]
                cycle_run_repo = ctx.runtime["cycle_run_repository"]
                strategy_registry = ctx.runtime["strategy_registry_service"]
                compiler = ctx.runtime["compiler"]

                # ---- 1a. Create a new strategy definition (no source_code column) ----
                definition_id = "sd-e2e-authoring-01"
                await strategy_registry.create_definition(
                    StrategyDefinitionCreate(
                        definition_id=definition_id,
                        name="E2E Authoring Test Strategy",
                        api_version="v1",
                        parameter_schema={},
                        default_parameters={},
                        capabilities={},
                        provenance={"source": "e2e-authoring"},
                    )
                )

                # ---- 1b. Simulate authoring session: open draft ----
                session_id_1 = "sess-e2e-authoring-01"
                draft_dir = storage.open_draft(
                    definition_id, session_id_1, base_version=None
                )
                self.assertTrue(draft_dir.is_dir(), "draft directory must exist after open")
                # Scaffold should be present
                self.assertTrue(
                    (draft_dir / "strategy.py").is_file(),
                    "strategy.py scaffold must be written on open",
                )

                # ---- 1c. Write a valid strategy body (simulates write_strategy_file) ----
                (draft_dir / "strategy.py").write_text(_STRATEGY_V1, encoding="utf-8")

                # ---- 1d. Compile (no persist) ----
                compile_result = compiler.validate_directory(draft_dir)
                self.assertTrue(
                    compile_result.success,
                    f"compile must succeed; got errors: {compile_result.errors}",
                )

                # ---- 1e. Finalize (promote to versioned dir + update DB) ----
                v1_label, v1_hash = storage.finalize_draft(definition_id, session_id_1)
                await defn_repo.update_definition(
                    definition_id,
                    current_version=v1_label,
                    code_hash=v1_hash,
                    status="active",
                )

                self.assertTrue(v1_label.startswith("v0001-"), v1_label)

                # version directory must exist on disk
                versions_dir = storage.versions_dir(definition_id)
                self.assertTrue((versions_dir / v1_label).is_dir())

                # draft dir must be gone (promoted via rename)
                draft_gone = storage.draft_dir(definition_id, session_id_1)
                self.assertFalse(draft_gone.exists(), "draft dir must be gone after finalize")

                # DB must reflect v1
                defn_record = await defn_repo.get_definition(definition_id)
                self.assertEqual(defn_record.current_version, v1_label)

                # ---- 2. Cycle pin: run a task cycle and check cycle_run row ----
                # Use the seeded symbol so we don't require a specific catalog entry.
                raw_syms = ctx.e2e_settings.get("symbols")
                universe = (
                    [str(s).strip() for s in raw_syms if str(s).strip()]
                    if isinstance(raw_syms, list) and raw_syms
                    else ["600000.SH"]
                )
                from doyoutrade.runtime.cycle_task import merge_task_settings
                task = await service.create_task(
                    name="e2e-authoring-task-01",
                    mode="live",
                    data_provider="mock",
                    settings=merge_task_settings(
                        {
                            "model_route_name": ctx.model_route_name,
                            "universe": universe,
                            "strategy": {"definition_id": definition_id},
                        }
                    ),
                )
                ctx.created_task_ids.add(task.task_id)
                await service.start_task(task.task_id)
                executed = await service.tick_once(source="manual")
                self.assertGreaterEqual(executed, 1, "at least one cycle must execute")

                runs, total = await cycle_run_repo.list_for_task(task.task_id)
                self.assertGreater(total, 0, "cycle_run row must exist after tick")
                row = runs[0]

                self.assertEqual(
                    row.get("code_version"),
                    v1_label,
                    f"cycle_run.code_version must be {v1_label!r}, got {row.get('code_version')!r}",
                )
                self.assertEqual(
                    row.get("code_hash"),
                    v1_hash,
                    f"cycle_run.code_hash must match finalized hash",
                )

                await ctx.stop_agent_task(task.task_id)

        _run(_run_test())

    # ------------------------------------------------------------------
    # Scenario 3: mid-flight version bump
    # ------------------------------------------------------------------

    def test_mid_flight_version_bump_does_not_change_in_flight_cycle_run(self) -> None:
        """Finalizing v0002 while v0001 cycle_run exists must not change the
        existing record; a fresh cycle must pick up v0002."""

        async def _run_test() -> None:
            async with build_e2e_runtime(
                profile="isolated", model_mode=E2EModelMode.STUB
            ) as ctx:
                service = ctx.service
                storage = ctx.runtime["strategy_storage"]
                defn_repo = ctx.runtime["strategy_definition_repository"]
                cycle_run_repo = ctx.runtime["cycle_run_repository"]
                strategy_registry = ctx.runtime["strategy_registry_service"]

                definition_id = "sd-e2e-midflight-01"
                await strategy_registry.create_definition(
                    StrategyDefinitionCreate(
                        definition_id=definition_id,
                        name="E2E Mid-flight Strategy",
                        api_version="v1",
                        parameter_schema={},
                        default_parameters={},
                        capabilities={},
                        provenance={"source": "e2e-midflight"},
                    )
                )

                # Create and finalize v0001
                v1_label, v1_hash = await _write_draft_and_finalize(
                    storage, defn_repo, definition_id, "sess-mf-v1", _STRATEGY_V1
                )
                self.assertTrue(v1_label.startswith("v0001-"), v1_label)

                # Create instance + task and run first cycle (pinned to v0001)
                # Use the seeded symbol so we don't require a specific catalog entry.
                raw_syms = ctx.e2e_settings.get("symbols")
                universe = (
                    [str(s).strip() for s in raw_syms if str(s).strip()]
                    if isinstance(raw_syms, list) and raw_syms
                    else ["600000.SH"]
                )
                from doyoutrade.runtime.cycle_task import merge_task_settings
                task = await service.create_task(
                    name="e2e-midflight-task-01",
                    mode="live",
                    data_provider="mock",
                    settings=merge_task_settings(
                        {
                            "model_route_name": ctx.model_route_name,
                            "universe": universe,
                            "strategy": {"definition_id": definition_id},
                        }
                    ),
                )
                ctx.created_task_ids.add(task.task_id)
                await service.start_task(task.task_id)
                await service.tick_once(source="manual")

                # v1 cycle_run must exist
                runs_v1, total_v1 = await cycle_run_repo.list_for_task(task.task_id)
                self.assertGreater(total_v1, 0, "v0001 cycle_run must exist")
                v1_run = runs_v1[0]
                self.assertEqual(v1_run.get("code_version"), v1_label)
                v1_run_id = v1_run["run_id"]

                # ---- Simulate assistant mid-flight bump: finalize v0002 ----
                v2_label, v2_hash = await _write_draft_and_finalize(
                    storage, defn_repo, definition_id, "sess-mf-v2", _STRATEGY_V2
                )
                self.assertTrue(v2_label.startswith("v0002-"), v2_label)

                # v1 cycle_run record must be UNCHANGED
                v1_row_after_bump = await cycle_run_repo.get_by_run_id(v1_run_id)
                self.assertEqual(
                    v1_row_after_bump.get("code_version"),
                    v1_label,
                    "in-flight cycle_run.code_version must not change after v0002 bump",
                )

                # ---- Run a fresh cycle — must pick up v0002 ----
                await service.tick_once(source="manual")
                all_runs, total_all = await cycle_run_repo.list_for_task(task.task_id)
                self.assertGreater(total_all, 1, "second cycle_run must exist")

                new_runs = [r for r in all_runs if r["run_id"] != v1_run_id]
                self.assertTrue(new_runs, "second cycle_run must be a distinct row")
                self.assertEqual(
                    new_runs[0].get("code_version"),
                    v2_label,
                    f"new cycle must carry v0002 code_version, got {new_runs[0].get('code_version')!r}",
                )

                await ctx.stop_agent_task(task.task_id)

        _run(_run_test())

    # ------------------------------------------------------------------
    # Scenario 4: sandbox rejection
    # ------------------------------------------------------------------

    def test_write_file_rejects_path_outside_workspace(self) -> None:
        """write_file with a path-traversal attempt must return
        error_code="path_outside_workspace"."""

        async def _run_test() -> None:
            async with build_e2e_runtime(
                profile="isolated", model_mode=E2EModelMode.STUB
            ) as ctx:
                storage = ctx.runtime["strategy_storage"]
                from doyoutrade.tools import _sandbox
                from doyoutrade.tools.file_tools import WriteFileTool

                # Open a real draft and register it in the sandbox registry.
                definition_id = "sd-e2e-sandbox-01"
                session_id = "sess-e2e-sandbox-01"
                work_dir = storage.open_draft(
                    definition_id, session_id, base_version=None
                )
                _sandbox.register_sandbox(work_dir)
                try:
                    tool = WriteFileTool()
                    # Construct an absolute path that traverses outside work_dir.
                    escape_path = str(work_dir / ".." / ".." / ".." / "etc" / "passwd")
                    result = tool.execute(
                        file_path=escape_path,
                        content="boom",
                    )

                    self.assertEqual(result.get("status"), "error")
                    self.assertEqual(
                        result.get("error_code"),
                        "path_outside_workspace",
                        f"expected path_outside_workspace, got {result.get('error_code')!r}",
                    )
                finally:
                    _sandbox.unregister_sandbox(work_dir)

        _run(_run_test())


if __name__ == "__main__":
    unittest.main()
