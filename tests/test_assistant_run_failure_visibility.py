"""A mid-run assistant failure (e.g. a streaming ``httpx.ReadTimeout``) must stay
visible: the partial work is persisted as a flagged assistant message instead of the
chat showing only the user's query, and a transient transport error is retried once at
the model-call level before any tool is dispatched.

Regression coverage for the ReadTimeout-invisible-failure bug. See CLAUDE.md §错误可见性.
"""

import unittest
from unittest.mock import MagicMock

import httpx

from doyoutrade.agent_runtime import AgentToolCall, AgentTurnResponse
from doyoutrade.assistant import AssistantService, InMemoryAssistantRepository
from doyoutrade.assistant.service import (
    _AssistantRunError,
    _is_retryable_model_transport_error,
)
from doyoutrade.tools import OperationHandler, OperationRegistry


class _DummyTool(OperationHandler):
    name = "dummy_tool"
    description = "Dummy test tool"
    category = "kline"
    parameters = {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    }

    async def execute(self, symbol: str) -> str:
        return f'{{"status":"ok","symbol":"{symbol}"}}'


class _ReadTimeoutAdapter:
    """Streams a text delta, then fails mid-stream like ``httpx.ReadTimeout``.

    Fails on the first ``fail_times`` calls, then returns a normal answer so the
    retry path can be exercised.
    """

    def __init__(self, *, fail_times: int = 99, stream: bool = True) -> None:
        self.calls = 0
        self.fail_times = fail_times
        self.stream = stream

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        self.calls += 1
        if self.stream and on_text_delta is not None:
            await on_text_delta("部分")
        if self.calls <= self.fail_times:
            raise httpx.ReadTimeout("read timed out")
        return AgentTurnResponse(content="恢复成功", tool_calls=[], raw=None)


class _ValueErrorAdapter:
    """Raises a non-transport error that must NOT be retried."""

    def __init__(self) -> None:
        self.calls = 0

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        self.calls += 1
        raise ValueError("boom")


