"""ask_user_question end-to-end regression (fizz-style blocking): the tool call
suspends inside its execution slot until the user answers; the answer is fed
back as THIS call's tool_result and the SAME run continues — no synthetic user
message, no new attempt. Option clicks (answer endpoint / QuestionBroker) and
free-typed replies both resolve the wait with visible user_question.* events;
stale clicks with no live wait never crash; malformed input fails with
structured errors."""

import asyncio
import unittest
from typing import Any
from unittest.mock import MagicMock

from doyoutrade.assistant import AssistantService, InMemoryAssistantRepository
from doyoutrade.tools import OperationRegistry
from doyoutrade.tools.ask_user import AskUserQuestionTool
from tests.scripted_model import ScriptedModelAdapter, call_tool, say

_QUESTION_ARGS = {
    "question": "回测区间用哪个？",
    "header": "回测区间",
    "options": [
        {"label": "近一年", "description": "2025-06 至今"},
        {"label": "近三年", "description": "覆盖完整牛熊"},
    ],
}


class AskUserQuestionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._services: list[AssistantService] = []

    async def asyncTearDown(self) -> None:
        for service in self._services:
            await service.aclose()

    def _build(self, adapter: ScriptedModelAdapter):
        repo = InMemoryAssistantRepository()
        service = AssistantService(
            repo,
            model_adapter_factory=adapter.factory,
            tool_registry=OperationRegistry(
                [AskUserQuestionTool(assistant_repository=repo)]
            ),
        )
        self._services.append(service)
        return service, repo

    async def _pending(self, repo, session_id: str):
        session = await repo.get_session(session_id)
        return (session.get("config") or {}).get("pending_user_question")

    async def _event_types(self, service, session_id: str) -> list[str]:
        events = await service.list_events(session_id, limit=200)
        return [e["event_type"] for e in events]

    async def _wait_for_pending_question(self, service, session_id: str) -> str:
        """Poll until the ask_user tool has suspended on a broker future."""
        for _ in range(500):
            pending = service.question_broker.list_pending(session_id)
            if pending:
                return pending[0]["question_id"]
            await asyncio.sleep(0.005)
        raise AssertionError("ask_user_question never suspended on the broker")

    async def test_option_answer_continues_same_run_without_user_message(self):
        def _expect_answer_visible(messages, _tools):
            joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
            assert '"selected"' in joined and "近一年" in joined, (
                "structured answer not fed back as tool_result"
            )

        adapter = ScriptedModelAdapter(
            [
                call_tool("ask_user_question", dict(_QUESTION_ARGS)),
                say("收到，按近一年跑。", expect=_expect_answer_visible),
            ]
        )
        service, repo = self._build(adapter)
        session = await service.create_session(agent_id="test-agent", title="ask")
        sid = session["session_id"]

        # send_message blocks until the question is answered (the turn suspends
        # inside the tool slot), so drive it as a task and resolve concurrently.
        run = asyncio.create_task(service.send_message(session_id=sid, content="帮我定回测区间"))
        question_id = await self._wait_for_pending_question(service, sid)
        self.assertIsNotNone(await self._pending(repo, sid))
        self.assertIn("user_question.asked", await self._event_types(service, sid))

        accepted = service.question_broker.resolve(
            question_id, selected=["近一年"], source="option_click"
        )
        self.assertTrue(accepted)

        result = await run
        # Same run continued: pending cleared, answered event emitted.
        self.assertIsNone(await self._pending(repo, sid))
        self.assertIn("user_question.answered", await self._event_types(service, sid))
        # NO synthetic user message for the answer — the only user message is the
        # original prompt.
        user_rows = [
            m
            for m in await service.list_messages(sid, limit=100, offset=0)
            if m["role"] == "user"
        ]
        self.assertEqual(len(user_rows), 1)
        self.assertEqual(user_rows[0]["content"], "帮我定回测区间")
        # The final assistant message carries the answered recap on the block.
        block = next(
            b
            for b in result["messages"][-1]["metadata"].get("content_blocks", [])
            if b.get("type") == "user_question"
        )
        self.assertTrue(block.get("answered"))
        self.assertEqual(block.get("selected"), ["近一年"])
        self.assertEqual(result["messages"][-1]["content"], "收到，按近一年跑。")
        adapter.assert_exhausted()

    async def test_free_text_reply_resolves_without_new_attempt(self):
        adapter = ScriptedModelAdapter(
            [
                call_tool("ask_user_question", dict(_QUESTION_ARGS)),
                say("明白，按你说的来。"),
            ]
        )
        service, repo = self._build(adapter)
        session = await service.create_session(agent_id="test-agent", title="free")
        sid = session["session_id"]

        run = asyncio.create_task(service.send_message(session_id=sid, content="定一下区间"))
        await self._wait_for_pending_question(service, sid)

        # A free-typed reply while a question is pending resolves the wait — it
        # returns a light envelope and does NOT start a new attempt.
        envelope = await service.send_message(session_id=sid, content="都不要，用 2024 全年")
        self.assertTrue(envelope.get("resolved_user_question", {}).get("accepted"))
        self.assertEqual(envelope.get("messages"), [])

        await run
        self.assertIsNone(await self._pending(repo, sid))
        self.assertIn("user_question.answered", await self._event_types(service, sid))
        user_rows = [
            m
            for m in await service.list_messages(sid, limit=100, offset=0)
            if m["role"] == "user"
        ]
        self.assertEqual([m["content"] for m in user_rows], ["定一下区间"])

    async def test_stale_click_with_no_live_wait_is_visible_not_fatal(self):
        # No live suspended wait: a stale /ask_user click is surfaced, does not
        # start a model turn, and never crashes.
        adapter = ScriptedModelAdapter([])
        service, _repo = self._build(adapter)
        session = await service.create_session(agent_id="test-agent", title="stale")
        sid = session["session_id"]

        envelope = await service.send_message(
            session_id=sid, content="/ask_user uq-deadbeef 近一年"
        )
        self.assertFalse(envelope.get("resolved_user_question", {}).get("accepted"))
        self.assertIn(
            "user_question.stale_answer", await self._event_types(service, sid)
        )
        # No model turn ran and no user message was persisted.
        self.assertEqual(len(adapter.calls), 0)
        self.assertEqual(await service.list_messages(sid, limit=100, offset=0), [])

    async def test_user_stop_cancels_the_suspended_question(self):
        adapter = ScriptedModelAdapter(
            [call_tool("ask_user_question", dict(_QUESTION_ARGS)), say("unreachable")]
        )
        service, repo = self._build(adapter)
        session = await service.create_session(agent_id="test-agent", title="stop")
        sid = session["session_id"]

        run = asyncio.create_task(service.send_message(session_id=sid, content="定区间"))
        await self._wait_for_pending_question(service, sid)
        await service.stop_attempt(sid)
        with self.assertRaises(Exception):
            await run
        # The abort race covered the human-wait; the second scripted step never ran.
        self.assertLessEqual(len(adapter.calls), 1)

    async def test_validation_rejects_bad_options(self):
        repo = InMemoryAssistantRepository()
        tool = AskUserQuestionTool(assistant_repository=repo)
        session = await repo.create_session(agent_id="a", title="t")

        too_few = await tool.execute(
            question="选哪个？",
            options=[{"label": "唯一"}],
            session_id=session["session_id"],
        )
        self.assertTrue(too_few.is_error)
        self.assertIn("validation_error", too_few.text)

        duplicated = await tool.execute(
            question="选哪个？",
            options=[{"label": "相同"}, {"label": "相同"}],
            session_id=session["session_id"],
        )
        self.assertTrue(duplicated.is_error)
        self.assertIn("duplicates", duplicated.text)

        unknown = await tool.execute(
            question="选哪个？",
            options=[{"label": "甲"}, {"label": "乙"}],
            optoins_typo="x",
            session_id=session["session_id"],
        )
        self.assertTrue(unknown.is_error)
        self.assertIn("unknown", unknown.text.lower())

    async def test_unwired_runtime_fails_loudly(self):
        tool = AskUserQuestionTool()
        result = await tool.execute(
            question="选哪个？",
            options=[{"label": "甲"}, {"label": "乙"}],
            session_id="asst-x",
        )
        self.assertTrue(result.is_error)
        self.assertIn("ask_user_unwired", result.text)


