from __future__ import annotations

import asyncio
import ast
import json
import os
from pathlib import Path
import unittest
from typing import Any, Callable
from unittest.mock import patch

import httpx
from click.testing import CliRunner
from fastapi.testclient import TestClient

from doyoutrade.api.app import create_app
from doyoutrade.cli._api import invoke_api
from doyoutrade.cli._envelope import EXIT_OK, Meta, parse_tool_result
from doyoutrade.cli.commands.backtest_runs import backtest as backtest_group
from doyoutrade.cli.commands.observability import cycle
from doyoutrade.cli.commands.strategy import strategy as strategy_group
from doyoutrade.cli.commands.task import task
from doyoutrade.tools import OperationHandler, OperationRegistry, ToolResult
from doyoutrade.tools._prose import append_json_payload


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_async_client(handler: Callable[[httpx.Request], httpx.Response]):
    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return patch("doyoutrade.cli._api.httpx.AsyncClient", factory)


class _ContextEchoTool(OperationHandler):
    name = "context_echo"
    description = "echo calling context"
    parameters = {
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "agent_id": {"type": "string"},
            "target_session_id": {"type": "string"},
        },
        "required": ["value"],
    }
    requires_calling_agent_id = True
    requires_calling_session_id = True

    async def execute(
        self,
        value: str,
        agent_id: str | None = None,
        target_session_id: str | None = None,
    ) -> ToolResult:  # type: ignore[override]
        return ToolResult(
            text=append_json_payload(
                "context echoed",
                {
                    "value": value,
                    "agent_id": agent_id,
                    "target_session_id": target_session_id,
                },
            )
        )


class _FakeAssistantService:
    def __init__(self) -> None:
        self.tool_registry = OperationRegistry([_ContextEchoTool()])

    def list_tools(self) -> list[dict[str, Any]]:
        return self.tool_registry.list_tools()


class _EmptyAssistantService:
    def __init__(self) -> None:
        self.tool_registry = OperationRegistry([])

    def list_tools(self) -> list[dict[str, Any]]:
        return []


class _TaskService:
    async def get_task_status(self, identifier: str) -> dict[str, Any]:
        return {"task_id": identifier, "status": "configured", "name": "demo"}


class ApiToolInvocationEndpointTests(unittest.TestCase):
    def test_tool_invocation_endpoint_executes_cli_tool_with_forwarded_context(self) -> None:
        with patch(
            "doyoutrade.api.cli_tools.build_cli_tool_registry",
            return_value=OperationRegistry([_ContextEchoTool()]),
        ):
            app = create_app(
                service=object(),
                approval_gate=None,
                assistant_service=_EmptyAssistantService(),
            )
        client = TestClient(app)

        response = client.post(
            "/assistant/tools/context_echo/execute",
            json={"args": {"value": "hello"}},
            headers={
                "X-DOYOUTRADE-Agent-Id": "asst-api",
                "X-DOYOUTRADE-Session-Id": "sess-api",
            },
        )

        self.assertEqual(response.status_code, 200, msg=response.text)
        body = response.json()
        self.assertEqual(body["tool_name"], "context_echo")
        self.assertFalse(body["is_error"])
        self.assertIn("context echoed", body["text"])
        data, _, _ = parse_tool_result(body["text"], is_error=False)
        self.assertEqual(data["agent_id"], "asst-api")
        self.assertEqual(data["target_session_id"], "sess-api")

    def test_tool_invocation_endpoint_does_not_expose_assistant_chat_registry(self) -> None:
        app = create_app(
            service=object(),
            approval_gate=None,
            assistant_service=_FakeAssistantService(),
        )
        client = TestClient(app)

        response = client.post(
            "/assistant/tools/context_echo/execute",
            json={"args": {"value": "hello"}},
        )

        self.assertEqual(response.status_code, 404, msg=response.text)

    def test_tool_invocation_endpoint_exposes_server_runtime_task_tools(self) -> None:
        app = create_app(
            service=_TaskService(),
            approval_gate=None,
            assistant_service=_EmptyAssistantService(),
        )
        client = TestClient(app)

        response = client.post(
            "/assistant/tools/get_task/execute",
            json={"args": {"identifier": "task-1"}},
        )

        self.assertEqual(response.status_code, 200, msg=response.text)
        body = response.json()
        self.assertEqual(body["tool_name"], "get_task")
        self.assertFalse(body["is_error"])
        data, _, _ = parse_tool_result(body["text"], is_error=False)
        self.assertEqual(data["task"]["task_id"], "task-1")


class CliApiClientHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_patch = patch.dict(
            os.environ,
            {
                "DOYOUTRADE_API_BASE_URL": "http://test.local/base",
                "TRACEPARENT": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
                "TRACESTATE": "vendor=value",
            },
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_invoke_api_forwards_agent_session_debug_run_and_trace_headers(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"status": "ok"})

        meta = Meta(
            agent_id="asst-cli",
            session_id="sess-cli",
            debug_session_id="debug-cli",
            run_id="run-cli",
        )
        with _patch_async_client(handler):
            envelope, exit_code = asyncio.run(
                invoke_api("POST", "/assistant/tools/context_echo/execute", json={}, meta=meta)
            )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(envelope["ok"])
        headers = captured[0].headers
        self.assertEqual(headers["x-doyoutrade-agent-id"], "asst-cli")
        self.assertEqual(headers["x-doyoutrade-session-id"], "sess-cli")
        self.assertEqual(headers["x-doyoutrade-debug-session-id"], "debug-cli")
        self.assertEqual(headers["x-doyoutrade-run-id"], "run-cli")
        self.assertEqual(headers["traceparent"], "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01")
        self.assertEqual(headers["tracestate"], "vendor=value")


class TaskCommandApiRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self._env_patch = patch.dict(
            os.environ,
            {"DOYOUTRADE_API_URL": "http://test.local"},
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_task_get_invokes_resource_endpoint_without_runtime_bootstrap(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"task_id": "task-1", "status": "configured"},
            )

        with _patch_async_client(handler):
            result = self.runner.invoke(
                task,
                ["get", "task-1"],
                obj={"fmt": "json"},
                catch_exceptions=False,
            )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["task_id"], "task-1")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].method, "GET")
        self.assertEqual(str(captured[0].url), "http://test.local/tasks/task-1")

    def test_task_start_posts_to_start_endpoint(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"task_id": "task-1", "status": "running"},
            )

        with _patch_async_client(handler):
            result = self.runner.invoke(
                task,
                ["start", "task-1"],
                obj={"fmt": "json"},
                catch_exceptions=False,
            )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(str(captured[0].url), "http://test.local/tasks/task-1/start")

    def test_task_start_rejects_strategy_definition_identifier_locally(self) -> None:
        result = self.runner.invoke(
            task,
            ["start", "sd-123"],
            obj={"fmt": "json"},
            catch_exceptions=False,
        )

        self.assertEqual(result.exit_code, 2, msg=result.output)
        envelope = json.loads(result.output)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "wrong_identifier_type")

    def test_task_pause_posts_to_pause_endpoint(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"task_id": "task-1", "status": "paused"},
            )

        with _patch_async_client(handler):
            result = self.runner.invoke(
                task,
                ["pause", "task-1"],
                obj={"fmt": "json"},
                catch_exceptions=False,
            )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(str(captured[0].url), "http://test.local/tasks/task-1/pause")

    def test_task_pause_rejects_strategy_definition_identifier_locally(self) -> None:
        result = self.runner.invoke(
            task,
            ["pause", "sd-123"],
            obj={"fmt": "json"},
            catch_exceptions=False,
        )

        self.assertEqual(result.exit_code, 2, msg=result.output)
        envelope = json.loads(result.output)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "wrong_identifier_type")

    def test_task_stop_posts_to_stop_endpoint(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"task_id": "task-1", "status": "stopped"},
            )

        with _patch_async_client(handler):
            result = self.runner.invoke(
                task,
                ["stop", "task-1"],
                obj={"fmt": "json"},
                catch_exceptions=False,
            )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(str(captured[0].url), "http://test.local/tasks/task-1/stop")

    def test_task_stop_rejects_strategy_definition_identifier_locally(self) -> None:
        result = self.runner.invoke(
            task,
            ["stop", "sd-123"],
            obj={"fmt": "json"},
            catch_exceptions=False,
        )

        self.assertEqual(result.exit_code, 2, msg=result.output)
        envelope = json.loads(result.output)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "wrong_identifier_type")

    def test_task_create_accepts_signal_only_mode(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"task_id": "task-1", "status": "configured"},
            )

        with _patch_async_client(handler):
            result = self.runner.invoke(
                task,
                [
                    "create",
                    "--name",
                    "demo",
                    "--mode",
                    "signal_only",
                    "--definition",
                    "sd-123",
                ],
                obj={"fmt": "json"},
                catch_exceptions=False,
            )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        payload = json.loads(captured[0].content.decode("utf-8"))
        self.assertEqual(payload["mode"], "signal_only")


class ObservabilityCommandApiRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self._env_patch = patch.dict(
            os.environ,
            {"DOYOUTRADE_API_URL": "http://test.local"},
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_cycle_get_invokes_resource_endpoint(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"run_id": "run-1", "status": "completed"},
            )

        with _patch_async_client(handler):
            result = self.runner.invoke(
                cycle,
                ["get", "run-1"],
                obj={"fmt": "json"},
                catch_exceptions=False,
            )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["run_id"], "run-1")
        self.assertEqual(captured[0].method, "GET")
        self.assertEqual(str(captured[0].url), "http://test.local/cycle-runs/run-1")


class BacktestCommandApiRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()

    def test_backtest_run_uses_tool_wait_timeout_for_http_call(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                201,
                json={"run_id": "btjob-1", "task_id": "task-1", "status": "completed"},
            )

        with patch.dict(os.environ, {"DOYOUTRADE_API_URL": "http://test.local"}, clear=False):
            with _patch_async_client(handler):
                result = self.runner.invoke(
                    backtest_group,
                    [
                        "run",
                        "--definition",
                        "sd-1",
                        "--range-start",
                        "2026-03-24",
                        "--range-end",
                        "2026-05-24",
                        "--universe",
                        "300058.SZ",
                        "--timeout",
                        "120",
                    ],
                    obj={"fmt": "json"},
                    catch_exceptions=False,
                )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertEqual(len(captured), 1)
        self.assertEqual(str(captured[0].url), "http://test.local/backtest-runs")
        self.assertEqual(json.loads(captured[0].content)["timeout_seconds"], 120.0)

    def test_backtest_run_posts_resource_endpoint_not_assistant_tool(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                201,
                json={
                    "status": "completed",
                    "run_id": "btjob-1",
                    "task_id": "task-1",
                    "summary": {"open_positions": 0},
                },
            )

        with patch.dict(os.environ, {"DOYOUTRADE_API_URL": "http://test.local"}, clear=False):
            with _patch_async_client(handler):
                result = self.runner.invoke(
                    backtest_group,
                    [
                        "run",
                        "--definition",
                        "sd-1",
                        "--range-start",
                        "2026-03-24",
                        "--range-end",
                        "2026-05-24",
                        "--universe",
                        "300058.SZ",
                        "--timeout",
                        "120",
                    ],
                    obj={"fmt": "json"},
                    catch_exceptions=False,
                )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(str(captured[0].url), "http://test.local/backtest-runs")
        self.assertNotIn("/assistant/tools/", str(captured[0].url))
        self.assertEqual(
            json.loads(captured[0].content),
            {
                "definition_id": "sd-1",
                "range_start": "2026-03-24",
                "range_end": "2026-05-24",
                "universe": ["300058.SZ"],
                "timeout_seconds": 120.0,
                "debug_enabled": True,
            },
        )
        envelope = json.loads(result.output)
        self.assertEqual(envelope["data"]["run_id"], "btjob-1")


class StrategyCommandOpenApiRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self._env_patch = patch.dict(
            os.environ,
            {"DOYOUTRADE_API_URL": "http://test.local"},
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_strategy_bind_puts_task_resource_endpoint_not_assistant_tool(self) -> None:
        # StrategyInstance / ``si-`` bindings were removed; ``strategy bind``
        # writes settings.strategy.definition_id on the task via PUT /tasks.
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "task_id": "task-1",
                    "settings": {"strategy": {"definition_id": "sd-1"}},
                },
            )

        with _patch_async_client(handler):
            result = self.runner.invoke(
                strategy_group,
                [
                    "bind",
                    "task-1",
                    "sd-1",
                ],
                obj={"fmt": "json"},
                catch_exceptions=False,
            )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].method, "PUT")
        self.assertEqual(str(captured[0].url), "http://test.local/tasks/task-1")
        self.assertNotIn("/assistant/tools/", str(captured[0].url))
        self.assertEqual(
            json.loads(captured[0].content),
            {"settings": {"strategy": {"definition_id": "sd-1"}}},
        )
        envelope = json.loads(result.output)
        self.assertEqual(envelope["data"]["task_id"], "task-1")


class CliRuntimeBoundaryTests(unittest.TestCase):
    def test_cli_commands_do_not_import_runtime_bootstrap_or_repositories(self) -> None:
        root = Path(__file__).resolve().parent.parent
        checked = [
            root / "doyoutrade" / "cli" / "main.py",
            *sorted((root / "doyoutrade" / "cli" / "commands").glob("*.py")),
        ]
        forbidden_prefixes = (
            "doyoutrade.bootstrap",
            "doyoutrade.persistence.repositories",
            "doyoutrade.cli._runtime",
        )
        offenders: list[str] = []
        for path in checked:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(forbidden_prefixes):
                            offenders.append(f"{path.relative_to(root)} imports {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith(forbidden_prefixes):
                        offenders.append(f"{path.relative_to(root)} imports from {node.module}")

        self.assertEqual(offenders, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