class _PrefaceToolThenTimeoutAdapter:
    """Turn 1 streams a preface + a tool call; turn 2's model call fails pre-stream.

    Exercises the multi-turn case where the failure happens on a turn that produced
    NO output of its own — the persisted failure must not misattribute turn 1's text.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        self.calls += 1
        if self.calls == 1:
            if on_text_delta is not None:
                await on_text_delta("先查行情。")
            return AgentTurnResponse(
                content="先查行情。",
                tool_calls=[
                    AgentToolCall(id="c1", name="dummy_tool", arguments={"symbol": "600000.SH"})
                ],
                raw=MagicMock(tool_calls=None, content="先查行情。"),
            )
        raise httpx.ReadTimeout("read timed out")


class _FailThenCaptureAdapter:
    """First run fails pre-stream; the second run captures the model history it sees."""

    def __init__(self) -> None:
        self.calls = 0
        self.captured_second: list[str] | None = None

    async def agent_turn(self, messages, *, tools=None, on_text_delta=None, on_thinking_delta=None):
        self.calls += 1
        if self.calls == 1:
            raise httpx.ReadTimeout("read timed out")
        self.captured_second = [str(getattr(m, "content", "")) for m in messages]
        return AgentTurnResponse(content="第二次回答", tool_calls=[], raw=None)


class AssistantRunFailureVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._services: list[AssistantService] = []

    async def asyncTearDown(self) -> None:
        for service in self._services:
            await service.aclose()

    def _track(self, service: AssistantService) -> AssistantService:
        self._services.append(service)
        return service

    def _service(self, adapter, *, max_retries: int = 1, tool_registry=None) -> AssistantService:
        async def _factory(_route_name):
            return adapter

        kwargs: dict = {"model_adapter_factory": _factory}
        if tool_registry is not None:
            kwargs["tool_registry"] = tool_registry
        service = self._track(AssistantService(InMemoryAssistantRepository(), **kwargs))
        service._model_transport_max_retries = max_retries
        return service

    def test_classifier_matches_wrapped_timeout_but_not_plain_error(self):
        self.assertTrue(_is_retryable_model_transport_error(httpx.ReadTimeout("x")))
        # Wrapped in the cause chain (how the SDK surfaces it).
        wrapped = RuntimeError("outer")
        wrapped.__cause__ = httpx.ConnectTimeout("inner")
        self.assertTrue(_is_retryable_model_transport_error(wrapped))
        self.assertFalse(_is_retryable_model_transport_error(ValueError("boom")))

    async def test_failed_run_persists_partial_assistant_message(self):
        adapter = _ReadTimeoutAdapter(fail_times=99)
        service = self._service(adapter, max_retries=0)  # isolate persist-on-failure
        session = await service.create_session(agent_id="test-agent", title="t")

        with self.assertRaises(_AssistantRunError):
            await service.send_message(session_id=session["session_id"], content="hello")

        messages = await service.list_messages(session["session_id"], limit=100, offset=0)
        self.assertEqual([row["role"] for row in messages], ["user", "assistant"])
        failed = messages[1]
        meta = failed["metadata"]
        self.assertTrue(meta.get("failed"))
        self.assertTrue(meta.get("partial"))
        self.assertEqual(meta.get("error_type"), "ReadTimeout")
        # Streamed partial text is preserved as the message content.
        self.assertEqual(failed["content"], "部分")

        sess = await service.get_session(session["session_id"])
        self.assertEqual(sess["status"], "error")

        events = await service.list_events(session["session_id"], limit=100)
        failed_events = [e for e in events if e["event_type"] == "attempt.failed"]
        self.assertEqual(len(failed_events), 1)
        payload = failed_events[0]["payload"]
        self.assertEqual(payload.get("error_type"), "ReadTimeout")
        self.assertEqual(payload.get("message_id"), failed["message_id"])
        self.assertEqual(adapter.calls, 1)  # no retry when max_retries=0

    async def test_failed_run_without_stream_persists_error_notice(self):
        adapter = _ValueErrorAdapter()
        service = self._service(adapter, max_retries=1)
        session = await service.create_session(agent_id="test-agent", title="t")

        with self.assertRaises(_AssistantRunError):
            await service.send_message(session_id=session["session_id"], content="hello")

        messages = await service.list_messages(session["session_id"], limit=100, offset=0)
        self.assertEqual([row["role"] for row in messages], ["user", "assistant"])
        failed = messages[1]
        meta = failed["metadata"]
        self.assertTrue(meta.get("failed"))
        self.assertFalse(meta.get("partial"))
        self.assertEqual(meta.get("error_type"), "ValueError")
        # No partial text => a user-facing failure notice is synthesized.
        self.assertIn("本轮运行失败", failed["content"])
        self.assertIn("ValueError", failed["content"])

        # A non-transport error is never retried, even with max_retries=1.
        self.assertEqual(adapter.calls, 1)
        events = await service.list_events(session["session_id"], limit=100)
        self.assertEqual(
            [e for e in events if e["event_type"] == "assistant_model_transport_retry"],
            [],
        )

    async def test_transport_timeout_is_retried_then_recovers(self):
        adapter = _ReadTimeoutAdapter(fail_times=1)
        service = self._service(adapter, max_retries=1)
        session = await service.create_session(agent_id="test-agent", title="t")

        await service.send_message(session_id=session["session_id"], content="hello")

        messages = await service.list_messages(session["session_id"], limit=100, offset=0)
        self.assertEqual([row["role"] for row in messages], ["user", "assistant"])
        ok_message = messages[1]
        self.assertEqual(ok_message["content"], "恢复成功")
        self.assertNotEqual(ok_message["metadata"].get("failed"), True)
        self.assertEqual(adapter.calls, 2)  # one failed attempt + one successful retry

        sess = await service.get_session(session["session_id"])
        self.assertEqual(sess["status"], "idle")

        events = await service.list_events(session["session_id"], limit=100)
        retry_events = [e for e in events if e["event_type"] == "assistant_model_transport_retry"]
        self.assertEqual(len(retry_events), 1)
        retry_payload = retry_events[0]["payload"]
        self.assertEqual(retry_payload.get("error_type"), "ReadTimeout")
        self.assertEqual(retry_payload.get("model_attempt"), 1)
        self.assertEqual(retry_payload.get("reason"), "model_transport_timeout")

    async def test_later_turn_failure_does_not_misattribute_prior_turn_text(self):
        # A failure on turn 2 (which produced nothing) must not persist turn 1's
        # preface as this turn's "partial" output, and must surface the error notice.
        adapter = _PrefaceToolThenTimeoutAdapter()
        service = self._service(
            adapter, max_retries=0, tool_registry=OperationRegistry([_DummyTool()])
        )
        session = await service.create_session(agent_id="test-agent", title="t")

        with self.assertRaises(_AssistantRunError):
            await service.send_message(session_id=session["session_id"], content="hello")

        self.assertEqual(adapter.calls, 2)  # turn 1 (tool call) + turn 2 (timeout)
        messages = await service.list_messages(session["session_id"], limit=100, offset=0)
        failed = messages[-1]
        meta = failed["metadata"]
        self.assertTrue(meta.get("failed"))
        self.assertFalse(meta.get("partial"))  # turn 2 produced no output
        self.assertEqual(meta.get("error_type"), "ReadTimeout")
        # Content is the synthesized notice, NOT turn 1's preface.
        self.assertIn("本轮运行失败", failed["content"])
        self.assertNotIn("先查行情", failed["content"])

    async def test_failed_message_is_not_replayed_into_model_history(self):
        adapter = _FailThenCaptureAdapter()
        service = self._service(adapter, max_retries=0)
        session = await service.create_session(agent_id="test-agent", title="t")

        with self.assertRaises(_AssistantRunError):
            await service.send_message(session_id=session["session_id"], content="问题一")
        # Second message succeeds; its model context must omit the failure notice.
        await service.send_message(session_id=session["session_id"], content="问题二")

        joined = "\n".join(adapter.captured_second or [])
        self.assertNotIn("本轮运行失败", joined)

        # The failure marker is still persisted for the UI (visible in the chat).
        messages = await service.list_messages(session["session_id"], limit=100, offset=0)
        self.assertEqual(
            [row["role"] for row in messages],
            ["user", "assistant", "user", "assistant"],
        )
        self.assertTrue(messages[1]["metadata"].get("failed"))
        self.assertEqual(messages[3]["content"], "第二次回答")


if __name__ == "__main__":
    unittest.main()
