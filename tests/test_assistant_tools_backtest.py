import json
import re
import unittest

from doyoutrade.tools import OperationRegistry, OperationHandler
from doyoutrade.assistant.strategy_tools.run_tools import RunStrategyBacktestTool

from tests._tool_result_helpers import payload as _payload


def _parse_payload(result) -> dict:
    """Extract the embedded ```json``` block from a single-channel ToolResult.text."""

    text = result if isinstance(result, str) else result.text
    match = re.search(r"```json\n(.*?)\n```", text, re.DOTALL)
    if match is None:
        return {}
    return json.loads(match.group(1))


class _PlatformServiceStub:
    """Fresh-run happy path: start succeeds; get cycles through running→completed."""

    def __init__(self) -> None:
        self.start_calls: list[dict] = []
        self.get_calls: list[tuple[str, str]] = []
        self._statuses = iter(
            [
                {"run_id": "btjob-1", "task_id": "task-1", "status": "running"},
                {
                    "run_id": "btjob-1",
                    "task_id": "task-1",
                    "status": "completed",
                    "summary": {"return_pct": "0.12"},
                },
            ]
        )

    async def start_backtest_job(self, task_id: str, **kwargs):
        self.start_calls.append({"task_id": task_id, **kwargs})
        return {"run_id": "btjob-1", "task_id": task_id, "status": "running"}

    async def get_backtest_job(self, task_id: str, run_id: str):
        self.get_calls.append((task_id, run_id))
        return next(self._statuses)


class _ExistingRunServiceStub:
    """start_backtest_job raises 'already has a run'."""

    def __init__(
        self,
        *,
        list_runs: list[dict] | None = None,
        get_responses: list[dict] | None = None,
        clone_task_id: str = "task-1-copy",
    ) -> None:
        self._list_runs = list_runs
        self._get_responses = iter(get_responses) if get_responses else None
        self._clone_task_id = clone_task_id
        self.clone_calls: list[str] = []

    async def start_backtest_job(self, task_id: str, **kwargs):
        raise ValueError("backtest task already has a run")

    async def get_backtest_job(self, task_id: str, run_id: str):
        if self._get_responses is None:
            return {"run_id": run_id, "task_id": task_id, "status": "running"}
        return next(self._get_responses)

    async def list_backtest_jobs(self, task_id: str, *, limit: int = 50, offset: int = 0):
        if self._list_runs is None:
            raise AttributeError  # simulate platform that cannot list
        return {"items": list(self._list_runs)[:limit], "total": len(self._list_runs)}

    async def clone_task(self, source_identifier: str, **kwargs):
        self.clone_calls.append(source_identifier)

        class _Cloned:
            def __init__(self, task_id: str) -> None:
                self.task_id = task_id

        return _Cloned(self._clone_task_id)


class _SilentFailureServiceStub:
    """Raises an exception with empty ``str(exc)`` to lock the empty-error regression."""

    async def start_backtest_job(self, task_id: str, **kwargs):
        raise RuntimeError()  # noqa: TRY003 — empty message is the test condition

    async def get_backtest_job(self, task_id: str, run_id: str):
        return {"run_id": run_id, "task_id": task_id, "status": "running"}


class _SlowRunningStub:
    """Fresh run that stays in ``running`` indefinitely — used to force a timeout."""

    async def start_backtest_job(self, task_id: str, **kwargs):
        return {"run_id": "btjob-zzz", "task_id": task_id, "status": "running"}

    async def get_backtest_job(self, task_id: str, run_id: str):
        return {"run_id": run_id, "task_id": task_id, "status": "running"}


