"""Regression tests for sync-dict tool dispatch through API + CLI.

The sandboxed file tools in ``doyoutrade/tools/file_tools.py``
(``read_strategy_file`` / ``write_strategy_file`` / ``edit_strategy_file``
/ ``list_strategy_files``) deliberately keep a synchronous
``def execute(self, **kwargs) -> dict[str, Any]`` signature.  Both the
API-side dispatcher at ``doyoutrade/api/app.py::execute_assistant_tool``
and the in-process CLI dispatcher at ``doyoutrade/cli/_invoke.py::invoke_tool``
used to unconditionally ``await tool.execute(**call_args)``, which raised
``TypeError: object dict can't be used in 'await' expression`` and got
mislabelled as ``validation_error`` — masking the real shape mismatch.

These tests pin the contract:

* :func:`adapt_sync_dict_to_tool_result` adapts both success and error
  dicts into the ``ToolResult.text`` shape :func:`parse_tool_result`
  expects (``[error:<code>]`` prefix + fenced JSON payload).
* The two dispatchers honour both sync-dict and async-ToolResult tools
  on the same code path — no false ``validation_error`` for an obviously
  valid kwarg set, no Python-repr ``str(dict)`` leaking into the
  envelope text.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from doyoutrade.api.app import create_app
from doyoutrade.cli._envelope import EXIT_OK, Meta, exit_code_for_error
from doyoutrade.cli._invoke import invoke_tool
from doyoutrade.persistence.strategy_storage import StrategyStorage
from doyoutrade.tools import (
    OperationHandler,
    ToolResult,
    adapt_sync_dict_to_tool_result,
)
from doyoutrade.tools import _sandbox
from doyoutrade.tools.file_tools import WriteFileTool

# Reuse the heavy fakes already proven in test_api_app.  They satisfy
# create_app's wiring without standing up a real runtime.
from tests.test_api_app import (
    _FakeApprovalGate,
    _FakeAssistantService,
    _FakeService,
)


# ---------------------------------------------------------------------------
# Stub tools
# ---------------------------------------------------------------------------


class _SyncDictSuccessTool(OperationHandler):
    """Returns a plain dict synchronously, mimicking ``WriteStrategyFileTool``."""

    name = "sync_dict_ok"
    description = "Stub sync tool that returns a success dict."
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"echo": {"type": "string"}},
        "required": [],
    }

    def execute(self, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        return {
            "status": "ok",
            "_summary": "wrote 42 bytes to strategy.py",
            "file_path": "strategy.py",
            "bytes_written": 42,
            "echo": kwargs.get("echo"),
        }


class _SyncDictErrorTool(OperationHandler):
    """Returns a structured-error dict synchronously."""

    name = "sync_dict_err"
    description = "Stub sync tool that reports an error."
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }

    def execute(self, **_kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        return {
            "status": "error",
            "error_code": "file_not_found",
            "message": "strategy.py does not exist",
        }


class _AsyncToolResultTool(OperationHandler):
    """Async tool returning a ToolResult — the existing common case."""

    name = "async_tool_result"
    description = "Stub async tool that returns a ToolResult."
    parameters = {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }

    async def execute(self, **_kwargs: Any) -> ToolResult:  # type: ignore[override]
        return ToolResult(text="hello from async tool")


# ---------------------------------------------------------------------------
# adapt_sync_dict_to_tool_result — pure unit coverage
# ---------------------------------------------------------------------------


class AdaptSyncDictToToolResultTests(unittest.TestCase):
    def test_success_with_summary_and_payload(self) -> None:
        raw = {
            "status": "ok",
            "_summary": "wrote 42 bytes to strategy.py",
            "file_path": "strategy.py",
            "bytes_written": 42,
        }
        result = adapt_sync_dict_to_tool_result(raw)

        self.assertIsInstance(result, ToolResult)
        self.assertFalse(result.is_error)
        # Prose head preserved.
        self.assertIn("wrote 42 bytes to strategy.py", result.text)
        # Fenced JSON payload preserves every non-_summary field.
        self.assertIn("```json", result.text)
        fence = result.text.split("```json", 1)[1].split("```", 1)[0]
        parsed = json.loads(fence)
        self.assertEqual(parsed["file_path"], "strategy.py")
        self.assertEqual(parsed["bytes_written"], 42)
        self.assertNotIn("_summary", parsed)

    def test_success_without_summary_still_carries_payload(self) -> None:
        raw = {"status": "ok", "file_path": "strategy.py", "bytes_written": 17}
        result = adapt_sync_dict_to_tool_result(raw)

        self.assertFalse(result.is_error)
        self.assertIn("```json", result.text)
        fence = result.text.split("```json", 1)[1].split("```", 1)[0]
        parsed = json.loads(fence)
        self.assertEqual(parsed["bytes_written"], 17)

    def test_error_dict_renders_error_prefix(self) -> None:
        raw = {
            "status": "error",
            "error_code": "file_not_found",
            "message": "strategy.py does not exist",
        }
        result = adapt_sync_dict_to_tool_result(raw)

        self.assertTrue(result.is_error)
        self.assertTrue(
            result.text.startswith("[error:file_not_found] "),
            msg=f"unexpected text: {result.text!r}",
        )
        self.assertIn("strategy.py does not exist", result.text)

    def test_is_error_flag_also_triggers_error_path(self) -> None:
        raw = {
            "is_error": True,
            "error_code": "io_error",
            "message": "disk full",
        }
        result = adapt_sync_dict_to_tool_result(raw)

        self.assertTrue(result.is_error)
        self.assertTrue(result.text.startswith("[error:io_error] "))

    def test_error_dict_carries_repair_hints_as_hint(self) -> None:
        raw = {
            "status": "error",
            "error_code": "old_string_not_unique",
            "message": "matches 3 times",
            "repair_hints": [
                "pass replace_all=true",
                "or extend old_string with surrounding context",
            ],
        }
        result = adapt_sync_dict_to_tool_result(raw)

        self.assertTrue(result.is_error)
        self.assertIn("[error:old_string_not_unique]", result.text)
        # tool_result_from_error_dict joins repair_hints with "; " under a Hint: line.
        self.assertIn("Hint: pass replace_all=true; ", result.text)


# ---------------------------------------------------------------------------
# In-process CLI dispatcher (invoke_tool)
# ---------------------------------------------------------------------------


class InvokeToolDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_dict_success_flows_into_envelope_data(self) -> None:
        tool = _SyncDictSuccessTool()
        envelope, code = await invoke_tool(
            tool, {"echo": "hi"}, meta=Meta()
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(envelope.get("ok"), msg=envelope)
        data = envelope["data"]
        self.assertEqual(data["file_path"], "strategy.py")
        self.assertEqual(data["bytes_written"], 42)
        self.assertEqual(data["echo"], "hi")
        self.assertEqual(data.get("_summary"), "wrote 42 bytes to strategy.py")

    async def test_sync_dict_error_surfaces_error_code(self) -> None:
        envelope, code = await invoke_tool(
            _SyncDictErrorTool(), {}, meta=Meta()
        )
        self.assertEqual(code, exit_code_for_error("file_not_found"))
        self.assertFalse(envelope.get("ok"), msg=envelope)
        err = envelope["error"]
        self.assertEqual(err["error_code"], "file_not_found")
        self.assertIn("strategy.py does not exist", err["message"])

    async def test_async_tool_result_still_works(self) -> None:
        envelope, code = await invoke_tool(
            _AsyncToolResultTool(), {}, meta=Meta()
        )
        self.assertEqual(code, EXIT_OK)
        self.assertTrue(envelope.get("ok"), msg=envelope)
        # No data block; the prose lives in _summary.
        self.assertIn("hello from async tool", json.dumps(envelope))


# ---------------------------------------------------------------------------
# API dispatcher (POST /assistant/tools/{tool}/execute)
# ---------------------------------------------------------------------------


class ApiOperationHandlerDispatcherTests(unittest.TestCase):
    """Round-trip through ``execute_assistant_tool`` via the FastAPI app.

    Injects a stub tool into the live ``cli_tool_registry`` after
    ``create_app`` finishes wiring so we exercise the actual endpoint,
    span attributes, and serialisation — not a hand-rolled mirror.
    """

    def _build_app_with_tool(self, tool: OperationHandler) -> Any:
        app = create_app(
            _FakeService(),
            _FakeApprovalGate(),
            assistant_service=_FakeAssistantService(),
        )
        registry = app.state.cli_tool_registry
        registry._tools[tool.name] = tool  # noqa: SLF001 — test-only injection
        return app

    def test_sync_dict_success_through_api(self) -> None:
        app = self._build_app_with_tool(_SyncDictSuccessTool())
        with TestClient(app) as client:
            r = client.post(
                "/assistant/tools/sync_dict_ok/execute",
                json={"args": {"echo": "via_api"}},
            )
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertEqual(body["tool_name"], "sync_dict_ok")
        self.assertFalse(body["is_error"], msg=body)
        # text must carry the fenced JSON payload with the dict fields.
        text = body["text"]
        self.assertIn("```json", text)
        fence = text.split("```json", 1)[1].split("```", 1)[0]
        parsed = json.loads(fence)
        self.assertEqual(parsed["file_path"], "strategy.py")
        self.assertEqual(parsed["echo"], "via_api")
        # Critically: NOT a Python repr of the dict (would contain single quotes).
        self.assertNotIn("'status': 'ok'", text)

    def test_sync_dict_error_through_api(self) -> None:
        app = self._build_app_with_tool(_SyncDictErrorTool())
        with TestClient(app) as client:
            r = client.post(
                "/assistant/tools/sync_dict_err/execute",
                json={"args": {}},
            )
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertTrue(body["is_error"], msg=body)
        # The dispatcher must NOT mislabel this as "validation_error" —
        # the underlying TypeError await-bug used to surface that token.
        self.assertNotIn("[error:validation_error]", body["text"])
        self.assertNotIn("object dict can't be used", body["text"])
        # Real error code from the tool flows through unchanged.
        self.assertTrue(
            body["text"].startswith("[error:file_not_found] "),
            msg=f"unexpected text: {body['text']!r}",
        )

    def test_async_tool_result_still_works_through_api(self) -> None:
        app = self._build_app_with_tool(_AsyncToolResultTool())
        with TestClient(app) as client:
            r = client.post(
                "/assistant/tools/async_tool_result/execute",
                json={"args": {}},
            )
        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertFalse(body["is_error"], msg=body)
        self.assertIn("hello from async tool", body["text"])


# ---------------------------------------------------------------------------
# End-to-end: real WriteStrategyFileTool through the API
# ---------------------------------------------------------------------------


class WriteStrategyFileEndToEndTests(unittest.TestCase):
    """Pin the exact scenario from the failed assistant session.

    Before the dispatcher fix, sync-dict-returning tools raised
    ``TypeError: object dict can't be used in 'await' expression``
    which was mislabelled as ``validation_error``.

    Now uses the renamed ``WriteFileTool`` (formerly ``WriteStrategyFileTool``)
    with the path-based ``_sandbox`` registry instead of session_id callbacks.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.storage = StrategyStorage(self.tmp / "strategies")
        self.work_dir = self.storage.open_draft("sd-1", "sess-1", base_version=None)
        # Register sandbox so the tool accepts paths under work_dir.
        _sandbox.register_sandbox(self.work_dir)

    def tearDown(self) -> None:
        _sandbox.unregister_sandbox(self.work_dir)
        shutil.rmtree(self.tmp)

    def test_write_file_round_trip(self) -> None:
        """write_file (formerly write_strategy_file) dispatched through API."""
        tool = WriteFileTool()
        target_path = str(self.work_dir / "strategy.py")

        app = create_app(
            _FakeService(),
            _FakeApprovalGate(),
            assistant_service=_FakeAssistantService(),
        )
        app.state.cli_tool_registry._tools[tool.name] = tool  # noqa: SLF001

        with TestClient(app) as client:
            r = client.post(
                f"/assistant/tools/{tool.name}/execute",
                json={
                    "args": {
                        "file_path": target_path,
                        "content": "x = 1\n",
                    }
                },
            )

        self.assertEqual(r.status_code, 200, msg=r.text)
        body = r.json()
        self.assertFalse(body["is_error"], msg=body)
        # Bug signature must be gone.
        self.assertNotIn("object dict can't be used", body["text"])
        self.assertNotIn("[error:validation_error]", body["text"])
        # The write actually landed.
        self.assertEqual((self.work_dir / "strategy.py").read_text(), "x = 1\n")
        # Payload carries the structured fields.
        fence = body["text"].split("```json", 1)[1].split("```", 1)[0]
        parsed = json.loads(fence)
        self.assertEqual(parsed["file_path"], target_path)
        self.assertEqual(parsed["bytes_written"], len("x = 1\n".encode("utf-8")))


if __name__ == "__main__":
    unittest.main()
