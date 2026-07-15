from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from typing import Any, Callable
from unittest.mock import patch

import httpx
from click.testing import CliRunner

from doyoutrade.cli.commands.assistant import assistant


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _patch_async_client(handler: Callable[[httpx.Request], httpx.Response]):
    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return patch("doyoutrade.cli._api.httpx.AsyncClient", factory)


class AssistantCliCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = CliRunner()
        self._env_patch = patch.dict(os.environ, {"DOYOUTRADE_API_URL": "http://test.local"}, clear=False)
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def _run(self, args: list[str], handler: Callable[[httpx.Request], httpx.Response] | None = None):
        if handler is None:
            return self.runner.invoke(assistant, args, obj={"fmt": "json"}, catch_exceptions=False)
        with _patch_async_client(handler):
            return self.runner.invoke(assistant, args, obj={"fmt": "json"}, catch_exceptions=False)

    def test_session_create_posts_agent_id_and_title(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                201,
                json={
                    "session_id": "asst-1",
                    "agent_id": "agent-1",
                    "title": "Validation",
                    "status": "idle",
                },
            )

        result = self._run(
            ["session", "create", "--agent-id", "agent-1", "--title", "Validation"],
            handler,
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(captured[0].url.path, "/assistant/sessions")
        self.assertEqual(json.loads(captured[0].content), {"agent_id": "agent-1", "title": "Validation"})
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["session_id"], "asst-1")

    def test_chat_posts_message_text(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "session": {"session_id": "asst-1"},
                    "messages": [{"role": "user", "content": "hello"}],
                    "trace_id": "trace-1",
                },
            )

        result = self._run(["chat", "--session-id", "asst-1", "--message", "hello"], handler)

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(captured[0].url.path, "/assistant/sessions/asst-1/messages")
        self.assertEqual(json.loads(captured[0].content), {"content": "hello"})
        self.assertEqual(json.loads(result.output)["data"]["trace_id"], "trace-1")

    def test_chat_reads_message_file(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"session": {"session_id": "asst-1"}, "messages": []})

        with self.runner.isolated_filesystem():
            Path("message.txt").write_text("from file", encoding="utf-8")
            result = self._run(["chat", "--session-id", "asst-1", "--message-file", "message.txt"], handler)

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(json.loads(captured[0].content), {"content": "from file"})

    def test_chat_rejects_missing_message_source(self) -> None:
        result = self._run(["chat", "--session-id", "asst-1"])

        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "missing_message")

    def test_chat_rejects_conflicting_message_sources(self) -> None:
        with self.runner.isolated_filesystem():
            Path("message.txt").write_text("from file", encoding="utf-8")
            result = self._run(
                [
                    "chat",
                    "--session-id",
                    "asst-1",
                    "--message",
                    "inline",
                    "--message-file",
                    "message.txt",
                ]
            )

        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "conflicting_message_args")

    def test_chat_reports_message_file_read_failed(self) -> None:
        with self.runner.isolated_filesystem():
            result = self._run(["chat", "--session-id", "asst-1", "--message-file", "missing.txt"])

        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "message_file_read_failed")

    def test_chat_reports_message_file_decode_failed(self) -> None:
        with self.runner.isolated_filesystem():
            Path("bad.txt").write_bytes(b"\xff\xfe\xfa")
            result = self._run(["chat", "--session-id", "asst-1", "--message-file", "bad.txt"])

        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "message_file_read_failed")
        self.assertEqual(payload["error"]["error_type"], "UnicodeDecodeError")

    def test_export_writes_output_file_and_trims_stdout_payload(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "format": "markdown",
                    "ids": {"session_id": "asst-1", "run_ids": ["asst-run-1"], "trace_ids": ["trace-1"]},
                    "counts": {"messages": 2, "events": 3, "traces": 1, "spans": 1, "model_invocations": 1},
                    "export_text": "# Assistant Session Export\nbody\n",
                    "messages": [{"role": "user"}],
                },
            )

        with self.runner.isolated_filesystem():
            result = self._run(["export", "--session-id", "asst-1", "--output", "export.md"], handler)
            written = Path("export.md").read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured[0].method, "GET")
        self.assertEqual(captured[0].url.path, "/assistant/sessions/asst-1/export")
        self.assertEqual(captured[0].url.params.get("format"), "markdown")
        self.assertEqual(captured[0].url.params.get("include_traces"), "true")
        self.assertEqual(written, "# Assistant Session Export\nbody\n")
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["export_path"], "export.md")
        self.assertNotIn("export_text", payload["data"])
        self.assertNotIn("messages", payload["data"])

    def test_export_json_output_writes_structured_payload(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "format": "json",
                    "ids": {"session_id": "asst-1", "run_ids": ["asst-run-1"], "trace_ids": ["trace-1"]},
                    "counts": {"messages": 2, "events": 3, "traces": 1, "spans": 1, "model_invocations": 1},
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        with self.runner.isolated_filesystem():
            result = self._run(
                ["export", "--session-id", "asst-1", "--format", "json", "--output", "export.json"],
                handler,
            )
            written = json.loads(Path("export.json").read_text(encoding="utf-8"))

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(written["format"], "json")
        self.assertEqual(written["messages"][0]["content"], "hello")
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["format"], "json")
        self.assertEqual(payload["data"]["export_path"], "export.json")

    def test_export_reports_output_write_failure_as_structured_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "format": "markdown",
                    "ids": {"session_id": "asst-1", "run_ids": ["asst-run-1"], "trace_ids": ["trace-1"]},
                    "counts": {"messages": 2, "events": 3, "traces": 1, "spans": 1, "model_invocations": 1},
                    "export_text": "# Assistant Session Export\nbody\n",
                },
            )

        with self.runner.isolated_filesystem():
            result = self._run(["export", "--session-id", "asst-1", "--output", "."], handler)

        self.assertEqual(result.exit_code, 1, msg=result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "export_write_failed")
        self.assertEqual(payload["error"]["export_path"], ".")
        self.assertEqual(payload["error"]["diagnostic_export"]["session_id"], "asst-1")
        self.assertEqual(payload["error"]["diagnostic_export"]["counts"]["messages"], 2)

    def test_agent_list_supports_include_inactive(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "agent-1", "name": "Primary", "status": "active"},
                        {"id": "agent-2", "name": "Dormant", "status": "inactive"},
                    ],
                    "total": 2,
                },
            )

        result = self._run(["agent", "list", "--include-inactive"], handler)

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured[0].method, "GET")
        self.assertEqual(captured[0].url.path, "/assistant/agents")
        self.assertEqual(captured[0].url.params.get("include_inactive"), "true")
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["total"], 2)
        self.assertEqual(payload["data"]["items"][1]["status"], "inactive")

    def test_agent_get_fetches_single_agent(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"id": "agent-1", "name": "Primary", "skill_names": ["strategy-authoring"]},
            )

        result = self._run(["agent", "get", "agent-1"], handler)

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured[0].method, "GET")
        self.assertEqual(captured[0].url.path, "/assistant/agents/agent-1")
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["id"], "agent-1")
        self.assertEqual(payload["data"]["skill_names"], ["strategy-authoring"])

    def test_agent_create_posts_prompt_template_and_skills(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                201,
                json={
                    "id": "agent-1",
                    "name": "Validation Agent",
                    "prompt_template_id": "main-agent",
                    "skill_names": ["strategy-authoring", "doyoutrade-data"],
                },
            )

        result = self._run(
            [
                "agent",
                "create",
                "--name",
                "Validation Agent",
                "--prompt-template-id",
                "main-agent",
                "--model-route",
                "default",
                "--skill",
                "strategy-authoring",
                "--skill",
                "doyoutrade-data",
                "--tool",
                "read_file",
                "--max-turns",
                "8",
            ],
            handler,
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(captured[0].url.path, "/assistant/agents")
        self.assertEqual(
            json.loads(captured[0].content),
            {
                "name": "Validation Agent",
                "prompt_template_id": "main-agent",
                "model_route_name": "default",
                "tool_names": ["read_file"],
                "skill_names": ["strategy-authoring", "doyoutrade-data"],
                "max_turns": 8,
            },
        )
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["id"], "agent-1")

    def test_agent_create_supports_tool_configs_and_compaction_flags(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "agent-ctx", "name": "Context Agent"})

        result = self._run(
            [
                "agent",
                "create",
                "--name",
                "Context Agent",
                "--system-prompt",
                "You are a validator.",
                "--tool-config",
                "read_file=base",
                "--tool-config",
                "execute_bash=deferred",
                "--compaction-mode",
                "manual",
                "--compaction-auto-threshold-tokens",
                "28000",
                "--compaction-warning-threshold-tokens",
                "22000",
                "--compaction-preserve-recent-messages",
                "8",
                "--compaction-preserve-recent-tool-pairs",
                "3",
                "--compaction-micro-disabled",
                "--compaction-tool-result-max-chars",
                "2048",
                "--compaction-full-disabled",
                "--compaction-summary-model-route",
                "summary-route",
                "--compaction-disallow-slash-compact",
            ],
            handler,
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(
            json.loads(captured[0].content),
            {
                "name": "Context Agent",
                "system_prompt": "You are a validator.",
                "tool_configs": [
                    {"name": "read_file", "load_mode": "base"},
                    {"name": "execute_bash", "load_mode": "deferred"},
                ],
                "context_compaction": {
                    "mode": "manual",
                    "auto_threshold_tokens": 28000,
                    "warning_threshold_tokens": 22000,
                    "preserve_recent_messages": 8,
                    "preserve_recent_tool_pairs": 3,
                    "micro_compaction_enabled": False,
                    "tool_result_max_chars": 2048,
                    "full_compaction_enabled": False,
                    "summary_model_route_name": "summary-route",
                    "allow_slash_compact": False,
                },
            },
        )

    def test_agent_create_rejects_missing_prompt_source(self) -> None:
        result = self._run(["agent", "create", "--name", "Validation Agent"])

        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "missing_system_prompt")

    def test_agent_update_merges_skill_add_remove_before_put(self) -> None:
        paths: list[str] = []
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "id": "agent-1",
                        "name": "Validation Agent",
                        "skill_names": ["strategy-authoring", "doyoutrade-data"],
                    },
                )
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "agent-1",
                    "name": "Validation Agent",
                    "skill_names": bodies[-1]["skill_names"],
                },
            )

        result = self._run(
            [
                "agent",
                "update",
                "agent-1",
                "--add-skill",
                "strategy-iteration",
                "--remove-skill",
                "doyoutrade-data",
            ],
            handler,
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(paths, ["/assistant/agents/agent-1", "/assistant/agents/agent-1"])
        self.assertEqual(
            bodies,
            [{"skill_names": ["strategy-authoring", "strategy-iteration"]}],
        )
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["skill_names"], ["strategy-authoring", "strategy-iteration"])

    def test_agent_update_supports_tool_configs_model_route_clear_and_compaction_flags(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "agent-1", "name": "Validation Agent"})

        result = self._run(
            [
                "agent",
                "update",
                "agent-1",
                "--clear-model-route",
                "--tool-config",
                "read_file=deferred",
                "--tool-config",
                "execute_bash=base",
                "--compaction-enabled",
                "--compaction-mode",
                "manual",
                "--compaction-summary-model-route",
                "summary-route",
                "--compaction-allow-slash-compact",
            ],
            handler,
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured[0].method, "PUT")
        self.assertEqual(captured[0].url.path, "/assistant/agents/agent-1")
        self.assertEqual(
            json.loads(captured[0].content),
            {
                "model_route_name": "",
                "tool_configs": [
                    {"name": "read_file", "load_mode": "deferred"},
                    {"name": "execute_bash", "load_mode": "base"},
                ],
                "context_compaction": {
                    "enabled": True,
                    "mode": "manual",
                    "summary_model_route_name": "summary-route",
                    "allow_slash_compact": True,
                },
            },
        )

    def test_agent_update_supports_clearing_compaction_summary_model_route(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "agent-1", "name": "Validation Agent"})

        result = self._run(
            [
                "agent",
                "update",
                "agent-1",
                "--clear-compaction-summary-model-route",
            ],
            handler,
        )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(
            json.loads(captured[0].content),
            {
                "context_compaction": {
                    "summary_model_route_name": "",
                },
            },
        )

    def test_agent_update_rejects_conflicting_skill_options(self) -> None:
        result = self._run(
            [
                "agent",
                "update",
                "agent-1",
                "--skill",
                "strategy-authoring",
                "--add-skill",
                "doyoutrade-data",
            ]
        )

        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "conflicting_skill_args")

    def test_agent_update_rejects_conflicting_tool_config_options(self) -> None:
        result = self._run(
            [
                "agent",
                "update",
                "agent-1",
                "--tool-config",
                "read_file=base",
                "--add-tool",
                "execute_bash",
            ]
        )

        self.assertEqual(result.exit_code, 2, msg=result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "conflicting_tool_args")

    def test_agent_clone_posts_new_name(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "agent-2", "name": "Clone"})

        result = self._run(["agent", "clone", "agent-1", "--name", "Clone"], handler)

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(captured[0].url.path, "/assistant/agents/agent-1/clone")
        self.assertEqual(json.loads(captured[0].content), {"name": "Clone"})
        payload = json.loads(result.output)
        self.assertEqual(payload["data"]["id"], "agent-2")

    def test_agent_delete_calls_delete_endpoint(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        result = self._run(["agent", "delete", "agent-1"], handler)

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured[0].method, "DELETE")
        self.assertEqual(captured[0].url.path, "/assistant/agents/agent-1")
        self.assertNotIn("force", captured[0].url.params)
        payload = json.loads(result.output)
        self.assertTrue(payload["ok"])
        self.assertNotIn("error", payload)

    def test_agent_delete_force_passes_query_param(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        result = self._run(["agent", "delete", "agent-1", "--force"], handler)

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(captured[0].method, "DELETE")
        self.assertEqual(captured[0].url.path, "/assistant/agents/agent-1")
        self.assertEqual(captured[0].url.params.get("force"), "true")

    def test_run_creates_session_chats_then_exports(self) -> None:
        paths: list[str] = []
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.content:
                bodies.append(json.loads(request.content))
            if request.url.path == "/assistant/sessions":
                return httpx.Response(201, json={"session_id": "asst-1", "agent_id": "agent-1"})
            if request.url.path == "/assistant/sessions/asst-1/messages":
                return httpx.Response(200, json={"session": {"session_id": "asst-1"}, "messages": [], "trace_id": "trace-1"})
            if request.url.path == "/assistant/sessions/asst-1/export":
                return httpx.Response(
                    200,
                    json={
                        "format": "markdown",
                        "ids": {"session_id": "asst-1", "run_ids": ["asst-run-1"], "trace_ids": ["trace-1"]},
                        "counts": {"messages": 2, "events": 1, "traces": 1, "spans": 1, "model_invocations": 1},
                        "export_text": "# Export\n",
                    },
                )
            return httpx.Response(404, json={"detail": "unexpected"})

        with self.runner.isolated_filesystem():
            result = self._run(
                [
                    "run",
                    "--agent-id",
                    "agent-1",
                    "--title",
                    "Validation",
                    "--message",
                    "hello",
                    "--output",
                    "export.md",
                ],
                handler,
            )
            text = Path("export.md").read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(
            paths,
            [
                "/assistant/sessions",
                "/assistant/sessions/asst-1/messages",
                "/assistant/sessions/asst-1/export",
            ],
        )
        self.assertEqual(bodies, [{"agent_id": "agent-1", "title": "Validation"}, {"content": "hello"}])
        self.assertEqual(text, "# Export\n")
        payload = json.loads(result.output)
        self.assertEqual(payload["data"]["session_id"], "asst-1")
        self.assertEqual(payload["data"]["chat_trace_id"], "trace-1")
        self.assertEqual(payload["data"]["export_path"], "export.md")

    def test_run_waits_for_exported_diagnostics_after_chat(self) -> None:
        paths: list[str] = []
        export_calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal export_calls
            paths.append(request.url.path)
            if request.url.path == "/assistant/sessions":
                return httpx.Response(201, json={"session_id": "asst-1", "agent_id": "agent-1"})
            if request.url.path == "/assistant/sessions/asst-1/messages":
                return httpx.Response(
                    200,
                    json={"session": {"session_id": "asst-1"}, "messages": [], "trace_id": "trace-1"},
                )
            if request.url.path == "/assistant/sessions/asst-1/export":
                export_calls += 1
                if export_calls == 1:
                    return httpx.Response(
                        200,
                        json={
                            "format": "markdown",
                            "ids": {"session_id": "asst-1", "run_ids": [], "trace_ids": ["trace-1"]},
                            "counts": {"messages": 2, "events": 2, "traces": 1, "spans": 0, "model_invocations": 0},
                            "export_text": "# Export\nnot ready\n",
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "format": "markdown",
                        "ids": {
                            "session_id": "asst-1",
                            "run_ids": ["asst-run-1"],
                            "trace_ids": ["trace-1"],
                        },
                        "counts": {"messages": 2, "events": 3, "traces": 1, "spans": 1, "model_invocations": 1},
                        "export_text": "# Export\nready\n",
                    },
                )
            return httpx.Response(404, json={"detail": "unexpected"})

        with self.runner.isolated_filesystem():
            result = self._run(
                ["run", "--agent-id", "agent-1", "--message", "hello", "--output", "export.md"],
                handler,
            )
            text = Path("export.md").read_text(encoding="utf-8")

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(export_calls, 2)
        self.assertEqual(text, "# Export\nready\n")
        payload = json.loads(result.output)
        self.assertEqual(payload["data"]["counts"]["spans"], 1)
        self.assertEqual(payload["data"]["counts"]["model_invocations"], 1)

    def test_run_reports_output_write_failure_after_successful_chat(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/assistant/sessions":
                return httpx.Response(201, json={"session_id": "asst-1", "agent_id": "agent-1"})
            if request.url.path == "/assistant/sessions/asst-1/messages":
                return httpx.Response(200, json={"session": {"session_id": "asst-1"}, "messages": [], "trace_id": "trace-1"})
            if request.url.path == "/assistant/sessions/asst-1/export":
                return httpx.Response(
                    200,
                    json={
                        "format": "markdown",
                        "ids": {"session_id": "asst-1", "run_ids": [], "trace_ids": ["trace-1"]},
                        "counts": {"messages": 2, "events": 1, "traces": 1, "spans": 1, "model_invocations": 1},
                        "export_text": "# Export\n",
                    },
                )
            return httpx.Response(404, json={"detail": "unexpected"})

        with self.runner.isolated_filesystem():
            result = self._run(
                [
                    "run",
                    "--agent-id",
                    "agent-1",
                    "--message",
                    "hello",
                    "--output",
                    ".",
                ],
                handler,
            )

        self.assertEqual(result.exit_code, 1, msg=result.output)
        payload = json.loads(result.output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["error_code"], "export_write_failed")
        self.assertEqual(payload["error"]["diagnostic_export"]["session_id"], "asst-1")

    def test_assistant_command_module_keeps_thin_http_client_boundary(self) -> None:
        source = Path("doyoutrade/cli/commands/assistant.py").read_text(encoding="utf-8")

        self.assertNotIn("doyoutrade.bootstrap", source)
        self.assertNotIn("build_platform_runtime", source)
        self.assertNotIn("TradingPlatformService", source)
        self.assertNotIn("SqlAlchemy", source)
        self.assertNotIn("session_factory", source)


if __name__ == "__main__":
    unittest.main()
