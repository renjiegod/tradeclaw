from __future__ import annotations

import asyncio
import unittest
from typing import Any

from tests.e2e.support import (
    E2EModelMode,
    build_e2e_runtime,
    e2e_enabled,
    wait_for_model_invocation_tasks,
)
from doyoutrade.assistant.session_export import build_assistant_session_export
from doyoutrade.observability.debug_span_export import drain_debug_span_persist_queue


@unittest.skipUnless(e2e_enabled(), "set DOYOUTRADE_E2E=1 to run end-to-end tests")
class AssistantChatValidationE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_assistant_chat_export_includes_run_trace_spans_and_model_invocations(self) -> None:
        async with build_e2e_runtime(
            profile="isolated",
            model_mode=E2EModelMode.STUB,
        ) as ctx:
            assistant_service = ctx.runtime["assistant_service"]
            agent = await assistant_service.agent_repo.create_agent(
                {
                    "name": "E2E Chat Validation Agent",
                    "system_prompt": "You are validating assistant diagnostics for an E2E test.",
                    "status": "active",
                    "model_route_name": ctx.model_route_name,
                    "tool_names": [],
                    "tool_configs": [],
                }
            )
            session = await assistant_service.create_session(
                agent_id=agent["id"],
                title="E2E assistant chat validation",
            )

            result = await assistant_service.send_message(
                session_id=session["session_id"],
                content="Reply once so diagnostics can be exported.",
            )
            self.assertEqual(len(result["messages"]), 2)

            export_payload = await self._build_export_when_diagnostics_are_ready(
                assistant_service,
                agent,
                session["session_id"],
            )
            model_settings = ctx.e2e_settings.get("model")
            model_config = model_settings if isinstance(model_settings, dict) else {}
            expected_model = str(model_config.get("target_model") or "e2e-stub-model")

        self.assertGreaterEqual(export_payload["counts"]["messages"], 2)
        self.assertTrue(export_payload["ids"]["latest_attempt_id"].startswith("attempt-"))
        self.assertTrue(export_payload["ids"]["run_ids"], export_payload)
        self.assertTrue(export_payload["ids"]["trace_ids"], export_payload)
        self.assertGreaterEqual(export_payload["counts"]["spans"], 1, export_payload)
        self.assertGreaterEqual(export_payload["counts"]["model_invocations"], 1, export_payload)

        markdown = export_payload["export_text"]
        invocation_models = [
            invocation.get("model")
            for invocation in export_payload["model_invocations"]
        ]
        self.assertIn(expected_model, invocation_models, export_payload)
        self.assertIn("attempt-", markdown)
        self.assertIn("asst-run-", markdown)
        self.assertIn("assistant_loop", markdown)
        self.assertIn("assistant.loop", markdown)
        self.assertIn(expected_model, markdown)

    async def _build_export_when_diagnostics_are_ready(
        self,
        assistant_service: Any,
        agent: dict[str, Any],
        session_id: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_payload: dict[str, Any] | None = None
        while True:
            await wait_for_model_invocation_tasks()
            await drain_debug_span_persist_queue()

            session = await assistant_service.get_session(session_id)
            messages = await assistant_service.list_messages(session_id, limit=1000, offset=0)
            events = await assistant_service.list_events(session_id, after_id=None, limit=1000)
            traces = await assistant_service.list_traces(session_id, limit=200, offset=0)
            trace_details = []
            for trace in traces.get("items") or []:
                detail = await assistant_service.get_trace_detail(trace["trace_id"])
                if detail is not None:
                    trace_details.append(detail)

            last_payload = build_assistant_session_export(
                session=session,
                agent=agent,
                messages=messages,
                events=events,
                traces=traces,
                trace_details=trace_details,
                fmt="markdown",
                include_traces=True,
            )
            if (
                last_payload["ids"]["run_ids"]
                and last_payload["ids"]["trace_ids"]
                and last_payload["counts"]["spans"] >= 1
                and last_payload["counts"]["model_invocations"] >= 1
            ):
                return last_payload
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"assistant diagnostics were not exported in time: {last_payload}")
            await asyncio.sleep(0.1)
