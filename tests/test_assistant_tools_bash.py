from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from unittest.mock import patch

from doyoutrade.tools import OperationRegistry
from doyoutrade.tools import bash as bash_module
from doyoutrade.tools.bash import (
    BashPolicyEngine,
    BashTaskManager,
    ExecuteBashTool,
    ManageBashTasksTool,
)

from tests._tool_result_helpers import payload as _payload


_TASK_ID_RE = re.compile(r"task_id=([\w-]+)")


def _extract_task_id(text: str) -> str:
    """Pull ``task_id=...`` out of the background-start prose.

    ``ExecuteBashTool`` no longer attaches a JSON dump to the model-facing
    text — the structured row lives on the ``.started`` debug event. Tests
    still need the id to drive ``manage_bash_tasks(action='get', ...)``,
    so they parse it from the prose header just like the agent would.
    """

    match = _TASK_ID_RE.search(text)
    assert match is not None, f"no task_id in result text: {text!r}"
    return match.group(1)


class _DebugEventCapture:
    """Async context manager that records ``emit_debug_event`` calls.

    Patches the symbol re-imported into ``doyoutrade.tools.bash`` (the one
    actually called from inside ``ExecuteBashTool``) rather than the
    source in ``doyoutrade.debug.context``. This keeps the test isolated
    from the global OTel tracer provider — no span / exporter setup
    needed, and no cross-test bleed.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def _record(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, dict(payload)))

    def __enter__(self) -> "_DebugEventCapture":
        self._patcher = patch.object(bash_module, "emit_debug_event", self._record)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._patcher.stop()

    def by_name(self, name: str) -> list[dict]:
        return [payload for event, payload in self.events if event == name]


class BashPolicyEngineTests(unittest.TestCase):
    def test_policy_allows_safe_read_command(self) -> None:
        decision = BashPolicyEngine().decide("pwd")
        self.assertEqual(decision.kind, "allow")

    def test_policy_requires_approval_for_destructive_prefix(self) -> None:
        decision = BashPolicyEngine().decide("rm -rf build")
        self.assertEqual(decision.kind, "ask")

    def test_policy_denies_root_delete(self) -> None:
        decision = BashPolicyEngine().decide("rm -rf /")
        self.assertEqual(decision.kind, "deny")


class BashToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.manager = BashTaskManager(base_dir=self.tmpdir.name)
        self.execute_tool = ExecuteBashTool(task_manager=self.manager)
        self.manage_tool = ManageBashTasksTool(task_manager=self.manager)

    async def asyncTearDown(self) -> None:
        await self.manager.aclose()

    async def test_execute_bash_tool_metadata(self) -> None:
        self.assertEqual(self.execute_tool.name, "execute_bash")
        self.assertEqual(self.execute_tool.category, "system")
        self.assertIn("command", self.execute_tool.parameters["properties"])
        self.assertIn("background", self.execute_tool.parameters["properties"])

    async def test_execute_bash_foreground_returns_raw_stdout(self) -> None:
        with _DebugEventCapture() as cap:
            result = await self.execute_tool.execute(command="printf 'hello'")

        self.assertFalse(result.is_error)
        # No "Bash command finished (status=...)" preamble, no
        # "--- stdout ---" divider — just the command's actual output.
        self.assertEqual(result.text, "hello")

        completed = cap.by_name("operation_execute_bash.completed")
        self.assertEqual(len(completed), 1)
        payload = completed[0]
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["timed_out"], False)
        self.assertEqual(payload["stdout_length"], len("hello"))
        self.assertEqual(payload["stderr_length"], 0)
        self.assertIsNone(payload["return_code_interpretation"])
        self.assertFalse(payload["no_output_expected"])

    async def test_execute_bash_grep_no_match_is_not_an_error(self) -> None:
        # grep returning 1 (no matches) is a normal outcome, not a failure.
        # Before the semantic interpretation fix, this would have set
        # ``is_error=True`` and led the assistant to retry/escalate.
        with _DebugEventCapture() as cap:
            result = await self.execute_tool.execute(
                command="printf 'foo\\nbar\\n' | grep zzz"
            )

        self.assertFalse(result.is_error, msg=f"result.text={result.text!r}")
        # No "Exit code N" appended because we don't treat it as an error.
        self.assertNotIn("Exit code", result.text)

        completed = cap.by_name("operation_execute_bash.completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["exit_code"], 1)
        self.assertEqual(
            completed[0]["return_code_interpretation"], "No matches found"
        )

    async def test_execute_bash_silent_command_marks_no_output(self) -> None:
        # mkdir prints nothing on success — model gets "(no output)" so an
        # empty result doesn't look like a swallowed response, and the
        # debug event carries ``no_output_expected=True`` for the UI.
        with tempfile.TemporaryDirectory() as parent:
            with _DebugEventCapture() as cap:
                result = await self.execute_tool.execute(
                    command=f"mkdir -p {parent}/created",
                )

        self.assertFalse(result.is_error)
        self.assertEqual(result.text, "(no output)")

        completed = cap.by_name("operation_execute_bash.completed")
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0]["no_output_expected"])

    async def test_execute_bash_nonzero_exit_marks_error_with_code(self) -> None:
        with _DebugEventCapture() as cap:
            result = await self.execute_tool.execute(
                command="bash -c 'echo oops 1>&2; exit 7'",
            )

        self.assertTrue(result.is_error)
        self.assertIn("Exit code 7", result.text)
        self.assertIn("oops", result.text)

        failed = cap.by_name("operation_execute_bash.failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["exit_code"], 7)

    async def test_execute_bash_denied_command_returns_error_and_emits_rejected(self) -> None:
        with _DebugEventCapture() as cap:
            result = await self.execute_tool.execute(command="rm -rf /")

        self.assertTrue(result.is_error)
        self.assertIn("[error:bash_policy_deny]", result.text)
        self.assertIn("denied", result.text.lower())

        rejected = cap.by_name("operation_execute_bash.rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["policy"], "deny")

    async def test_execute_bash_ask_without_approval_emits_rejected(self) -> None:
        with _DebugEventCapture() as cap:
            result = await self.execute_tool.execute(command="rm -rf build")

        self.assertTrue(result.is_error)
        self.assertIn("[error:bash_approval_required]", result.text)

        rejected = cap.by_name("operation_execute_bash.rejected")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["policy"], "ask")
        self.assertEqual(rejected[0]["approval_granted"], False)

    async def test_execute_bash_background_task_can_be_polled(self) -> None:
        with _DebugEventCapture() as cap:
            started = await self.execute_tool.execute(
                command="echo start; sleep 0.2; echo done",
                background=True,
                session_id="session-a",
            )
        self.assertFalse(started.is_error)
        self.assertIn("started in background", started.text)
        task_id = _extract_task_id(started.text)

        started_events = cap.by_name("operation_execute_bash.started")
        self.assertEqual(len(started_events), 1)
        self.assertEqual(started_events[0]["task_id"], task_id)
        self.assertTrue(started_events[0]["output_path"])

        await asyncio.sleep(0.35)

        detail = _payload(
            await self.manage_tool.execute(
                action="get",
                task_id=task_id,
                session_id="session-a",
            )
        )
        self.assertEqual(detail["status"], "ok")
        self.assertEqual(detail["task"]["status"], "completed")
        self.assertIn("done", detail["task"]["output_preview"])

    async def test_stop_bash_task_terminates_running_process(self) -> None:
        started = await self.execute_tool.execute(
            command="sleep 5",
            background=True,
            session_id="session-stop",
        )
        task_id = _extract_task_id(started.text)

        stopped = _payload(
            await self.manage_tool.execute(
                action="stop",
                task_id=task_id,
                session_id="session-stop",
            )
        )
        self.assertEqual(stopped["status"], "ok")
        self.assertIn(stopped["task"]["status"], {"stopping", "killed", "failed"})

        await asyncio.sleep(0.1)
        detail = _payload(
            await self.manage_tool.execute(
                action="get",
                task_id=task_id,
                session_id="session-stop",
            )
        )
        self.assertIn(detail["task"]["status"], {"killed", "failed", "completed"})

    async def test_registry_injects_session_id_for_session_aware_tools(self) -> None:
        registry = OperationRegistry(
            [
                ExecuteBashTool(task_manager=self.manager),
                ManageBashTasksTool(task_manager=self.manager),
            ]
        )

        started = await registry.execute(
            "execute_bash",
            {"command": "sleep 0.2", "background": True},
            session_id="session-registry",
        )
        # Registry returns a tagged ``str`` subclass; ``.text`` access works
        # via attribute fallthrough in the registry, but ``_extract_task_id``
        # accepts either string or ToolResult-like via ``str`` conversion.
        started_text = started if isinstance(started, str) else started.text
        _extract_task_id(started_text)

        listed = _payload(
            await registry.execute(
                "manage_bash_tasks",
                {"action": "list"},
                session_id="session-registry",
            )
        )
        self.assertEqual(listed["status"], "ok")
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(listed["items"][0]["session_id"], "session-registry")

    async def test_registry_aclose_stops_background_tasks(self) -> None:
        registry = OperationRegistry(
            [
                ExecuteBashTool(task_manager=self.manager),
                ManageBashTasksTool(task_manager=self.manager),
            ]
        )
        started = await registry.execute(
            "execute_bash",
            {"command": "sleep 5", "background": True},
            session_id="session-registry",
        )
        started_text = started if isinstance(started, str) else started.text
        task_id = _extract_task_id(started_text)

        await registry.aclose()

        detail = _payload(
            await self.manage_tool.execute(
                action="get",
                task_id=task_id,
                session_id="session-registry",
            )
        )
        self.assertIn(detail["task"]["status"], {"failed", "completed", "stopping"})