class _InstanceModeStub:
    """Instance mode: ``create_task`` returns a fresh task, then ``start_backtest_job``
    succeeds and ``get_backtest_job`` completes immediately."""

    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.start_calls: list[dict] = []
        self.get_calls: list[tuple[str, str]] = []

    async def create_task(self, **kwargs):
        self.create_calls.append(kwargs)

        class _Task:
            task_id = "task-auto-1"

        return _Task()

    async def start_backtest_job(self, task_id: str, **kwargs):
        self.start_calls.append({"task_id": task_id, **kwargs})
        return {"run_id": "btjob-auto", "task_id": task_id, "status": "running"}

    async def get_backtest_job(self, task_id: str, run_id: str):
        self.get_calls.append((task_id, run_id))
        return {
            "run_id": run_id,
            "task_id": task_id,
            "status": "completed",
            "summary": {"return_pct": "0.05"},
        }


class _CreateTaskFailingStub:
    async def create_task(self, **kwargs):
        raise ValueError("strategy definition not found: sd-missing")

    async def start_backtest_job(self, *a, **k):
        raise RuntimeError("create_task failed before this should run")


class RunStrategyBacktestToolTaskModeTests(unittest.IsolatedAsyncioTestCase):
    """Task-mode contract — pre-existing behavior absorbed from the retired
    ``BacktestTool``. Default ``timeout_seconds`` waits for completion."""

    async def test_execute_waits_for_terminal_backtest_result(self) -> None:
        service = _PlatformServiceStub()
        tool = RunStrategyBacktestTool(service)

        tool_result = await tool.execute(
                task_id="task-1",
                range_start="2026-01-01",
                range_end="2026-01-10",
            )
        result = _parse_payload(tool_result)

        self.assertFalse(tool_result.is_error)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["backtest_job"]["status"], "completed")
        self.assertEqual(result["backtest_job"]["run_id"], "btjob-1")
        # KPIs (return_pct/equity/etc.) live in the report markdown only — the
        # envelope's ``backtest_job`` is intentionally narrow so the bash-tool
        # truncation can't bury ``report_path``.
        self.assertNotIn("summary", result["backtest_job"])
        self.assertNotIn("return_pct", result["backtest_job"])
        self.assertNotIn(
            "attached_to_existing_run",
            result,
            "fresh runs must not carry the attach marker",
        )
        self.assertEqual(
            service.start_calls,
            [
                {
                    "task_id": "task-1",
                    "range_start": "2026-01-01",
                    "range_end": "2026-01-10",
                    "bar_interval": None,
                    "market_profile": None,
                    "config_overrides": None,
                    "model_route_name": None,
                    "debug_enabled": True,
                }
            ],
        )
        self.assertEqual(
            service.get_calls,
            [("task-1", "btjob-1"), ("task-1", "btjob-1")],
        )

    async def test_fire_and_forget_returns_running_run(self) -> None:
        """``timeout_seconds=0`` skips polling and returns the just-queued row."""

        service = _PlatformServiceStub()
        tool = RunStrategyBacktestTool(service)

        result = _parse_payload(await tool.execute(
                task_id="task-1",
                range_start="2026-01-01",
                range_end="2026-01-10",
                timeout_seconds=0,
            ))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["backtest_job"]["status"], "running")
        self.assertEqual(result["backtest_job"]["run_id"], "btjob-1")
        self.assertIn("next_steps", result)
        # No polling happened — get_backtest_job should never have been called.
        self.assertEqual(service.get_calls, [])

    async def test_existing_completed_run_returns_ok_attached(self) -> None:
        service = _ExistingRunServiceStub(
            list_runs=[
                {"run_id": "btjob-aaa", "task_id": "task-1", "status": "completed"},
            ],
            get_responses=[
                {
                    "run_id": "btjob-aaa",
                    "task_id": "task-1",
                    "status": "completed",
                    "summary": {"return_pct": "0.08"},
                },
            ],
        )
        tool = RunStrategyBacktestTool(service)

        result = _parse_payload(await tool.execute(
                task_id="task-1",
                range_start="2026-01-01",
                range_end="2026-01-10",
            ))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["attached_to_existing_run"])
        self.assertEqual(result["backtest_job"]["status"], "completed")
        self.assertEqual(service.clone_calls, [])
        self.assertNotIn("cloned_task_id", result)

    async def test_does_not_autoclone_even_when_clone_task_is_available(self) -> None:
        """Regression for the death-loop in ``tmp/error_request.json``."""

        service = _ExistingRunServiceStub(list_runs=None)
        tool = RunStrategyBacktestTool(service)

        tool_result = await tool.execute(
                task_id="task-1",
                range_start="2026-01-01",
                range_end="2026-01-10",
            )
        result = _parse_payload(tool_result)

        self.assertTrue(tool_result.is_error)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "backtest_run_already_exists")
        self.assertNotIn("cloned_task_id", result)
        self.assertEqual(service.clone_calls, [])
        hints = " ".join(result["repair_hints"])
        self.assertIn("inspect", hints.lower())
        self.assertIn("clone_task", hints)

    async def test_attaches_to_running_existing_run_and_polls_to_terminal(self) -> None:
        service = _ExistingRunServiceStub(
            list_runs=[
                {"run_id": "btjob-bbb", "task_id": "task-1", "status": "running"},
            ],
            get_responses=[
                {"run_id": "btjob-bbb", "task_id": "task-1", "status": "running"},
                {
                    "run_id": "btjob-bbb",
                    "task_id": "task-1",
                    "status": "completed",
                    "summary": {"return_pct": "0.05"},
                },
            ],
        )
        tool = RunStrategyBacktestTool(service)

        result = _parse_payload(await tool.execute(
                task_id="task-1",
                range_start="2026-01-01",
                range_end="2026-01-10",
                poll_interval_seconds=0,
                timeout_seconds=5,
            ))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["attached_to_existing_run"])
        self.assertEqual(result["backtest_job"]["run_id"], "btjob-bbb")
        self.assertEqual(result["backtest_job"]["status"], "completed")
        self.assertEqual(service.clone_calls, [])

    async def test_attached_running_run_with_zero_timeout_returns_ok_immediately(self) -> None:
        """``timeout_seconds=0`` against a running existing run attaches without polling."""

        service = _ExistingRunServiceStub(
            list_runs=[
                {"run_id": "btjob-ccc", "task_id": "task-1", "status": "running"},
            ],
        )
        tool = RunStrategyBacktestTool(service)

        result = _parse_payload(await tool.execute(
                task_id="task-1",
                range_start="2026-01-01",
                range_end="2026-01-10",
                timeout_seconds=0,
                poll_interval_seconds=0,
            ))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["attached_to_existing_run"])
        self.assertEqual(result["backtest_job"]["status"], "running")
        self.assertEqual(service.clone_calls, [])

    async def test_failed_existing_run_returns_backtest_run_failed(self) -> None:
        service = _ExistingRunServiceStub(
            list_runs=[
                {
                    "run_id": "btjob-ddd",
                    "task_id": "task-1",
                    "status": "failed",
                    "error_message": "name 'locals' is not defined",
                },
            ],
            get_responses=[
                {
                    "run_id": "btjob-ddd",
                    "task_id": "task-1",
                    "status": "failed",
                    "error_message": "name 'locals' is not defined",
                },
            ],
        )
        tool = RunStrategyBacktestTool(service)

        tool_result = await tool.execute(
                task_id="task-1",
                range_start="2026-01-01",
                range_end="2026-01-10",
            )
        result = _parse_payload(tool_result)

        self.assertTrue(tool_result.is_error)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "backtest_run_failed")
        self.assertEqual(result["error_type"], "BacktestRunFailed")
        self.assertIn("locals", result["error"])
        self.assertTrue(result["attached_to_existing_run"])
        self.assertEqual(result["existing_run_id"], "btjob-ddd")
        self.assertEqual(result["existing_run_status"], "failed")
        self.assertEqual(service.clone_calls, [])
        self.assertNotIn("cloned_task_id", result)
        self.assertEqual(result["existing_run_ids"], ["btjob-ddd"])
        hints = " ".join(result["repair_hints"]).lower()
        self.assertIn("inspect", hints)
        self.assertIn("clone_task", " ".join(result["repair_hints"]))

    async def test_fresh_run_timeout_includes_resumable_hints(self) -> None:
        tool = RunStrategyBacktestTool(_SlowRunningStub())

        tool_result = await tool.execute(
                task_id="task-1",
                range_start="2026-01-01",
                range_end="2026-01-10",
                timeout_seconds=0.01,
                poll_interval_seconds=0,
            )
        result = _parse_payload(tool_result)

        self.assertTrue(tool_result.is_error)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "backtest_wait_timeout")
        self.assertNotIn("attached_to_existing_run", result)
        hints = " ".join(result["repair_hints"]).lower()
        self.assertIn("resume waiting", hints)
        # The repair hint must reference the merged tool name.
        self.assertIn("run_strategy_backtest", " ".join(result["repair_hints"]))

    async def test_execute_never_returns_empty_error_string(self) -> None:
        service = _SilentFailureServiceStub()
        tool = RunStrategyBacktestTool(service)

        tool_result = await tool.execute(
                task_id="task-1",
                range_start="2026-01-01",
                range_end="2026-01-10",
            )

        self.assertTrue(tool_result.is_error)
        self.assertIn("[error:backtest_start_failed]", tool_result.text)
        # The error prose must always carry the exception class to keep
        # the empty-message regression from regressing again.
        self.assertIn("RuntimeError", tool_result.text)

    async def test_execute_rejects_strategy_definition_id_as_task_id(self) -> None:
        tool = RunStrategyBacktestTool(_PlatformServiceStub())

        tool_result = await tool.execute(
                task_id="sd-deadbeef",
                range_start="2026-01-01",
                range_end="2026-01-10",
            )

        self.assertTrue(tool_result.is_error)
        self.assertIn("[error:wrong_identifier_type]", tool_result.text)
        self.assertIn("strategy definition id", tool_result.text)

    async def test_execute_missing_range_returns_structured_validation_error(self) -> None:
        tool = RunStrategyBacktestTool(_PlatformServiceStub())

        tool_result = await tool.execute(task_id="task-1")

        self.assertTrue(tool_result.is_error)
        self.assertIn("[error:backtest_validation_error]", tool_result.text)
        self.assertIn("range_start", tool_result.text)
        self.assertIn("range_end", tool_result.text)
        # Hint prose should mention recovery options
        self.assertIn("Hint:", tool_result.text)

    async def test_execute_blank_range_returns_structured_validation_error(self) -> None:
        tool = RunStrategyBacktestTool(_PlatformServiceStub())

        tool_result = await tool.execute(task_id="task-1", range_start=" ", range_end="")

        self.assertTrue(tool_result.is_error)
        self.assertIn("[error:backtest_validation_error]", tool_result.text)
        self.assertIn("range_start", tool_result.text)
        self.assertIn("range_end", tool_result.text)