class AskUserFeishuCardTests(unittest.IsolatedAsyncioTestCase):
    def test_card_shape_buttons_and_text_fallback(self):
        from doyoutrade.assistant.channels.feishu.card.builder import build_ask_user_card

        pending = {
            "question_id": "uq-12345678",
            "question": "回测区间用哪个？",
            "header": "回测区间",
            "options": [
                {"label": "近一年", "description": "2025-06 至今"},
                {"label": "近三年", "description": None},
            ],
            "multi_select": False,
        }
        card = build_ask_user_card(pending)
        self.assertEqual(card["schema"], "2.0")
        elements = card["body"]["elements"]

        def _buttons(card):
            found = []

            def _walk(items):
                for item in items or []:
                    tag = item.get("tag")
                    if tag == "button":
                        found.append(item)
                    elif tag == "column_set":
                        for col in item.get("columns", []):
                            _walk(col.get("elements", []))

            _walk(card.get("body", {}).get("elements", []))
            return found

        buttons = _buttons(card)
        option_buttons = [
            button
            for button in buttons
            if button.get("value", {}).get("action") == "ask_user_select"
        ]
        self.assertEqual(
            [b["value"]["option_label"] for b in option_buttons], ["近一年", "近三年"]
        )
        self.assertTrue(
            all(b["value"]["action"] == "ask_user_select" for b in option_buttons)
        )
        self.assertTrue(
            all(b["value"]["ask_user_id"] == "uq-12345678" for b in option_buttons)
        )
        # Free-text escape hatch present.
        self.assertTrue(any(e.get("tag") == "input" for e in elements))
        submit_buttons = [
            button
            for button in buttons
            if button.get("value", {}).get("action") == "ask_user_text"
        ]
        self.assertEqual(submit_buttons[0]["value"]["action"], "ask_user_text")

    async def test_streaming_controller_sends_card_and_raises_on_failure(self):
        from doyoutrade.assistant.channels.feishu.card.streaming import (
            StreamingCardController,
        )

        pending = {
            "question_id": "uq-1",
            "question": "Q?",
            "options": [{"label": "A"}, {"label": "B"}],
        }
        cardkit = MagicMock()
        cardkit.send_card_json.return_value = "msg_1"
        controller = StreamingCardController(
            cardkit_client=cardkit, chat_id="c", receive_id="u"
        )
        await controller.on_user_question(pending)
        kwargs: dict[str, Any] = cardkit.send_card_json.call_args.kwargs
        self.assertEqual(kwargs["receive_id"], "u")
        self.assertEqual(kwargs["card"]["schema"], "2.0")

        cardkit.send_card_json.return_value = None
        with self.assertRaises(RuntimeError):
            await controller.on_user_question(pending)


if __name__ == "__main__":
    unittest.main()
