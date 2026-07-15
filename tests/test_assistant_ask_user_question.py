"""ask_user_question end-to-end regression: the tool records the pending
question and ends the turn; option clicks (/ask_user protocol) and free-text
replies both answer + clear it with visible user_question.* events; stale
clicks never crash; malformed input fails with structured errors."""

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

    async def test_ask_then_option_click_answers(self):
        def _expect_answer_visible(messages, _tools):
            joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
            assert "我的选择：近一年" in joined, "rewritten answer not visible to model"

        adapter = ScriptedModelAdapter(
            [
                call_tool("ask_user_question", dict(_QUESTION_ARGS)),
                say("好的，等你选择。"),
                say("收到，按近一年跑。", expect=_expect_answer_visible),
            ]
        )
        service, repo = self._build(adapter)
        session = await service.create_session(agent_id="test-agent", title="ask")
        sid = session["session_id"]

        result = await service.send_message(session_id=sid, content="帮我定回测区间")
        pending = await self._pending(repo, sid)
        self.assertIsNotNone(pending)
        question_id = pending["question_id"]
        self.assertEqual(len(pending["options"]), 2)
        # The persisted assistant message carries the user_question block.
        block_types = [
            block.get("type")
            for block in result["messages"][-1]["metadata"].get("content_blocks", [])
        ]
        self.assertIn("user_question", block_types)
        self.assertIn("user_question.asked", await self._event_types(service, sid))

        # Option click arrives via the /ask_user protocol.
        result = await service.send_message(
            session_id=sid, content=f"/ask_user {question_id} 近一年"
        )
        self.assertIsNone(await self._pending(repo, sid))
        event_types = await self._event_types(service, sid)
        self.assertIn("user_question.answered", event_types)
        # The persisted user message is readable text, not the raw protocol.
        user_rows = [
            m
            for m in await service.list_messages(sid, limit=100, offset=0)
            if m["role"] == "user"
        ]
        self.assertIn("我的选择：近一年", user_rows[-1]["content"])
        adapter.assert_exhausted()

    async def test_free_text_reply_clears_pending(self):
        adapter = ScriptedModelAdapter(
            [
                call_tool("ask_user_question", dict(_QUESTION_ARGS)),
                say("等你选择。"),
                say("明白，按你说的来。"),
            ]
        )
        service, repo = self._build(adapter)
        session = await service.create_session(agent_id="test-agent", title="free")
        sid = session["session_id"]

        await service.send_message(session_id=sid, content="定一下区间")
        self.assertIsNotNone(await self._pending(repo, sid))

        await service.send_message(session_id=sid, content="都不要，用 2024 全年")
        self.assertIsNone(await self._pending(repo, sid))
        events = await self._event_types(service, sid)
        self.assertIn("user_question.answered", events)

    async def test_stale_click_is_visible_not_fatal(self):
        adapter = ScriptedModelAdapter([say("好的。")])
        service, repo = self._build(adapter)
        session = await service.create_session(agent_id="test-agent", title="stale")
        sid = session["session_id"]

        result = await service.send_message(
            session_id=sid, content="/ask_user uq-deadbeef 近一年"
        )
        self.assertIn("user_question.stale_answer", await self._event_types(service, sid))
        # The answer text still reaches the model as a plain message.
        user_rows = [
            m
            for m in await service.list_messages(sid, limit=100, offset=0)
            if m["role"] == "user"
        ]
        self.assertEqual(user_rows[-1]["content"], "近一年")
        self.assertEqual(result["messages"][-1]["content"], "好的。")

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