class RunStrategyBacktestToolDefinitionModeTests(unittest.IsolatedAsyncioTestCase):
    """Definition-mode contract — auto-create a backtest task from a
    definition (``sd-...``), then start + wait for the run in a single call.
    StrategyInstance / ``si-`` entry modes were removed."""

    async def test_definition_mode_auto_creates_task_and_waits(self) -> None:
        service = _InstanceModeStub()
        tool = RunStrategyBacktestTool(service)

        result = _parse_payload(await tool.execute(
                definition_id="sd-demo",
                universe=["600522.SH"],
                range_start="2026-05-08",
                range_end="2026-05-15",
            ))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["backtest_job"]["status"], "completed")
        self.assertEqual(result["auto_created_task_id"], "task-auto-1")
        self.assertEqual(len(service.create_calls), 1)
        created = service.create_calls[0]
        self.assertEqual(created["mode"], "backtest")
        self.assertEqual(created["data_provider"], "auto")
        self.assertEqual(
            created["settings"],
            {"strategy": {"definition_id": "sd-demo"}, "universe": ["600522.SH"]},
        )
        self.assertEqual(created["name"], "sd-demo@2026-05-08-2026-05-15")
        self.assertEqual(service.start_calls[0]["task_id"], "task-auto-1")

    async def test_definition_mode_honors_custom_name_and_data_provider(self) -> None:
        service = _InstanceModeStub()
        tool = RunStrategyBacktestTool(service)

        await tool.execute(
            definition_id="sd-demo",
            universe=["600522.SH"],
            range_start="2026-05-08",
            range_end="2026-05-15",
            name="custom task name",
            data_provider="qmt",
        )

        created = service.create_calls[0]
        self.assertEqual(created["name"], "custom task name")
        self.assertEqual(created["data_provider"], "qmt")

    async def test_definition_mode_requires_universe(self) -> None:
        service = _InstanceModeStub()
        tool = RunStrategyBacktestTool(service)

        tool_result = await tool.execute(
                definition_id="sd-demo",
                range_start="2026-05-08",
                range_end="2026-05-15",
            )

        self.assertTrue(tool_result.is_error)
        self.assertIn("[error:missing_universe_for_auto_create_mode]", tool_result.text)
        self.assertEqual(service.create_calls, [])

    async def test_missing_both_ids_returns_structured_error(self) -> None:
        tool = RunStrategyBacktestTool(_PlatformServiceStub())

        tool_result = await tool.execute(
                range_start="2026-05-08",
                range_end="2026-05-15",
            )

        self.assertTrue(tool_result.is_error)
        self.assertIn("[error:missing_task_or_definition_id]", tool_result.text)

    async def test_both_ids_returns_conflict_error(self) -> None:
        tool = RunStrategyBacktestTool(_PlatformServiceStub())

        tool_result = await tool.execute(
                task_id="task-1",
                definition_id="sd-demo",
                range_start="2026-05-08",
                range_end="2026-05-15",
            )

        self.assertTrue(tool_result.is_error)
        self.assertIn("[error:conflicting_backtest_entry_mode]", tool_result.text)

    async def test_definition_mode_create_task_failure_surfaces_structured_error(self) -> None:
        tool = RunStrategyBacktestTool(_CreateTaskFailingStub())

        tool_result = await tool.execute(
                definition_id="sd-missing",
                universe=["600522.SH"],
                range_start="2026-05-08",
                range_end="2026-05-15",
            )

        self.assertTrue(tool_result.is_error)
        self.assertIn("[error:auto_create_task_failed]", tool_result.text)

    async def test_definition_mode_rejects_task_id_shape_on_definition_field(self) -> None:
        tool = RunStrategyBacktestTool(_PlatformServiceStub())

        tool_result = await tool.execute(
                definition_id="not-an-sd-id",
                universe=["600522.SH"],
                range_start="2026-05-08",
                range_end="2026-05-15",
            )

        self.assertTrue(tool_result.is_error)
        self.assertIn("[error:wrong_identifier_type]", tool_result.text)


class _RaisingTool(OperationHandler):
    name = "raising_tool"
    description = "Test fixture that always raises an empty-message exception."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):  # type: ignore[override]
        raise RuntimeError()  # empty message is the test condition


class OperationRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_fallback_attaches_error_type_and_message(self) -> None:
        registry = OperationRegistry([_RaisingTool()])
        result = await registry.execute("raising_tool", {})
        # Single-channel prose: prefix carries the error_type token, body
        # carries the empty-message fallback + tool name + traceback tail.
        self.assertTrue(getattr(result, "is_error", False))
        self.assertIn("[error:RuntimeError]", result)
        self.assertIn("RuntimeError (no message)", result)
        self.assertIn("Tool: raising_tool", result)
        self.assertIn("Traceback: RuntimeError", result)


if __name__ == "__main__":
    unittest.main()
