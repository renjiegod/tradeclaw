"""Integration tests for doyoutrade.cli._invoke.

Verifies that ``invoke_tool`` correctly translates an
``OperationHandler.execute`` result into a CLI envelope (success / error /
exception). Uses ``LookupStockSymbolTool`` which is the cheapest real
tool to invoke (no DB; talks to an external API). The external call is
patched out in the negative-path tests to keep them deterministic.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from doyoutrade.cli._envelope import (
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_VALIDATION,
    Meta,
)
from doyoutrade.cli._invoke import invoke_tool, read_session_meta
from doyoutrade.tools import OperationHandler, ToolResult


class _SuccessStubTool(OperationHandler):
    name = "stub_success"
    description = "stub"
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    async def execute(self, value: str) -> ToolResult:  # type: ignore[override]
        # Emit prose + fenced JSON, mirroring the real tool pattern.
        from doyoutrade.tools._prose import append_json_payload

        return ToolResult(text=append_json_payload(f"echo {value}", {"status": "ok", "value": value}))


class _ErrorStubTool(OperationHandler):
    name = "stub_error"
    description = "stub"
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    async def execute(self, value: str) -> ToolResult:  # type: ignore[override]
        del value  # exercising the error path, not the input
        from doyoutrade.tools._prose import format_error_text

        return ToolResult(
            text=format_error_text("wrong_identifier_type", "bad shape", "use get_task"),
            is_error=True,
        )


class _ExceptionStubTool(OperationHandler):
    name = "stub_exception"
    description = "stub"
    category = "agent"
    parameters = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }

    async def execute(self, value: str) -> ToolResult:  # type: ignore[override]
        del value
        raise RuntimeError("kaboom")


class _StrReturningTool(OperationHandler):
    """Some legacy tools return a bare str; the invoker must handle it."""

    name = "stub_str"
    description = "stub"
    category = "agent"
    parameters = {"type": "object", "properties": {}}

    async def execute(self) -> str:  # type: ignore[override]
        return "plain text result"


class InvokeToolTests(unittest.TestCase):
    def test_success_envelope_carries_data_and_summary(self) -> None:
        envelope, exit_code = asyncio.run(
            invoke_tool(_SuccessStubTool(), {"value": "hello"}, meta=Meta())
        )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["data"]["value"], "hello")
        self.assertEqual(envelope["data"]["_summary"], "echo hello")

    def test_error_envelope_carries_error_code_and_hint(self) -> None:
        envelope, exit_code = asyncio.run(
            invoke_tool(_ErrorStubTool(), {"value": "x"}, meta=Meta())
        )

        self.assertEqual(exit_code, EXIT_VALIDATION)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "wrong_identifier_type")
        self.assertEqual(envelope["error"]["hint"], "use get_task")

    def test_exception_is_wrapped_as_internal_error(self) -> None:
        envelope, exit_code = asyncio.run(
            invoke_tool(_ExceptionStubTool(), {"value": "x"}, meta=Meta())
        )

        self.assertEqual(exit_code, EXIT_FAILURE)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["error_code"], "internal_error")
        self.assertEqual(envelope["error"]["error_type"], "RuntimeError")
        self.assertIn("kaboom", envelope["error"]["message"])

    def test_unexpected_kwarg_returns_validation_error(self) -> None:
        envelope, exit_code = asyncio.run(
            invoke_tool(_SuccessStubTool(), {"value": "x", "unexpected": True}, meta=Meta())
        )

        self.assertEqual(exit_code, EXIT_VALIDATION)
        self.assertEqual(envelope["error"]["error_code"], "validation_error")
        self.assertEqual(envelope["error"]["error_type"], "TypeError")

    def test_str_return_is_wrapped_as_success(self) -> None:
        envelope, exit_code = asyncio.run(invoke_tool(_StrReturningTool(), {}, meta=Meta()))

        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(envelope["ok"])
        # No JSON block → only summary lands.
        self.assertEqual(envelope["data"]["_summary"], "plain text result")

    def test_meta_round_trips_to_envelope(self) -> None:
        meta = Meta(agent_id="asst-1", session_id="s-1", debug_session_id="s-1")
        envelope, _ = asyncio.run(invoke_tool(_SuccessStubTool(), {"value": "x"}, meta=meta))

        self.assertEqual(envelope["meta"]["agent_id"], "asst-1")
        self.assertEqual(envelope["meta"]["session_id"], "s-1")


class ReadSessionMetaTests(unittest.TestCase):
    def test_reads_all_four_env_vars(self) -> None:
        env = {
            "DOYOUTRADE_AGENT_ID": "asst-a",
            "DOYOUTRADE_SESSION_ID": "sess-b",
            "DOYOUTRADE_DEBUG_SESSION_ID": "dbg-c",
            "DOYOUTRADE_RUN_ID": "run-d",
        }
        with patch.dict("os.environ", env, clear=False):
            meta = read_session_meta()

        self.assertEqual(meta.agent_id, "asst-a")
        self.assertEqual(meta.session_id, "sess-b")
        self.assertEqual(meta.debug_session_id, "dbg-c")
        self.assertEqual(meta.run_id, "run-d")

    def test_missing_vars_yield_none_fields(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            meta = read_session_meta()

        self.assertIsNone(meta.agent_id)
        self.assertIsNone(meta.session_id)
        self.assertIsNone(meta.debug_session_id)
        self.assertIsNone(meta.run_id)


class StockLookupInvocationTests(unittest.TestCase):
    """Smoke test against the real LookupStockSymbolTool with the upstream API mocked."""

    def test_happy_path_with_mocked_upstream(self) -> None:
        from doyoutrade.api.operations.stock_lookup import LookupStockSymbolTool

        fake_result = {"items": [{"symbol": "600519.SH", "name": "贵州茅台", "market": "SH"}]}

        with patch(
            "doyoutrade.data.instrument_universe.search_instrument_universe",
            new=AsyncMock(return_value=fake_result),
        ):
            envelope, exit_code = asyncio.run(
                invoke_tool(
                    LookupStockSymbolTool(),
                    {"q": "茅台", "limit": 5, "source": "akshare_a"},
                    meta=Meta(),
                )
            )

        self.assertEqual(exit_code, EXIT_OK)
        self.assertTrue(envelope["ok"])
        # The fenced JSON payload landed under data.items.
        self.assertEqual(envelope["data"]["items"][0]["symbol"], "600519.SH")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
