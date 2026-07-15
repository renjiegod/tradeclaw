"""Tests for the HTTP-mode cron write commands in ``doyoutrade.cli.commands.cron``.

The write commands (``cron create / update / delete / pause / resume /
trigger``) cannot run in-process — the API server is the only place the
``AgentCronManager`` (and its APScheduler) lives. These tests verify the
CLI ↔ HTTP boundary:

* ``invoke_api`` translates 2xx / 4xx / 5xx / transport errors into the
  same envelope + exit-code contract as the in-process tool path.
* ``cron create`` / ``cron update`` / ``cron delete`` / ``cron pause`` /
  ``cron resume`` / ``cron trigger`` build the right URL + payload and
  forward the response.
* ``--agent-id`` falls back to ``DOYOUTRADE_AGENT_ID``; absence is a
  ``missing_agent_id`` validation error (no HTTP call attempted).
* ``--pre-action`` bad JSON / wrong shape is an ``invalid_pre_action_json``
  validation error (no HTTP call attempted).

``httpx.MockTransport`` intercepts every request and lets the test
inspect the outbound URL / method / body without spinning up uvicorn.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from typing import Any, Callable
from unittest.mock import patch

import httpx
from click.testing import CliRunner

from doyoutrade.cli._api import invoke_api, resolve_api_base_url
from doyoutrade.cli._envelope import (
    EXIT_FAILURE,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_VALIDATION,
    Meta,
)
from doyoutrade.cli.commands.cron import cron


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured before any test patches the symbol


def _patch_async_client(handler: Callable[[httpx.Request], httpx.Response]):
    """Patch ``httpx.AsyncClient`` in ``_api`` to route requests via MockTransport.

    Returned context manager swaps the class with a factory that builds a
    fresh client per call (the real code uses ``async with httpx.AsyncClient()``
    which closes the transport on exit; reusing one instance breaks the
    second test). The factory must reference the *captured* real class
    rather than ``httpx.AsyncClient``, since the patch makes the latter
    point at the factory itself — recursing on each call.
    """

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    return patch("doyoutrade.cli._api.httpx.AsyncClient", factory)


class ResolveApiBaseUrlTests(unittest.TestCase):
    def test_env_wins(self) -> None:
        with patch.dict(os.environ, {"DOYOUTRADE_API_URL": "https://api.example.com/"}, clear=False):
            self.assertEqual(resolve_api_base_url(), "https://api.example.com")

    def test_falls_back_to_server_settings(self) -> None:
        # Clear env so the fallback path runs.
        env = {k: v for k, v in os.environ.items() if k != "DOYOUTRADE_API_URL"}
        with patch.dict(os.environ, env, clear=True):
            url = resolve_api_base_url()
        # The config loaded for tests has server.host = 0.0.0.0 → must be
        # rewritten to 127.0.0.1 (cannot dial the wildcard).
        self.assertTrue(url.startswith("http://"))
        self.assertNotIn("0.0.0.0", url)


class InvokeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env_patch = patch.dict(
            os.environ,
            {"DOYOUTRADE_API_URL": "http://test.local"},
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_2xx_returns_success_envelope_with_body(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "cj_1", "name": "demo"})

        with _patch_async_client(handler):
            envelope, exit_code = asyncio.run(
                invoke_api(
                    "POST",
                    "/assistant/agents/asst-a/cron/jobs",
                    json={"name": "demo"},
                    meta=Meta(),
                )
            )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["id"], "cj_1")
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(str(captured[0].url), "http://test.local/assistant/agents/asst-a/cron/jobs")
        self.assertEqual(json.loads(captured[0].content), {"name": "demo"})

    def test_204_returns_success_with_no_data(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        with _patch_async_client(handler):
            envelope, exit_code = asyncio.run(
                invoke_api("DELETE", "/some/path", meta=Meta())
            )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(envelope["ok"])
        # No body → no ``data`` key (success_envelope skips empty bodies).
        self.assertNotIn("data", envelope)

    def test_404_uses_custom_not_found_code(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "cron job not found: cj_xxx"})

        with _patch_async_client(handler):
            envelope, exit_code = asyncio.run(
                invoke_api(
                    "DELETE",
                    "/assistant/agents/asst-a/cron/jobs/cj_xxx",
                    meta=Meta(),
                    not_found_error_code="cron_job_not_found",
                )
            )

        self.assertEqual(exit_code, EXIT_NOT_FOUND)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "cron_job_not_found")
        self.assertIn("cron job not found", envelope["error"]["message"])
        self.assertEqual(envelope["error"]["http_status"], 404)

    def test_400_maps_to_validation_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"detail": "name is required"})

        with _patch_async_client(handler):
            envelope, exit_code = asyncio.run(
                invoke_api("POST", "/some/path", json={}, meta=Meta())
            )

        self.assertEqual(exit_code, EXIT_VALIDATION)
        self.assertEqual(envelope["error"]["error_code"], "validation_error")
        self.assertEqual(envelope["error"]["message"], "name is required")

    def test_400_with_structured_detail_preserves_error_code(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "detail": {
                        "error_code": "cron_strategy_kind_retired",
                        "message": (
                            "strategy cron task_kinds are retired; schedule "
                            "strategy execution via a Task Trigger"
                        ),
                    }
                },
            )

        with _patch_async_client(handler):
            envelope, exit_code = asyncio.run(
                invoke_api("POST", "/some/path", json={}, meta=Meta())
            )

        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertEqual(
            envelope["error"]["error_code"], "cron_strategy_kind_retired",
        )
        self.assertIn(
            "Task Trigger", envelope["error"]["message"],
        )

    def test_500_maps_to_server_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        with _patch_async_client(handler):
            envelope, exit_code = asyncio.run(
                invoke_api("POST", "/some/path", meta=Meta())
            )

        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertEqual(envelope["error"]["error_code"], "server_error")

    def test_connect_error_maps_to_api_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        with _patch_async_client(handler):
            envelope, exit_code = asyncio.run(
                invoke_api("POST", "/some/path", meta=Meta())
            )

        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertEqual(envelope["error"]["error_code"], "api_unavailable")
        self.assertIn("test.local", envelope["error"]["message"])
        self.assertIn("Start the server", envelope["error"]["message"])


class CronWriteCommandTests(unittest.TestCase):
    """Drive the click commands end-to-end with the HTTP boundary mocked."""

    def setUp(self) -> None:
        self.runner = CliRunner()
        self._env_patch = patch.dict(
            os.environ,
            {
                "DOYOUTRADE_API_URL": "http://test.local",
                "DOYOUTRADE_AGENT_ID": "asst-env",
            },
            clear=False,
        )
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()

    def _run(self, args: list[str], handler: Callable[[httpx.Request], httpx.Response]):
        # ``run_async_command`` reads ``ctx.obj["fmt"]`` — when invoking the
        # ``cron`` group directly (no root ``cli`` group), CliRunner doesn't
        # populate ``ctx.obj``. Inject it so the command exercises the same
        # output path as the real CLI entry point.
        with _patch_async_client(handler):
            return self.runner.invoke(cron, args, obj={"fmt": "json"}, catch_exceptions=False)

    def test_create_happy_path_uses_env_agent_id(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                201,
                json={
                    "id": "cj_1",
                    "agent_id": "asst-env",
                    "name": "every 5m",
                    "enabled": True,
                },
            )

        result = self._run(
            [
                "create",
                "--name", "every 5m",
                "--cron-expression", "*/5 * * * *",
                "--input-template", "say hi at {{now}}",
            ],
            handler,
        )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        envelope = json.loads(result.output)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["id"], "cj_1")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].method, "POST")
        self.assertEqual(
            str(captured[0].url),
            "http://test.local/assistant/agents/asst-env/cron/jobs",
        )
        body = json.loads(captured[0].content)
        self.assertEqual(body["name"], "every 5m")
        self.assertEqual(body["cron_expression"], "*/5 * * * *")
        from doyoutrade.cli.commands.cron import _resolve_local_iana_tz

        self.assertEqual(body["timezone"], _resolve_local_iana_tz())
        self.assertEqual(body["enabled"], True)
        # pre_action omitted unless provided.
        self.assertNotIn("pre_action", body)

    def test_create_rejects_when_agent_id_missing(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "DOYOUTRADE_AGENT_ID"}
        with patch.dict(os.environ, env, clear=True), \
                patch.dict(os.environ, {"DOYOUTRADE_API_URL": "http://test.local"}, clear=False):
            called: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                called.append(request)
                return httpx.Response(201, json={})

            with _patch_async_client(handler):
                result = self.runner.invoke(
                    cron,
                    [
                        "create",
                        "--name", "x",
                        "--cron-expression", "*/5 * * * *",
                        "--input-template", "t",
                    ],
                    obj={"fmt": "json"},
                    catch_exceptions=False,
                )

            envelope = json.loads(result.output)
            self.assertEqual(result.exit_code, EXIT_VALIDATION, msg=result.output)
            self.assertEqual(envelope["error"]["error_code"], "missing_agent_id")
            self.assertEqual(called, [], "no HTTP call should be issued")

    def test_create_rejects_bad_pre_action_json(self) -> None:
        called: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(201, json={})

        result = self._run(
            [
                "create",
                "--name", "x",
                "--cron-expression", "*/5 * * * *",
                "--input-template", "t",
                "--pre-action", "{not-json",
            ],
            handler,
        )

        envelope = json.loads(result.output)
        self.assertEqual(result.exit_code, EXIT_VALIDATION, msg=result.output)
        self.assertEqual(envelope["error"]["error_code"], "invalid_pre_action_json")
        self.assertEqual(called, [])

    def test_create_rejects_pre_action_missing_kind(self) -> None:
        result = self._run(
            [
                "create",
                "--name", "x",
                "--cron-expression", "*/5 * * * *",
                "--input-template", "t",
                "--pre-action", '{"params": {}}',
            ],
            lambda req: httpx.Response(201, json={}),
        )

        envelope = json.loads(result.output)
        self.assertEqual(result.exit_code, EXIT_VALIDATION, msg=result.output)
        self.assertEqual(envelope["error"]["error_code"], "invalid_pre_action_json")
        self.assertIn("kind", envelope["error"]["message"])

    def test_create_forwards_pre_action(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "cj_2"})

        result = self._run(
            [
                "create",
                "--name", "with-pre",
                "--cron-expression", "0 9 * * *",
                "--input-template", "go",
                "--pre-action", '{"kind": "trigger_cycle", "params": {"instance_id": "inst_1"}}',
            ],
            handler,
        )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        body = json.loads(captured[0].content)
        self.assertEqual(body["pre_action"], {
            "kind": "trigger_cycle",
            "params": {"instance_id": "inst_1"},
        })

    def test_create_forwards_task_pipeline_payload_without_input_template(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "cj_task"})

        result = self._run(
            [
                "create",
                "--name", "chat-reminder",
                "--cron-expression", "30 14 * * 1-5",
                "--task-kind", "agent_chat_reply",
                "--task-params", (
                    '{"user_request":"14:30 提醒我",'
                    '"target_session_id":"asst-user"}'
                ),
            ],
            handler,
        )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        body = json.loads(captured[0].content)
        self.assertNotIn("input_template", body)
        self.assertEqual(body["task_kind"], "agent_chat_reply")
        self.assertEqual(
            body["task_params_json"]["user_request"], "14:30 提醒我",
        )

    def test_create_rejects_input_template_and_task_kind_conflict(self) -> None:
        called: list[httpx.Request] = []

        result = self._run(
            [
                "create",
                "--name", "bad",
                "--cron-expression", "30 14 * * 1-5",
                "--input-template", "legacy",
                "--task-kind", "agent_chat_reply",
                "--task-params", '{"user_request":"hi"}',
            ],
            lambda req: called.append(req) or httpx.Response(201, json={}),
        )

        envelope = json.loads(result.output)
        self.assertEqual(result.exit_code, EXIT_VALIDATION, msg=result.output)
        self.assertEqual(
            envelope["error"]["error_code"], "conflicting_execution_form",
        )
        self.assertEqual(called, [])

    def test_update_only_sends_provided_fields(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "cj_1", "enabled": False})

        result = self._run(
            [
                "update", "cj_1",
                "--agent-id", "asst-x",
                "--disabled",
            ],
            handler,
        )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertEqual(captured[0].method, "PUT")
        self.assertEqual(
            str(captured[0].url),
            "http://test.local/assistant/agents/asst-x/cron/jobs/cj_1",
        )
        body = json.loads(captured[0].content)
        self.assertEqual(body, {"enabled": False})

    def test_update_task_params_alone_is_sent(self) -> None:
        # Standalone --task-params (no --task-kind) must reach the server as
        # task_params_json — the "tweak no_signal_mode on an existing alert"
        # flow. Regression: previously this option parsed but was dropped
        # unless --task-kind was also passed (a silent no-op).
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "cj_1"})

        result = self._run(
            [
                "update", "cj_1",
                "--agent-id", "asst-x",
                "--task-params",
                '{"strategy_task_ids":["task-1"],"no_signal_mode":"full"}',
            ],
            handler,
        )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        body = json.loads(captured[0].content)
        self.assertEqual(body["task_params_json"]["no_signal_mode"], "full")
        self.assertEqual(body["task_params_json"]["strategy_task_ids"], ["task-1"])
        self.assertNotIn("task_kind", body)

    def test_update_clear_pre_action_sends_null(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={"id": "cj_1"})

        result = self._run(
            ["update", "cj_1", "--clear-pre-action"],
            handler,
        )

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        body = json.loads(captured[0].content)
        self.assertEqual(body, {"pre_action": None})

    def test_update_clear_and_set_pre_action_conflict(self) -> None:
        result = self._run(
            [
                "update", "cj_1",
                "--clear-pre-action",
                "--pre-action", '{"kind": "x"}',
            ],
            lambda req: httpx.Response(200, json={}),
        )

        envelope = json.loads(result.output)
        self.assertEqual(result.exit_code, EXIT_VALIDATION, msg=result.output)
        self.assertEqual(envelope["error"]["error_code"], "validation_error")

    def test_update_with_no_fields_is_a_noop_success(self) -> None:
        called: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(200, json={})

        result = self._run(["update", "cj_1"], handler)

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        # No HTTP call should be issued for an empty patch.
        self.assertEqual(called, [])

    def test_delete_uses_correct_method_and_path(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(204)

        result = self._run(["delete", "cj_1"], handler)

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertEqual(captured[0].method, "DELETE")
        self.assertEqual(
            str(captured[0].url),
            "http://test.local/assistant/agents/asst-env/cron/jobs/cj_1",
        )

    def test_delete_404_maps_to_not_found(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "cron job not found: cj_x"})

        result = self._run(["delete", "cj_x"], handler)

        envelope = json.loads(result.output)
        self.assertEqual(result.exit_code, EXIT_NOT_FOUND, msg=result.output)
        self.assertEqual(envelope["error"]["error_code"], "cron_job_not_found")

    def test_pause_resume_trigger_path_and_method(self) -> None:
        for verb, expected_status in [("pause", 200), ("resume", 200), ("trigger", 200)]:
            captured: list[httpx.Request] = []

            def handler(request: httpx.Request, _verb=verb) -> httpx.Response:
                captured.append(request)
                if _verb == "trigger":
                    return httpx.Response(expected_status, json={"cron_job_run_id": "crun-abc"})
                return httpx.Response(expected_status, json={"id": "cj_1"})

            result = self._run([verb, "cj_1"], handler)

            self.assertEqual(result.exit_code, EXIT_OK, msg=f"{verb}: {result.output}")
            self.assertEqual(captured[0].method, "POST")
            # trigger maps to POST .../run; pause/resume map to .../pause and .../resume.
            tail = "run" if verb == "trigger" else verb
            self.assertEqual(
                str(captured[0].url),
                f"http://test.local/assistant/agents/asst-env/cron/jobs/cj_1/{tail}",
            )

    def test_trigger_returns_run_id_in_envelope(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"cron_job_run_id": "crun-zzz"})

        result = self._run(["trigger", "cj_1"], handler)
        envelope = json.loads(result.output)

        self.assertEqual(result.exit_code, EXIT_OK, msg=result.output)
        self.assertEqual(envelope["data"]["cron_job_run_id"], "crun-zzz")

    def test_connect_error_envelope(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("nope")

        result = self._run(["trigger", "cj_1"], handler)
        envelope = json.loads(result.output)

        self.assertEqual(result.exit_code, EXIT_FAILURE, msg=result.output)
        self.assertEqual(envelope["error"]["error_code"], "api_unavailable")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
